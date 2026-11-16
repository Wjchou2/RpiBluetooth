"""Classic L2CAP sessions and media transfer jobs."""
import concurrent.futures
from contextlib import closing
import errno
import logging
import re
import secrets
import socket
import struct
import tempfile
import threading
import time
from pathlib import Path

import media

DESTINATIONS = {"youtubeAudio": 0x1009, "youtubeVideo": 0x100B}
DEFAULTS = {
    "youtubeAudio": {"audio": "fullMono", "playback": "live", "download": True},
    "youtubeVideo": {"audio": "fullMono", "playback": "live", "download": True,
                     "quality": "large", "fps": 22, "convert": True},
}
ADDRESS = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}")
VIDEO_STREAM_START_TIMEOUT = 45
BLUETOOTH_SEND_TIMEOUT = 5


class VideoStreamStartupTimeout(TimeoutError):
    pass


class PairingKeyMissing(RuntimeError):
    def __init__(self):
        super().__init__("Bluetooth pairing key is missing. Forget the Pi on the Nano, then use "
                         "Device Setup → Pair again in the browser.")


def connect_socket(address, psm, local_address, timeout=12):
    if not hasattr(socket, "BTPROTO_L2CAP"):
        raise RuntimeError("Classic L2CAP requires Linux with Bluetooth support.")
    channel = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    try:
        channel.settimeout(timeout)
        if local_address:
            channel.bind((local_address, 0))
        # Linux BT_SECURITY, medium: authenticate with the paired Nano.
        channel.setsockopt(getattr(socket, "SOL_BLUETOOTH", 274),
                           getattr(socket, "BT_SECURITY", 4), bytes((2, 0)))
        try:
            channel.connect((address, psm))
        except OSError as error:
            # Linux maps Bluetooth PIN or Key Missing (HCI 0x06) to EBADE.
            if error.errno == getattr(errno, "EBADE", 52):
                raise PairingKeyMissing() from error
            raise
        # Linux L2CAP_OPTIONS starts with outgoing and incoming MTUs.
        mtu = struct.unpack_from("=H", channel.getsockopt(getattr(socket, "SOL_L2CAP", 6), 1, 12))[0]
        channel.settimeout(1)
        return channel, mtu
    except BaseException:
        channel.close()
        raise


