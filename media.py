"""Media encoders and packet layouts shared with the Nano's C receivers."""
import concurrent.futures
from contextlib import contextmanager
import json
import math
import os
import re
import signal
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
VIDEO_QUALITY = {
    "double": (128, 72, 0, 0), "compact": (192, 112, 160, 320),
    "medium": (256, 144, 225, 480), "large": (320, 176, 320, 650),
    "native": (432, 240, 500, 950),
}
FRAME_RATES = (5, 10, 15, 22, 30)
IMA_BLOCK = 1024
ID = re.compile(r"[A-Za-z0-9_-]{11}")
GROUP = 12
MAX_SAMPLE = 16 * 1024
HEADER = 60
UINT32_MAX = (1 << 32) - 1
YT_DLP = ["yt-dlp", "--ignore-config", "--no-progress", "--no-warnings",
          "--js-runtimes", "node", "--socket-timeout", "15", "--extractor-retries", "1"]


YOUTUBE_NO_JS = os.environ.get("NANO_YOUTUBE_NO_JS") == "1"
YOUTUBE_VIDEO_NO_JS = os.environ.get("NANO_YOUTUBE_VIDEO_NO_JS") == "1"


def yt_dlp_args(video):
    if YOUTUBE_NO_JS or (video and YOUTUBE_VIDEO_NO_JS):
        return [*YT_DLP, "--no-js-runtimes"]
    return YT_DLP


class YouTubeFormatUnavailable(RuntimeError):
    pass


def youtube_download_error(log):
    lines = log.strip().splitlines()
    detail = next((line for line in reversed(lines) if line.startswith("ERROR:")), "")
    detail = detail.removeprefix("ERROR: ") or (lines[-1] if lines else "YouTube download failed.")
    if "Requested format is not available" in detail:
        return YouTubeFormatUnavailable(detail)
    return RuntimeError(detail)


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
    magic = (b"NYC3" if cover else b"NYW3") if token else (b"NYC1" if cover else b"NCL1")
    capacity = 44 if token else 48
    suffix = extension.encode("ascii")
    stem = Path(name).stem or "Audio"
    label = (utf8(stem, capacity - len(suffix)) + suffix).ljust(capacity, b"\0")
    return record(magic, words(size, hash_value) + label + (words(token) if token else b""))


def process(args, check, timeout=180):
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as child:
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
                detail = stderr.decode(errors="replace")
                if args[0] == YT_DLP[0]:
                    raise youtube_download_error(detail)
                raise RuntimeError(detail.strip()[-400:] or f"{args[0]} failed.")
            return stdout
        finally:
            stop_process(child)


def audio_args(source, output, quality, adpcm):
    rate, channels = AUDIO[quality]
    codec = ["-c:a", "adpcm_ima_wav", "-block_size", str(IMA_BLOCK)] if adpcm else ["-c:a", "pcm_mulaw"]
    return ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source), "-map", "0:a:0", "-vn",
            "-ac", str(channels), "-ar", str(rate), *codec, "-rf64", "never", "-fflags", "+bitexact",
            "-flags:a", "+bitexact", "-flush_packets", "1", str(output)]


def samples_per_block(channels):
    return (IMA_BLOCK - 4 * channels) * 2 // channels + 1


def audio_file(source, output, quality, adpcm, check):
    process(audio_args(source, output, quality, adpcm), check)
    size = output.stat().st_size
    header_size = 60 if adpcm else 58
    with output.open("rb") as file:
        header = file.read(header_size)
    if (len(header) != header_size or not header_size < size <= UINT32_MAX
            or header[:4] != b"RIFF" or header[-8:-4] != b"data"
            or header[20] != (17 if adpcm else 7)
            or struct.unpack_from("<I", header, 4)[0] != size - 8):
        raise ValueError("FFmpeg did not produce a compatible WAV.")
    length = struct.unpack_from("<I", header, header_size - 4)[0]
    if not length or size - header_size != length + (length & 1):
        raise ValueError("The WAV length is invalid.")
    if adpcm:
        frames = struct.unpack_from("<I", header, 48)[0]
        samples = samples_per_block(AUDIO[quality][1])
        if not frames or (frames + samples - 1) // samples * IMA_BLOCK != length:
            raise ValueError("The IMA ADPCM block count is invalid.")
    return size


def stop_process(child):
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


