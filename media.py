"""Media encoders and packet layouts shared with the Nano's C receivers."""
import concurrent.futures
import json
import re
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUDIO = {
    "low": (7350, 1), "medium": (11025, 1), "high": (22050, 1),
    "fullMono": (44100, 1), "fullStereo": (44100, 2),
}
CRF = {"low": 32, "balanced": 23, "high": 17, "double": 13}
ID = re.compile(r"[A-Za-z0-9_-]{11}")
GROUP = 12
MAX_SAMPLE = 16 * 1024
HEADER = 60
UINT32_MAX = (1 << 32) - 1
YT_DLP = ["yt-dlp", "--ignore-config", "--no-progress", "--no-warnings",
          "--js-runtimes", "node", "--socket-timeout", "15", "--extractor-retries", "1"]


def words(*values):
    return struct.pack("<" + "I" * len(values), *values)


def checksum(data, value=2166136261):
    for byte in data:
        value = ((value ^ byte) * 16777619) & UINT32_MAX
    return value


def record(tag, payload=b""):
    if len(tag) != 4 or len(payload) > HEADER - 4:
        raise ValueError("Invalid Nano packet")
    return (tag + payload).ljust(HEADER, b"\0")


def utf8(text, limit):
    return text.encode()[:limit].decode("utf-8", "ignore").encode()


def audio_header(size, hash_value, name, extension, token=0, cover=False):
    magic = (b"NYC2" if cover else b"NYW2") if token else b"NCL1"
    capacity = 44 if token else 48
    suffix = extension.encode("ascii")
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", Path(name).stem) or "Audio"
    label = (stem.encode()[:capacity - len(suffix)] + suffix).ljust(capacity, b"\0")
    return record(magic, words(size, hash_value) + label + (words(token) if token else b""))


def process(args, check, timeout=180):
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        end = time.monotonic() + timeout
        try:
            while True:
                check()
                if time.monotonic() >= end:
                    raise TimeoutError(f"{args[0]} timed out.")
                try:
                    stdout, stderr = child.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if child.returncode:
                raise RuntimeError(stderr.decode(errors="replace").strip()[-400:] or f"{args[0]} failed.")
            return stdout
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


def audio_file(source, output, quality, mulaw, check):
    rate, channels = AUDIO[quality]
    args = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source),
            "-map", "0:a:0", "-ac", str(channels), "-ar", str(rate)]
    if not mulaw:
        # Read one sample beyond the 300-second limit to detect long inputs.
        args += ["-af", f"aresample={rate},atrim=end_sample={300 * rate + 1}"]
    process(args + ["-c:a", "pcm_mulaw" if mulaw else "pcm_s16le",
                    "-rf64", "never", "-fflags", "+bitexact", "-flags:a", "+bitexact", str(output)], check)
    size = output.stat().st_size
    with output.open("rb") as file:
        header = file.read(58 if mulaw else 44)
    header_size = len(header)
    marker = 50 if mulaw else 36
    if (header_size != marker + 8 or size <= header_size or size > UINT32_MAX
            or header[:4] != b"RIFF" or header[marker:marker + 4] != b"data"):
        raise ValueError("FFmpeg did not produce a compatible WAV.")
    length = struct.unpack_from("<I", header, marker + 4)[0]
    if size - header_size != length + (length & 1):
        raise ValueError("The WAV length is invalid.")
    if not mulaw and length > rate * channels * 2 * 300:
        raise ValueError("Classic Files and Stream accept audio up to five minutes.")
    return size


def annex_b(data):
    starts = list(re.finditer(rb"\x00\x00(?:\x00)*\x01", data))
    sps = pps = None
    samples = []
    for i, marker in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(data)
        nal = data[marker.end():end]
        if not nal:
            continue
        kind = nal[0] & 31
        if kind == 7:
            sps = nal
        elif kind == 8:
            pps = nal
        elif kind in (1, 5):
            if len(nal) + 4 > MAX_SAMPLE or (not samples and kind != 5):
                raise ValueError("Encoded frame exceeds the Nano decoder limits.")
            samples.append(struct.pack(">I", len(nal)) + nal)
    if (not sps or not pps or not 1 <= len(samples) <= GROUP or
            len(sps) < 5 or sps[1] != 66 or sps[3] > 30 or 11 + len(sps) + len(pps) > 128):
        raise ValueError("FFmpeg did not produce a compatible H.264 group.")
    config = bytes((1, sps[1], sps[2], sps[3], 255, 225))
    config += struct.pack(">H", len(sps)) + sps + b"\1" + struct.pack(">H", len(pps)) + pps
    return config, samples