class Session:
    def __init__(self, channel, mtu, destination, on_request, on_close):
        if mtu < media.HEADER:
            channel.close()
            raise RuntimeError(f"The Nano's packet size is too small ({mtu} bytes).")
        self.channel, self.mtu, self.destination = channel, mtu, destination
        self.on_request, self.on_close = on_request, on_close
        self.closed = threading.Event()
        self.condition = threading.Condition()
        self.credit = self.token = 0
        self.paused = self.artwork = self.awaiting_video = False
        self.audio_revision = self.video_revision = 0
        self.video_progress = False
        self.error = None
        self.audio_epoch = None
        self.audio_verified = False
        self.results = {}

    def start(self):
        threading.Thread(target=self.receive, daemon=True).start()

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        with self.condition:
            self.condition.notify_all()
        try:
            self.channel.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.channel.close()

    def receive(self):
        try:
            while not self.closed.is_set():
                try:
                    data = self.channel.recv(1024)
                except socket.timeout:
                    continue
                if not data:
                    break
                with self.condition:
                    request = self.receive_packet(data)
                    self.condition.notify_all()
                if self.error:
                    break
                if request:
                    self.on_request(self, data)
        except OSError:
            pass
        finally:
            self.close()
            self.on_close(self)

    def receive_packet(self, data):
        if data == b"NYH1":
            self.artwork = True
        if self.destination == "youtubeAudio":
            if data in (b"NYL3", b"NYL4", b"NYL5", b"NYL6", b"NYL7"):
                self.audio_revision = max(self.audio_revision, data[3] - ord("0"))
                self.artwork = True
            elif len(data) >= 9 and data.startswith(b"NAR1"):
                token, = struct.unpack_from("<I", data, 4)
                if self.token and token == self.token:
                    if data[8] == 0:
                        self.audio_verified = True
                    elif data[8] == 1:
                        detail = data[9:].decode("utf-8", "replace")
                        self.error = f"Nano: {detail}" if detail else "The Nano rejected the audio stream."
            elif len(data) == 8 and data[:4] in (b"NYZ2", b"NYZ3"):
                token, = struct.unpack_from("<I", data, 4)
                if self.token and token == self.token:
                    self.paused = data[:4] == b"NYZ2"
                    if not self.paused:
                        self.audio_epoch = None
        else:
            if data in (b"NVQ2", b"NVQ3", b"NVQ4", b"NVQ5", b"NVQ6"):
                self.video_revision = max(self.video_revision, data[3] - ord("0"))
                self.artwork = True
            elif data == b"NVP1":
                self.video_progress = self.artwork = True
            elif data in (b"NYZ1", b"NYZ0"):
                self.paused = data == b"NYZ1"
            elif data == b"NYV1":
                self.awaiting_video = False
            elif data.startswith(b"NVF1") and len(data) > 4:
                detail = data[4:].split(b"\0", 1)[0].decode("utf-8", "replace")
                self.error = f"Nano: {detail}"
            elif len(data) == 8 and data.startswith(b"NYG1"):
                self.credit = max(self.credit, struct.unpack_from("<I", data, 4)[0])
        request = (data.startswith(b"NYQ1") and len(data) > 4 or
                   data.startswith(b"NYP1") and len(data) >= 15 and not any(data[15:]))
        if request and self.destination == "youtubeVideo":
            self.artwork = True
        return request

    def wait_for(self, supported, check, message, timeout=2.5):
        deadline = time.monotonic() + timeout
        with self.condition:
            while not supported():
                check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(message)
                self.condition.wait(min(.1, remaining))

    def begin_flow(self, token=0):
        with self.condition:
            self.credit = 0
            self.token = token
            self.paused = False
            self.audio_epoch = None
            self.audio_verified = False
            self.awaiting_video = self.destination == "youtubeVideo"

    def write(self, data, check, video_offset=None, audio_rate=0, prefix=b""):
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            check()
            capacity = self.mtu - len(prefix)
            end = min(len(view), sent + (min(capacity, 336) if audio_rate else capacity))
            if video_offset is not None:
                with self.condition:
                    deadline = time.monotonic() + 20
                    while self.paused or self.credit <= video_offset + sent:
                        check()
                        if self.paused:
                            deadline = time.monotonic() + 20
                        elif time.monotonic() >= deadline:
                            raise TimeoutError("The Nano stopped reporting video buffer space. Reconnect to retry.")
                        self.condition.wait(.1)
                    end = min(end, self.credit - video_offset)
            if audio_rate:
                with self.condition:
                    while True:
                        check()
                        if self.paused:
                            self.condition.wait(.1)
                            continue
                        now = time.monotonic()
                        if self.audio_epoch is None:
                            self.audio_epoch = now
                        duration = (end - sent) / audio_rate
                        deadline = max(self.audio_epoch + duration, now - max(0, .02 - duration))
                        if deadline <= now:
                            self.audio_epoch = deadline
                            break
                        self.condition.wait(deadline - now)
            packet = prefix + view[sent:end].tobytes() if prefix else view[sent:end]
            send_deadline = time.monotonic() + BLUETOOTH_SEND_TIMEOUT
            while True:
                try:
                    size = self.channel.send(packet)
                    break
                except socket.timeout as error:
                    check()
                    if time.monotonic() >= send_deadline:
                        raise TimeoutError("Timed out sending to the Nano over Bluetooth.") from error
            if size != len(packet):
                raise OSError("Incomplete Bluetooth packet.")
            sent = end