@contextmanager
def download_stream(url, folder, video, check):
    formats = ("b[height<=480][protocol^=m3u8]/b[height<=480][ext=webm]/b[height<=480][ext=mp4]/b"
               if video else "ba[ext=webm]/ba/b")
    sort = ["-S", "res:480"] if video else []
    args = [*yt_dlp_args(video), "--no-playlist", "--hls-use-mpegts", *sort,
            "-f", formats, "-o", "-", "--", youtube_url(url)]
    with (folder / "yt-dlp-stream.log").open("w+b") as log, subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=log, start_new_session=True) as child:
        def validate():
            while child.poll() is None:
                check()
                time.sleep(.05)
            if child.returncode:
                log.seek(0)
                raise youtube_download_error(log.read().decode(errors="replace"))

        try:
            yield child.stdout, validate
        except (RuntimeError, ValueError):
            # FFmpeg can see EOF just before yt-dlp finishes reporting an error.
            try:
                result = child.wait(timeout=.25)
            except subprocess.TimeoutExpired:
                result = None
            if result:
                validate()
            raise
        finally:
            stop_process(child)


def audio_blocks(source, output, quality, check):
    with subprocess.Popen(audio_args("pipe:0", output, quality, True), stdin=source,
                          stderr=subprocess.DEVNULL, start_new_session=True) as child:
        position = 0
        try:
            while True:
                check()
                active = child.poll() is None
                if not active and child.returncode:
                    raise RuntimeError("Could not convert the YouTube audio stream.")
                size = output.stat().st_size if output.exists() else 0
                available = max(0, size - 60 - position)
                count = min(8 * IMA_BLOCK, available // IMA_BLOCK * IMA_BLOCK)
                if count:
                    with output.open("rb") as file:
                        if not position:
                            header = file.read(60)
                            if header[20] != 17 or header[-8:-4] != b"data":
                                raise ValueError("Unexpected streamed audio format.")
                        file.seek(60 + position)
                        blocks = file.read(count)
                    if len(blocks) != count:
                        raise OSError("Incomplete encoded audio blocks.")
                    position += count
                    yield blocks
                elif active:
                    time.sleep(.035)
                elif not position or available:
                    raise ValueError("The YouTube audio stream ended without complete audio blocks.")
                else:
                    break
        finally:
            stop_process(child)


def annex_b(data):
    starts = list(re.finditer(rb"\x00\x00(?:\x00)*\x01", data))
    sps = pps = None
    samples = []
    for i, marker in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(data.rstrip(b"\0"))
        nal = data[marker.end():end]
        if not nal:
            raise ValueError("FFmpeg returned an empty H.264 NAL unit.")
        kind = nal[0] & 31
        if kind == 7:
            if sps is not None and sps != nal:
                raise ValueError("H.264 sequence parameters changed within a group.")
            sps = nal
        elif kind == 8:
            if pps is not None and pps != nal:
                raise ValueError("H.264 picture parameters changed within a group.")
            pps = nal
        elif kind in (1, 5):
            if len(nal) + 4 > MAX_SAMPLE or (not samples and kind != 5):
                raise ValueError("Encoded frame exceeds the Nano decoder limits.")
            samples.append(struct.pack(">I", len(nal)) + nal)
    if (not sps or not pps or not 1 <= len(samples) <= GROUP or
            len(sps) < 5 or len(pps) < 2 or sps[1] != 66 or sps[3] > 30 or 11 + len(sps) + len(pps) > 128):
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
            frames = math.ceil(float(stream["duration"]) * fps)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if 0 < frames < UINT32_MAX:
            return frames
    return None


def video_records(source, folder, quality, fps, audio, live, check, *, adpcm=False, stream=None):
    rate, channels = AUDIO[audio]
    groups = GROUP if quality == "double" else 4
    has_audio = stream is not None or bool(process(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index",
         "-of", "csv=p=0", str(source)], check))
    audio_path = folder / "sound.raw"
    width, height, live_cap, saved_cap = VIDEO_QUALITY[quality]
    dimensions = f"{width}:{height}"
    scale = (f"scale={dimensions}:force_original_aspect_ratio=increase:force_divisible_by=2:"
             f"out_color_matrix=bt601:out_range=tv,crop={dimensions}")
    if quality == "double":
        scale += ",pad=128:128:0:28"
    cap = live_cap if live else saved_cap
    limits = ["-maxrate", f"{cap}k", "-bufsize", f"{min(cap // 2, 110)}k"] if cap else []
    params = (f"keyint={groups}:scenecut=0:bframes=0:ref=1:slices=1:repeat-headers=1:sliced-threads=0"
              + (":force-cfr=1" if cap else ""))
    args = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", "pipe:0" if stream is not None else str(source),
            "-map", "0:v:0", "-an", "-vf", f"fps={fps}:start_time=0:round=up:eof_action=pass,{scale},setsar=1",
            "-c:v", "libx264", "-threads", str(min(4, os.cpu_count() or 1)), "-thread_type", "frame",
            "-profile:v", "baseline", "-level:v", "3.0", "-pix_fmt", "yuv420p", "-tune", "zerolatency",
            "-crf", "13" if quality == "double" else "24", "-color_range", "tv", "-colorspace", "smpte170m",
            *limits, "-x264-params", params,
            "-f", "segment", "-segment_time", str(groups / fps), "-segment_time_delta", str(.5 / fps),
            "-reset_timestamps", "1", "-segment_format", "h264", str(folder / "%08d.h264")]
    if has_audio:
        args += ["-map", "0:a:0", "-vn", "-af", f"aresample={rate}:async=1:first_pts=0", "-ac", str(channels),
                 "-ar", str(rate), "-c:a", "pcm_s16le" if adpcm else "pcm_mulaw", "-flush_packets", "1",
                 "-f", "s16le" if adpcm else "mulaw", str(audio_path)]
    config = None
    frames = index = 0
    width_bytes = channels * (2 if adpcm else 1)
    with (folder / "ffmpeg.log").open("w+b") as log, subprocess.Popen(
            args, stdin=stream, stderr=log, start_new_session=True) as child:
        try:
            while True:
                check()
                active = child.poll() is None
                segment = folder / f"{index:08d}.h264"
                if active and not (folder / f"{index + 1:08d}.h264").exists():
                    time.sleep(.025)
                    continue
                if not active and child.returncode:
                    log.seek(0)
                    raise RuntimeError(log.read().decode(errors="replace").strip()[-400:] or "H.264 encoding failed.")
                if not segment.exists():
                    break
                avcc, samples = annex_b(segment.read_bytes())
                if len(samples) > groups or (active and len(samples) != groups):
                    raise ValueError("FFmpeg returned an unsupported H.264 group.")
                stream_header = None
                if config is None:
                    config = avcc
                    tag = b"NVL" if live else b"NVD"
                    flags = channels | (0x80000000 if adpcm else 0)
                    stream_header = tag + (b"2" if groups == GROUP else b"3") + words(len(config), fps, rate, flags) + config
                elif config != avcc:
                    raise ValueError("H.264 configuration changed mid-clip.")
                start = (frames * rate // fps) * width_bytes
                frames += len(samples)
                if frames >= UINT32_MAX:
                    raise ValueError("Invalid video duration.")
                stop = (frames * rate // fps) * width_bytes
                sound = b""
                if has_audio:
                    while child.poll() is None and (not audio_path.exists() or audio_path.stat().st_size < stop):
                        check()
                        time.sleep(.025)
                    if audio_path.exists():
                        with audio_path.open("rb") as file:
                            file.seek(start)
                            sound = file.read(stop - start)
                if adpcm:
                    sound = sound[:len(sound) // width_bytes * width_bytes].ljust(stop - start, b"\0")
                    sound = video_adpcm(sound, channels)
                else:
                    sound = sound.ljust(stop - start, b"\xff")
                body = b"".join(words(len(sample)) + sample for sample in samples) + sound
                # The caller commits the Nano transfer as soon as it sees the
                # stream header. Wait until the first full group is available.
                group = words(len(body), len(samples), checksum(body)) + body
                if stream_header is not None:
                    yield stream_header
                yield group
                segment.unlink()
                index += 1
            if not frames:
                raise ValueError("Invalid video duration.")
            yield words(0, frames, 0)
        finally:
            stop_process(child)


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
    if video:
        formats = ("bv[height<=480][vcodec^=avc1][ext=mp4]+ba[ext=m4a]/"
                   "b[height<=480][vcodec^=avc1][ext=mp4]/bv*+ba/b/bv")
        args = ["-f", formats, "-S", "res:480", "--merge-output-format", "mp4/mkv"]
    else:
        args = ["-f", "bestaudio[ext=m4a]/bestaudio", "-x", "--audio-format", "m4a", "--write-thumbnail"]
    raw = process([*yt_dlp_args(video), "--no-playlist", *args, "--print", "after_move:filepath",
                   "-o", output, "--", youtube_url(url)], check, timeout=900)
    paths = raw.decode(errors="replace").splitlines()
    if paths:
        file = Path(paths[-1]).resolve()
        if file.parent == folder.resolve() and file.is_file():
            return file
    raise RuntimeError("Could not prepare the YouTube media.")


def search(query, video, check):
    targets = [f"ytsearch8:{query}"] if video else [
        "https://music.youtube.com/search?" + urllib.parse.urlencode({"q": query}) + "#" + kind
        for kind in ("songs", "videos")
    ]

    def entries(target):
        raw = process([*yt_dlp_args(video), "--flat-playlist", "--playlist-items", "1:8", "--dump-json", "--", target],
                      check, timeout=45)
        rows = {}
        for line in raw.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            key, title = row.get("id"), row.get("title")
            if not isinstance(key, str) or not ID.fullmatch(key) or not isinstance(title, str) or not title.strip():
                continue
            names = [row.get("channel"), row.get("uploader")]
            if not video:
                names.insert(0, row.get("artist"))
            author = next((name for name in names if isinstance(name, str) and name), "")
            if not video:
                author = author.removesuffix(" - Topic")
            rows.setdefault(key, {"id": key, "title": title, "author": author,
                                  "thumbnail": row.get("thumbnail"), "thumbnails": row.get("thumbnails") or []})
        return list(rows.values())[:8]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        lists = []
        errors = []
        for future in [pool.submit(entries, target) for target in targets]:
            try:
                lists.append(future.result())
            except (RuntimeError, TimeoutError, ValueError) as error:
                errors.append(error)
        if not lists:
            if video:
                raise errors[0]
            raise RuntimeError("YouTube Music search failed. Check the connection and yt-dlp.") from errors[0]
    results = {}
    for index in range(8):
        for rows in lists:
            if index < len(rows):
                row = rows[index]
                results.setdefault(row["id"], row)
    rows = list(results.values())[:8]
    if not rows:
        raise RuntimeError("No videos found on YouTube." if video else "No tracks found on YouTube Music.")

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


def image_pixels(source, side, video):
    from PIL import Image, ImageOps

    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
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


def cover_pixels(folder):
    for file in folder.glob("YouTube.*"):
        if file.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".avif"):
            try:
                return image_pixels(file, 160, False)
            except (OSError, ValueError):
                continue
    return None


def artwork(result, side, video):
    from io import BytesIO

    fallback = f"https://i.ytimg.com/vi/{result['id']}/mqdefault.jpg"
    high = f"https://i.ytimg.com/vi/{result['id']}/hqdefault.jpg"
    thumbs = [entry["url"] for entry in result["thumbnails"]
              if isinstance(entry, dict) and isinstance(entry.get("url"), str)]
    square = next((url for url in thumbs if (urllib.parse.urlsplit(url).hostname or "")
                   .endswith((".ggpht.com", ".googleusercontent.com"))), "")
    primary = result.get("thumbnail")
    if not isinstance(primary, str):
        primary = ""
    tail = thumbs[-1] if thumbs else ""
    urls = [primary, fallback, high, tail] if video else [square, primary, tail, fallback, high]
    for url in dict.fromkeys(urls):
        try:
            host = urllib.parse.urlsplit(url).hostname or ""
            if not url.startswith("https://") or not host.endswith((".ytimg.com", ".ggpht.com", ".googleusercontent.com")):
                continue
            with urllib.request.urlopen(url, timeout=4) as response:
                data = response.read(2_000_001)
            if len(data) <= 2_000_000:
                return image_pixels(BytesIO(data), side, video)
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


def video_adpcm(pcm, channels):
    frames = len(pcm) // (2 * channels)
    if not frames:
        return b""
    groups = (frames + 6) // 8
    stride = 4 * channels
    encoded = bytearray(stride * (1 + groups))
    samples = struct.unpack(f"<{frames * channels}h", pcm)
    index_change = (-1, -1, -1, -1, 2, 4, 6, 8)
    for channel in range(channels):
        predictor = samples[channel]
        difference = abs(samples[channels + channel] - predictor) if frames > 1 else 0
        index = next((i for i, step in enumerate(IMA_STEPS) if step >= difference), 88)
        struct.pack_into("<hBB", encoded, 4 * channel, predictor, index, 0)
        for frame in range(1, frames):
            error = samples[frame * channels + channel] - predictor
            remaining = abs(error)
            step = IMA_STEPS[index]
            delta, magnitude = step >> 3, 0
            for bit, part in ((4, step), (2, step >> 1), (1, step >> 2)):
                if remaining >= part:
                    remaining -= part
                    magnitude |= bit
                    delta += part
            predictor = max(-32768, min(32767, predictor + (-delta if error < 0 else delta)))
            index = max(0, min(88, index + index_change[magnitude]))
            group, nibble = divmod(frame - 1, 8)
            at = (group + 1) * stride + channel * 4 + nibble // 2
            encoded[at] |= (magnitude | (8 if error < 0 else 0)) << (4 * (nibble % 2))
    return bytes(encoded)


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
