"""Classic L2CAP sessions and media transfer jobs."""
import concurrent.futures
from contextlib import closing
import re
import secrets
import socket
import struct
import tempfile
import threading
import time
from pathlib import Path

import media

DESTINATIONS = {"inbox": 0x1003, "stream": 0x1005, "video": 0x1007,
                "youtubeAudio": 0x1009, "youtubeVideo": 0x100B}
VIDEO = {"video", "youtubeVideo"}
YOUTUBE = {"youtubeAudio", "youtubeVideo"}
ADDRESS = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}")


def connect_socket(address, psm, local_address):
    if not hasattr(socket, "BTPROTO_L2CAP"):
        raise RuntimeError("Classic L2CAP requires Linux with Bluetooth support.")
    channel = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    try:
        channel.settimeout(12)
        if local_address:
            channel.bind((local_address, 0))
        # Linux BT_SECURITY, medium: authenticate with the paired Nano.
        channel.setsockopt(getattr(socket, "SOL_BLUETOOTH", 274),
                           getattr(socket, "BT_SECURITY", 4), bytes((2, 0)))
        channel.connect((address, psm))
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
        self.last_credit = time.monotonic()
        self.paused = self.artwork = self.video_ready = False
        self.results = {}
        self.finished = False

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
                request = False
                with self.condition:
                    if data == b"NYH1":
                        self.artwork = True
                    elif data == b"NYZ1":
                        self.paused = True
                    elif data == b"NYZ0":
                        self.paused = False
                        self.last_credit = time.monotonic()
                    elif data == b"NYV1":
                        self.video_ready = True
                    elif data[:4] == b"NYG1" and len(data) == 8 and self.destination in VIDEO:
                        self.update_credit(struct.unpack_from("<I", data, 4)[0])
                    elif data[:4] == b"NYF1" and len(data) == 12 and self.destination == "youtubeAudio":
                        token, limit = struct.unpack_from("<II", data, 4)
                        if token == self.token and token:
                            self.update_credit(limit)
                    elif self.destination in YOUTUBE and (
                            data[:4] == b"NYQ1" and 4 < len(data) <= 48
                            or data[:4] == b"NYP1" and len(data) == 15):
                        if self.destination == "youtubeVideo":
                            self.artwork = True
                        request = True
                    self.condition.notify_all()
                if request:
                    self.on_request(self, data)
        except OSError:
            pass
        finally:
            self.close()
            self.on_close(self)

    def update_credit(self, limit):
        if limit >= self.credit:
            self.credit = limit
            self.last_credit = time.monotonic()

    def begin_flow(self, token=0):
        with self.condition:
            self.credit = 0
            self.token = token
            self.paused = self.video_ready = False
            self.last_credit = time.monotonic()

    def write(self, data, check, offset=None):
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            check()
            end = min(len(view), sent + self.mtu)
            if offset is not None:
                with self.condition:
                    while self.paused or self.credit <= offset + sent:
                        check()
                        if not self.paused and time.monotonic() - self.last_credit > 20:
                            raise TimeoutError("The Nano stopped reporting buffer space. Check its app version and reconnect.")
                        self.condition.wait(.1)
                    end = min(end, self.credit - offset)
            packet = view[sent:end]
            if self.channel.send(packet) != len(packet):
                raise OSError("Incomplete Bluetooth packet.")
            sent = end

    def finish_video(self, check):
        with self.condition:
            while not self.video_ready:
                check()
                self.condition.wait(.25)