class Sender:
    def __init__(self, connection_closed=None):
        self.lock = threading.RLock()
        self.session = None
        self.job = None
        self.job_error = None
        self.status = "Connect to the Nano."
        self.progress = None
        self.options = {mode: options.copy() for mode, options in DEFAULTS.items()}
        self.connection_closed = connection_closed

    def snapshot(self):
        with self.lock:
            session = self.session
            status = self.status
            if session and session.destination == "youtubeVideo" and not session.awaiting_video and status.startswith("Video sent."):
                status = "Ready for another video."
            return {"connected": session.destination if session else None, "busy": self.job is not None,
                    "status": status, "progress": self.progress, "jobError": self.job_error,
                    "waitingForVideo": bool(session and session.awaiting_video),
                    "audioRevision": session.audio_revision if session else 0,
                    "videoRevision": session.video_revision if session else 0,
                    "settings": {mode: options.copy() for mode, options in self.options.items()}}

    def connect(self, address, destination, local_address, *, automatic=False):
        if not automatic:
            self.disconnect()
        timeout = 4 if automatic else 12
        channel, mtu = connect_socket(address, DESTINATIONS[destination], local_address, timeout)
        session = Session(channel, mtu, destination, self.request, self.closed)
        with self.lock:
            if automatic and (self.session or self.job):
                session.close()
                return False
            self.session = session
            app = "YT Music" if destination == "youtubeAudio" else "YT Video"
            self.status = f"Connected to {app} on {address}."
            self.progress = None
        session.start()
        return True

    def disconnect(self):
        with self.lock:
            if self.job:
                self.job.set()
            if self.session:
                self.session.close()
                self.session = None
            self.status = "Disconnected."

    def closed(self, session):
        with self.lock:
            if self.session is session:
                self.session = None
                if self.job:
                    self.job.set()
                if session.error:
                    self.status = session.error
                elif self.status.startswith(("Connected", "Sending", "Playing", "Preparing", "Searching")):
                    self.status = "The Nano closed the connection."
        if self.connection_closed:
            self.connection_closed()

    def require(self, destination):
        with self.lock:
            if not self.session or self.session.destination != destination or self.session.closed.is_set():
                raise RuntimeError("Connect to the matching Nano app first.")
            if self.session.awaiting_video:
                raise RuntimeError("Close playback on the Nano before sending another video.")
            return self.session

    def launch(self, action, session=None, quiet=False, cleanup=None):
        with self.lock:
            if self.job or (session and (session is not self.session or session.closed.is_set() or session.awaiting_video)):
                if quiet:
                    return False
                raise RuntimeError("The sender is busy or disconnected.")
            cancelled = self.job = threading.Event()
            self.job_error = None
            self.progress = None
            options = self.options[session.destination].copy() if session else {}

        def check():
            if session and session.error:
                raise RuntimeError(session.error)
            if cancelled.is_set() or (session and session.closed.is_set()):
                raise InterruptedError("Cancelled.")

        def run():
            try:
                with tempfile.TemporaryDirectory(prefix="nano-sender-") as folder:
                    action(Path(folder), options, check)
            except Exception as error:
                if isinstance(error, InterruptedError):
                    self.status = "Cancelled."
                else:
                    phase = self.status
                    self.job_error = str(error) or type(error).__name__
                    self.status = "Media job failed."
                    logging.warning("Nano media job failed: %s (status: %s)", self.job_error, phase)
            finally:
                try:
                    if cleanup:
                        cleanup()
                finally:
                    with self.lock:
                        self.job = None

        threading.Thread(target=run, daemon=True).start()
        return True

    def request(self, session, data):
        raw = data[4:15] if data[:4] == b"NYP1" else data[4:].split(b"\0", 1)[0]
        text = raw.decode("utf-8", "replace").strip()
        if data[:4] == b"NYQ1":
            self.launch(lambda folder, opts, check: self.search(session, text, check), session, quiet=True)
        else:
            self.launch(lambda folder, opts, check: self.select(session, text, folder, opts, check),
                        session, quiet=True)

    def send_status(self, session, text, check, accepted=False):
        session.write(media.record(b"NYA1" if accepted else b"NYS1",
                                  text.encode("ascii", "replace")[:55]), check)

    def search(self, session, query, check):
        if not query:
            self.send_status(session, "Invalid search text.", check)
            return
        video = session.destination == "youtubeVideo"
        self.status = "Searching YouTube…" if video else "Searching YouTube Music…"
        self.send_status(session, self.status, check, True)
        try:
            results = media.search(query, video, check)
            session.results = {item["id"]: item for item in results}
            with_art = session.artwork
            response = bytearray(media.record(b"NYL1", bytes((len(results), with_art))))
            for item in results:
                key = item["id"].encode()
                if item["author"]:
                    response += media.record(b"NYT1", key + media.utf8(item["author"], 44))
                response += media.record(b"NYR1", key + media.utf8(item["title"], 44))
            if with_art:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    for i, pixels in enumerate(pool.map(lambda item: media.artwork(item, 96, video), results)):
                        check()
                        if pixels:
                            response += media.record(b"NYI1", bytes([i])) + pixels
                response += media.record(b"NYE1")
        except Exception as error:
            check()
            self.send_status(session, str(error), check)
            raise
        try:
            session.write(response, check)
        except Exception:
            session.close()
            raise
        self.status = "Choose a video on the Nano." if video else "Choose a song on the Nano."

    def select(self, session, key, folder, opts, check):
        item = session.results.get(key)
        if not item:
            self.send_status(session, "Search again to refresh the results.", check)
            return
        video = session.destination == "youtubeVideo"
        host = "www.youtube.com" if video else "music.youtube.com"
        self.send_youtube(session, f"https://{host}/watch?v={key}", folder, opts, check, item)

    def preparation(self, session, check, message, stage=None):
        next_update = 0.0

        def heartbeat(force=False):
            nonlocal next_update
            check()
            now = time.monotonic()
            if force or now >= next_update:
                next_update = now + 1
                if stage and session.video_progress:
                    percent = 255 if self.progress is None else min(100, self.progress)
                    session.write(media.record(b"NVT1", bytes((stage, percent))), check)
                else:
                    self.send_status(session, message, check, accepted=True)
        return heartbeat

    def send_youtube(self, session, url, folder, opts, check, item=None):
        video = session.destination == "youtubeVideo"
        streaming = opts["playback"] == "live" and opts["download"]
        try:
            if video:
                self.send_video(session, None, folder, opts, check, url)
            elif streaming:
                self.send_progressive_audio(session, url, folder, opts, check)
            else:
                self.status = "Downloading YouTube audio…"
                preparing = self.preparation(session, check, self.status)
                preparing()
                source = media.download(url, folder, False, preparing)
                cover = media.cover_pixels(folder)
                if cover is None and item:
                    cover = media.artwork(item, 160, False)
                self.send_audio(session, source, folder, opts, check, cover=cover)
        except Exception as error:
            if not session.closed.is_set():
                try:
                    self.send_status(session, str(error), check)
                except OSError:
                    # A second Bluetooth failure must not hide the media error.
                    pass
            raise

    def send_audio(self, session, source, folder, opts, check, name=None, cover=None):
        self.status = "Preparing audio…"
        live = opts["playback"] == "live"
        rate, channels = media.AUDIO[opts["audio"]]
        if live:
            revision = 4 if rate == 44100 else 3
            session.wait_for(lambda: session.audio_revision >= revision, check,
                             "Update YT Music on the Nano to use While Sending.")
        adpcm = session.audio_revision >= 5 or (live and rate == 44100 and session.audio_revision >= 4)
        wav = folder / "audio.wav"
        size = media.audio_file(source, wav, opts["audio"], adpcm, check)
        value = 2166136261
        with wav.open("rb") as file:
            for chunk in iter(lambda: file.read(65536), b""):
                check()
                value = media.checksum(chunk, value)
        token = secrets.randbelow(media.UINT32_MAX) + 1 if live else 0
        session.begin_flow(token)
        suffix = {"live": ".wav", "after": ".nsa", "test": ".ntx"}[opts["playback"]]
        header = media.audio_header(size, value, name or source.name, suffix, token, bool(cover))
        speed = audio_speed(rate, channels, adpcm) if live else 40 * 1024
        sent = 0
        self.status = "Sending audio…"
        try:
            session.write(header, check)
            if cover:
                session.write(cover, check)
            with wav.open("rb") as file:
                for chunk in iter(lambda: file.read(8192), b""):
                    session.write(chunk, check, audio_rate=speed)
                    sent += len(chunk)
                    self.progress = sent * 100 // size
        except Exception:
            session.close()
            raise
        self.status = {
            "live": "Audio sent. Leave YT Music open to finish playback.",
            "after": "Audio sent. The Nano will verify it before playing.",
            "test": "Audio sent. Check the Nano for the receive test result.",
        }[opts["playback"]]

    def send_progressive_audio(self, session, url, folder, opts, check):
        session.wait_for(lambda: session.audio_revision >= 6, check,
                         "Update YT Music on the Nano to use Download during transfer.")
        framed = session.audio_revision >= 7
        tag = b"NYU2" if framed else b"NYU1"
        rate, channels = media.AUDIO[opts["audio"]]
        token = secrets.randbelow(media.UINT32_MAX) + 1
        speed = audio_speed(rate, channels, True)
        self.status = "Opening YouTube audio stream…"
        preparing = self.preparation(session, check, "Preparing YouTube audio…")
        started = False
        sent, checksum = 0, 2166136261

        def check_preparation():
            if started:
                check()
            else:
                preparing()

        try:
            with media.download_stream(url, folder, False, check_preparation) as (pipe, validate):
                blocks = media.audio_blocks(pipe, folder / "audio.wav", opts["audio"], check_preparation)
                with closing(blocks):
                    for chunk in blocks:
                        if not started:
                            started = True
                            session.begin_flow(token)
                            session.write(media.record(tag, media.words(rate, channels, token)), check)
                            self.status = "Downloading, converting, and sending audio…"
                        if sent + len(chunk) > media.UINT32_MAX:
                            raise ValueError("The audio exceeds the Nano's 4 GiB transfer range.")
                        if framed:
                            session.write(media.words(len(chunk)) + chunk, check, audio_rate=speed)
                        else:
                            session.write(chunk, check, audio_rate=speed, prefix=b"\0")
                        checksum = media.checksum(chunk, checksum)
                        sent += len(chunk)
                validate()
                if framed:
                    session.write(media.words(0, sent, checksum), check)
                    self.status = "Waiting for the Nano to verify the audio…"
                    session.wait_for(lambda: session.audio_verified, check,
                                     "The Nano did not confirm the audio transfer.", timeout=6)
                else:
                    session.write(b"\1" + media.words(sent, checksum), check)
        except Exception:
            if started:
                session.close()
            raise
        self.progress = 100
        self.status = "Audio sent. Leave YT Music open to finish playback."

    def send_video(self, session, source, folder, opts, check, url=None, *, fallback=False):
        live = opts["playback"] == "live"
        quality, fps = opts["quality"], opts["fps"]
        minimum = 2 if quality == "native" else 0 if quality == "double" else 3
        if fps == 22:
            minimum = max(4, minimum)
        if minimum:
            session.wait_for(lambda: session.video_revision >= minimum, check,
                             "Update YT Video on the Nano or choose another video quality or frame rate.")
        if quality == "native" and fps != 22 and session.video_revision < 6:
            fps = 30
        streamed = url is not None and live and opts["download"]
        convert = opts["convert"] or streamed
        started = False
        if url and not streamed:
            self.status = ("Stream unavailable. Downloading the full video…" if fallback
                           else "Downloading the full YouTube video…")
            downloading = self.preparation(session, check, self.status, 1)
            downloading()
            source = media.download(url, folder, True, downloading)

        preparing = self.preparation(session, check, "Downloading YouTube video…" if streamed
                                     else "Preparing H.264 video…", 1 if streamed else 2)
        self.status = "Opening YouTube video stream…" if streamed else "Preparing H.264 video…"
        preparing()
        estimated = media.video_frame_estimate(source, fps, preparing) if source else None
        startup_deadline = time.monotonic() + VIDEO_STREAM_START_TIMEOUT if streamed else None

        def check_preparation():
            if started:
                check()
            else:
                preparing()
                # The downloader can keep its pipe open even when FFmpeg cannot
                # produce a playable group from it.
                if startup_deadline is not None and time.monotonic() >= startup_deadline:
                    raise VideoStreamStartupTimeout("No playable video arrived from YouTube in time.")

        def records(encoding_folder, stream=None, validate=None):
            track = media.video_records(source, encoding_folder, quality, fps, opts["audio"], live,
                                        check_preparation, adpcm=session.video_revision >= 5, stream=stream)
            with closing(track):
                for packet in track:
                    # The downloader must succeed before the receiver sees the end record.
                    if validate and len(packet) == 12 and packet[:4] == b"\0" * 4:
                        validate()
                    yield packet

        def send(packets, size=0, spool_size=None):
            nonlocal started
            sent = frames = 0
            for packet in packets:
                if not started:
                    self.preparation(session, check, "Starting video transfer…", 3)()
                    session.begin_flow()
                    started = True
                    session.write(media.record(b"NCL3", media.words(size, estimated or 0)), check)
                    self.status = ("Downloading, converting, and sending H.264…" if streamed else
                                   "Converting and sending H.264…" if convert else "Sending H.264…")
                if sent + len(packet) > media.UINT32_MAX:
                    raise ValueError("The video exceeds the Nano's 4 GiB transfer range.")
                session.write(packet, check, video_offset=sent)
                sent += len(packet)
                frames += video_group_frames(packet)
                if spool_size:
                    self.progress = min(99, sent * 100 // spool_size)
                elif estimated:
                    self.progress = min(99, frames * 100 // estimated)

        try:
            if streamed:
                stream_folder = folder / "stream"
                stream_folder.mkdir()
                with media.download_stream(url, stream_folder, True, check_preparation) as (pipe, validate):
                    with closing(records(stream_folder, pipe, validate)) as packets:
                        send(packets)
            elif convert:
                with closing(records(folder)) as packets:
                    send(packets)
            else:
                spool = folder / "video.groups"
                size = frames = 0
                with closing(records(folder)) as packets, spool.open("wb") as file:
                    for packet in packets:
                        size += len(packet)
                        if size > media.UINT32_MAX:
                            raise ValueError("The video exceeds the Nano's 4 GiB transfer range.")
                        file.write(media.words(len(packet)))
                        file.write(packet)
                        frames += video_group_frames(packet)
                        if estimated:
                            self.progress = min(99, frames * 100 // estimated)
                self.progress = 0
                with closing(read_spool(spool)) as packets:
                    send(packets, size, size)
        except Exception as error:
            if started:
                session.close()
            elif streamed and isinstance(error, (media.YouTubeFormatUnavailable, VideoStreamStartupTimeout)):
                logging.warning("YouTube video streaming startup failed: %s. Trying a full download.", error)
                file_options = {**opts, "download": False, "convert": True}
                return self.send_video(session, None, folder, file_options, check, url, fallback=True)
            raise
        check()
        self.progress = 100
        self.status = "Video sent. Leave YT Video open to finish playback."


def audio_speed(rate, channels, adpcm):
    samples = media.samples_per_block(channels)
    encoded = (rate * media.IMA_BLOCK + samples - 1) // samples if adpcm else rate * channels
    cap = (48 if encoded > 40 * 1024 else 40) * 1024
    return min(cap, encoded * 1.05)


def video_group_frames(packet):
    return struct.unpack_from("<I", packet, 4)[0] if len(packet) > 12 and packet[:3] not in (b"NVL", b"NVD") else 0


def read_spool(path):
    with path.open("rb") as file:
        while length := file.read(4):
            size = struct.unpack("<I", length)[0]
            packet = file.read(size)
            if len(packet) != size:
                raise OSError("Incomplete prepared video file.")
            yield packet