def video_frame_estimate(source, fps, check):
    raw = process(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                   "stream=duration:format=duration", "-of", "json", str(source)], check)
    info = json.loads(raw)
    for stream in [*info.get("streams", ()), info.get("format", {})]:
        try:
            frames = round(float(stream["duration"]) * fps)
        except (KeyError, ValueError, OverflowError):
            continue
        if frames > 0:
            return frames
    return None


def video_records(source, folder, quality, fps, audio, live, check):
    rate, channels = AUDIO[audio]
    streams = process(["ffprobe", "-v", "error", "-select_streams", "a:0",
                       "-show_entries", "stream=index", "-of", "csv=p=0", str(source)], check)
    audio_path = folder / "sound.ulaw"
    scale = ("scale=128:72:force_original_aspect_ratio=increase:force_divisible_by=2:"
             "out_color_matrix=bt601:out_range=tv,crop=128:72,pad=128:128:0:28,setsar=1")
    args = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source), "-map", "0:v:0", "-an",
            "-vf", f"fps={fps}:start_time=0:round=up:eof_action=pass,{scale}",
            "-c:v", "libx264", "-threads", "1", "-profile:v", "baseline", "-level:v", "3.0",
            "-pix_fmt", "yuv420p", "-tune", "zerolatency", "-crf", str(CRF[quality]),
            "-color_range", "tv", "-colorspace", "smpte170m",
            "-x264-params", "keyint=12:scenecut=0:bframes=0:ref=1:slices=1:repeat-headers=1",
            "-f", "segment", "-segment_time", str(GROUP / fps), "-segment_time_delta", str(.5 / fps),
            "-reset_timestamps", "1", "-segment_format", "h264", str(folder / "%08d.h264")]
    if streams:
        args += ["-map", "0:a:0", "-vn", "-af", f"aresample={rate}:async=1:first_pts=0",
                 "-ac", str(channels), "-ar", str(rate), "-c:a", "pcm_mulaw", "-flush_packets", "1",
                 "-f", "mulaw", str(audio_path)]
    config = None
    frames = index = 0
    with (folder / "ffmpeg.log").open("w+b") as log, subprocess.Popen(args, stderr=log) as child:
        try:
            while True:
                check()
                segment = folder / f"{index:08d}.h264"
                if child.poll() is None and not (folder / f"{index + 1:08d}.h264").exists():
                    time.sleep(.025)
                    continue
                if child.poll() not in (None, 0):
                    log.seek(0)
                    raise RuntimeError(log.read().decode(errors="replace").strip()[-400:] or "H.264 encoding failed.")
                if not segment.exists():
                    break
                avcc, samples = annex_b(segment.read_bytes())
                if config is None:
                    config = avcc
                    yield (b"NVL2" if live else b"NVD2") + words(len(config), fps, rate, channels) + config
                elif config != avcc:
                    raise ValueError("H.264 configuration changed mid-clip.")
                start = (frames * rate // fps) * channels
                frames += len(samples)
                stop = (frames * rate // fps) * channels
                sound = b""
                if streams:
                    while child.poll() is None and (not audio_path.exists() or audio_path.stat().st_size < stop):
                        check()
                        time.sleep(.025)
                    if audio_path.exists():
                        with audio_path.open("rb") as file:
                            file.seek(start)
                            sound = file.read(stop - start)
                body = b"".join(words(len(s)) + s for s in samples) + sound.ljust(stop - start, b"\xff")
                yield words(len(body), len(samples), checksum(body)) + body
                segment.unlink()
                index += 1
            if child.wait() != 0:
                log.seek(0)
                raise RuntimeError(log.read().decode(errors="replace").strip()[-400:] or "H.264 encoding failed.")
            if not frames or frames >= UINT32_MAX:
                raise ValueError("Invalid video duration.")
            yield words(0, frames, 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


def youtube_url(value):
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    url = urllib.parse.urlsplit(value)
    host = url.hostname or ""
    if (url.scheme not in ("https", "http") or url.username or url.password
            or not (host in ("youtube.com", "youtu.be") or host.endswith(".youtube.com"))):
        raise ValueError("Enter a YouTube link.")
    return value


def download(url, folder, video, check):
    output = str(folder / "YouTube.%(ext)s")
    args = (["-f", "bv[height<=480][vcodec^=avc1][ext=mp4]+ba[ext=m4a]/b[height<=480][vcodec^=avc1][ext=mp4]",
             "--merge-output-format", "mp4"] if video else
            ["-f", "bestaudio[ext=m4a]/bestaudio", "-x", "--audio-format", "m4a"])
    process([*YT_DLP, "--no-playlist", *args, "-o", output, "--", youtube_url(url)], check, timeout=900)
    file = folder / ("YouTube.mp4" if video else "YouTube.m4a")
    if not file.is_file():
        raise RuntimeError("Could not prepare the YouTube media.")
    return file


def search(query, video, check):
    targets = [f"ytsearch8:{query}"] if video else [
        "https://music.youtube.com/search?" + urllib.parse.urlencode({"q": query}) + "#" + kind
        for kind in ("songs", "videos")
    ]

    def entries(target):
        raw = process([*YT_DLP, "--flat-playlist", "--playlist-items", "1:8", "--dump-json", "--", target],
                      check, timeout=45)
        return [json.loads(line) for line in raw.splitlines()]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        lists = []
        errors = []
        for future in [pool.submit(entries, target) for target in targets]:
            try:
                lists.append(future.result())
            except (RuntimeError, TimeoutError, ValueError) as error:
                errors.append(error)
        if not lists:
            raise errors[0]
    results = {}
    for index in range(8):
        for rows in lists:
            if index >= len(rows):
                continue
            row = rows[index]
            key, title = row.get("id"), row.get("title")
            if isinstance(key, str) and ID.fullmatch(key) and isinstance(title, str) and title.strip():
                author = (None if video else row.get("artist")) or row.get("channel") or row.get("uploader")
                if not video and isinstance(author, str):
                    author = author.removesuffix(" - Topic")
                results.setdefault(key, {"id": key, "title": title, "author": author if isinstance(author, str) else "",
                                         "thumbnails": row.get("thumbnails") or []})
    rows = list(results.values())[:8]

    def attribute(row):
        if not row["author"]:
            url = "https://www.youtube.com/oembed?" + urllib.parse.urlencode({
                "url": f"https://www.youtube.com/watch?v={row['id']}", "format": "json"})
            try:
                with urllib.request.urlopen(url, timeout=4) as response:
                    name = json.loads(response.read(32768)).get("author_name", "")
                if isinstance(name, str):
                    row["author"] = name if video else name.removesuffix(" - Topic")
            except (OSError, ValueError):
                pass
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(attribute, rows))


def artwork(result, side, video):
    from PIL import Image, ImageOps
    from io import BytesIO

    fallback = f"https://i.ytimg.com/vi/{result['id']}/mqdefault.jpg"
    urls = [fallback]
    if not video:
        urls[:0] = [entry.get("url", "") for entry in result["thumbnails"] if isinstance(entry, dict)]
    for url in dict.fromkeys(urls[:4] + [fallback]):
        try:
            host = urllib.parse.urlsplit(url).hostname or ""
            if not url.startswith("https://") or not host.endswith((".ytimg.com", ".ggpht.com", ".googleusercontent.com")):
                continue
            with urllib.request.urlopen(url, timeout=4) as response:
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                continue
            image = Image.open(BytesIO(data)).convert("RGB")
            if video:
                fitted = ImageOps.contain(image, (side, side), Image.Resampling.LANCZOS)
                image = Image.new("RGB", (side, side))
                image.paste(fitted, ((side - fitted.width) // 2, (side - fitted.height) // 2))
            else:
                image = ImageOps.fit(image, (side, side), Image.Resampling.LANCZOS)
            pixels = bytearray(side * side * 2)
            for i, (r, g, b) in enumerate(image.getdata()):
                struct.pack_into("<H", pixels, i * 2, (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3))
            return bytes(pixels)
        except (OSError, ValueError):
            continue
    return None

IMA_STEPS = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253,
    279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166,
    1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026,
    4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
)


def ble_clip(source, quality, check):
    rate = 22050 // quality
    raw = process(["ffmpeg", "-v", "error", "-nostdin", "-i", str(source),
                   "-map", "0:a:0", "-ac", "1", "-ar", str(rate),
                   "-af", f"aresample={rate},atrim=end_sample={30 * rate + 1}",
                   "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1"], check)
    frames = len(raw) // 2
    if not 0 < frames <= 30 * rate:
        raise ValueError("BLE audio clips must be between zero and 30 seconds.")
    packed = bytearray((frames + 1) // 2)
    predictor = index = 0
    for i, (sample,) in enumerate(struct.iter_unpack("<h", raw)):
        difference = sample - predictor
        remaining = abs(difference)
        step = IMA_STEPS[index]
        delta = step >> 3
        code = 8 if difference < 0 else 0
        for bit in (4, 2, 1):
            part = (step * bit) >> 2
            if remaining >= part:
                remaining -= part
                delta += part
                code |= bit
        predictor = min(32767, max(-32768, predictor + (-delta if code & 8 else delta)))
        index = min(88, max(0, index + (-1, -1, -1, -1, 2, 4, 6, 8)[code & 7]))
        packed[i // 2] |= code << ((i & 1) * 4)
    return frames, packed, checksum(packed)