class Sender:
    def __init__(self):
        self.lock = threading.RLock()
        self.session = None
        self.job = None
        self.status = "Connect to the Nano."
        self.progress = None
        self.options = {"audio": "low", "quality": "balanced", "fps": 5,
                        "playback": "after", "convert": False}

    def snapshot(self):
        with self.lock:
            session = self.session
            return {"connected": session.destination if session else None, "busy": self.job is not None,
                    "status": self.status, "progress": self.progress,
                    "reconnect": bool(session and session.finished),
                    "artwork": bool(session and session.artwork)}

    def connect(self, address, destination, local_address):
        self.disconnect()
        channel, mtu = connect_socket(address, DESTINATIONS[destination], local_address)
        session = Session(channel, mtu, destination, self.request, self.closed)
        with self.lock:
            self.session = session
            self.status = f"Connected to {address}."
        session.start()

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
                if self.status.startswith(("Connected", "Sending", "Playing", "Preparing", "Searching")):
                    self.status = "The Nano closed the connection."

    def require(self, destination):
        with self.lock:
            if not self.session or self.session.destination != destination or self.session.closed.is_set():
                raise RuntimeError("Connect to the matching Nano app first.")
            if self.session.finished:
                raise RuntimeError("Reconnect to the Nano before sending another file.")
            return self.session

    def launch(self, action, session=None, quiet=False, cleanup=None):
        with self.lock:
            if self.job or (session and (session is not self.session or session.closed.is_set())):
                if quiet:
                    return False
                raise RuntimeError("The sender is busy or disconnected.")
            cancelled = self.job = threading.Event()
            self.progress = None
            options = self.options.copy()

        def check():
            if cancelled.is_set() or (session and session.closed.is_set()):
                raise InterruptedError("Cancelled.")

        def run():
            try:
                with tempfile.TemporaryDirectory(prefix="nano-sender-") as folder:
                    action(Path(folder), options, check)
            except Exception as error:
                self.status = str(error)
            finally:
                if cleanup:
                    cleanup()
                with self.lock:
                    self.job = None

        threading.Thread(target=run, daemon=True).start()
        return True

    def request(self, session, data):
        text = data[4:].decode("utf-8", "replace").strip()
        if data[:4] == b"NYQ1":
            self.launch(lambda folder, opts, check: self.search(session, text, check), session, quiet=True)
        else:
            self.launch(lambda folder, opts, check: self.select(session, text, folder, opts, check),
                        session, quiet=True)

    def send_status(self, session, text, check, accepted=False):
        session.write(media.record(b"NYA1" if accepted else b"NYS1",
                                  text.encode("ascii", "replace")[:55]), check)

    def search(self, session, query, check):
        video = session.destination == "youtubeVideo"
        self.status = "Searching YouTube…" if video else "Searching YouTube Music…"
        self.send_status(session, self.status, check, True)
        try:
            results = media.search(query, video, check)
            session.results = {item["id"]: item for item in results}
            with_art = bool(results and session.artwork)
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
        self.status = "Choose a result on the Nano." if results else "No results found."

    def select(self, session, key, folder, opts, check):
        item = session.results.get(key)
        if not item:
            self.send_status(session, "Search again to refresh the results.", check)
            return
        video = session.destination == "youtubeVideo"
        host = "www.youtube.com" if video else "music.youtube.com"
        self.send_youtube(session, f"https://{host}/watch?v={key}", folder, opts, check, item)

    def send_youtube(self, session, url, folder, opts, check, item=None):
        video = session.destination in VIDEO
        self.status = "Downloading YouTube video…" if video else "Downloading YouTube audio…"
        if session.destination in YOUTUBE:
            self.send_status(session, self.status, check, True)
        try:
            source = media.download(url, folder, video, check)
            if video:
                self.send_video(session, source, folder, opts, check)
            else:
                cover = media.artwork(item, 160, False) if item else None
                self.send_audio(session, source, folder, opts, check, cover=cover)
        except Exception as error:
            if session.destination in YOUTUBE and not session.closed.is_set():
                self.send_status(session, str(error), check)
            raise

    def send_audio(self, session, source, folder, opts, check, name=None, cover=None):
        self.status = "Preparing audio…"
        mulaw = session.destination == "youtubeAudio"
        live = mulaw or (session.destination == "stream" and opts["playback"] == "live")
        rate, channels = media.AUDIO[opts["audio"]]
        wav = folder / "audio.wav"
        size = media.audio_file(source, wav, opts["audio"], mulaw, check)
        value = 2166136261
        with wav.open("rb") as file:
            for chunk in iter(lambda: file.read(65536), b""):
                check()
                value = media.checksum(chunk, value)
        token = secrets.randbelow(media.UINT32_MAX) + 1 if mulaw else 0
        session.begin_flow(token)
        header = media.audio_header(size, value, name or source.name,
                                    ".nsa" if session.destination == "stream" and not live else ".wav",
                                    token, bool(cover))
        self.status = "Sending audio…"
        try:
            session.write(header, check)
            if cover:
                session.write(cover, check)
            sent = 0
            started = time.monotonic()
            with wav.open("rb") as file:
                for chunk in iter(lambda: file.read(8192), b""):
                    if live and not mulaw:
                        wait_until = started + (sent + len(chunk)) / (rate * channels * 2) - 3
                        while time.monotonic() < wait_until:
                            check()
                            time.sleep(.025)
                    session.write(chunk, check, sent if mulaw else None)
                    sent += len(chunk)
                    self.progress = sent * 100 // size
        except Exception:
            session.close()
            raise
        session.finished = not mulaw
        self.status = "Audio sent."

    def send_video(self, session, source, folder, opts, check):
        live = opts["playback"] == "live"
        self.status = "Preparing H.264 video…"
        estimated = media.video_frame_estimate(source, opts["fps"], check)
        video = media.video_records(source, folder, opts["quality"], opts["fps"], opts["audio"], live, check)
        spool = folder / "video.groups"

        def advance(frames, packet):
            if packet[:4] not in (b"NVL2", b"NVD2"):
                length, count = struct.unpack_from("<II", packet)
                if length:
                    frames += count
            if estimated:
                self.progress = min(99, frames * 100 // estimated)
            return frames

        if not opts["convert"]:
            frames = 0
            with closing(video), spool.open("wb") as file:
                for packet in video:
                    file.write(media.words(len(packet)))
                    file.write(packet)
                    frames = advance(frames, packet)
            video = read_spool(spool)

        def send():
            session.begin_flow()
            session.write(media.record(b"NCL3"), check)
            sent = frames = 0
            total = spool.stat().st_size if not opts["convert"] else None
            self.progress = 0 if total or estimated else None
            with closing(video):
                for packet in video:
                    if sent + len(packet) > media.UINT32_MAX:
                        raise ValueError("The video exceeds the Nano's 4 GiB transfer range.")
                    session.write(packet, check, sent)
                    sent += len(packet)
                    if total:
                        self.progress = min(99, sent * 100 // total)
                    else:
                        frames = advance(frames, packet)

        self.status = "Converting and sending H.264…" if opts["convert"] else "Sending H.264…"
        try:
            send()
        except Exception:
            video.close()
            session.close()
            raise
        self.progress = 100
        if session.destination == "youtubeVideo":
            self.status = "Video sent. Close playback on the Nano to send another."
            session.finish_video(check)
            self.status = "Ready for another video."
        else:
            session.finished = True
            self.status = "Video sent."


def read_spool(path):
    with path.open("rb") as file:
        while length := file.read(4):
            size = struct.unpack("<I", length)[0]
            packet = file.read(size)
            if len(packet) != size:
                raise OSError("Incomplete prepared video file.")
            yield packet
