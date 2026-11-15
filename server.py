"""Run Nano Sender on Raspberry Pi OS and operate it from a browser."""
import argparse
import asyncio
import base64
import errno
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

import media
from sender import ADDRESS, DEFAULTS, DESTINATIONS, Sender

PAGE = Path(__file__).with_name("index.html")
MAX_UPLOAD = media.UINT32_MAX
AUTO_RETRY_MIN = 3
AUTO_RETRY_MAX = 30
ADAPTER_UNREACHABLE = {errno.ETIMEDOUT, errno.EHOSTDOWN, errno.EHOSTUNREACH,
                       errno.ENETDOWN, errno.ENETUNREACH, errno.ENODEV}


def address(value):
    value = value.upper().strip().replace("-", ":") if isinstance(value, str) else ""
    if not ADDRESS.fullmatch(value) or value in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF", "02:00:00:00:00:00"):
        raise ValueError("Enter a Bluetooth address such as AA:BB:CC:DD:EE:FF.")
    return value


class App:
    def __init__(self, adapter, auto_connect=True):
        self.auto_enabled = adapter is not None and auto_connect
        self.auto_wake = threading.Event()
        self.auto_stop = threading.Event()
        self.auto_after = 0
        self.auto_thread = None
        self.sender = Sender(connection_closed=self.auto_wake.set)
        self.loop = asyncio.new_event_loop()
        self.settings_file = Path.home() / ".config" / "nano-sender" / "device.json"
        try:
            self.saved = address(json.loads(self.settings_file.read_text())["address"])
        except (OSError, KeyError, ValueError, TypeError, AttributeError):
            self.saved = ""
        self.connection_lock = threading.Lock()
        self.radio = None
        self.radio_error = None
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        if adapter is not None:
            self.call(self.start_radio(adapter))
            if self.auto_enabled:
                self.auto_thread = threading.Thread(target=self.auto_connect, daemon=True)
                self.auto_thread.start()

    async def start_radio(self, adapter):
        from bluez import Radio

        self.radio = Radio(adapter)
        await self.radio.start()
        await self.advertising(False)

    def close(self):
        self.auto_stop.set()
        self.auto_wake.set()
        if self.auto_thread:
            self.auto_thread.join()
        self.sender.disconnect()
        self.loop.call_soon_threadsafe(self.loop.stop)

    async def paired_for_auto_connect(self, peer):
        if self.radio.scan_stop or self.radio.pair_task or self.radio.tx.notifying:
            return False
        return any(device["address"] == peer and device["paired"] for device in await self.radio.devices())

    def auto_connect(self):
        destinations = list(DESTINATIONS)
        delay = 0
        retry = AUTO_RETRY_MIN
        while not self.auto_stop.is_set():
            self.auto_wake.wait(delay)
            self.auto_wake.clear()
            if self.auto_stop.is_set():
                break
            delay = None
            peer = self.saved
            with self.sender.lock:
                if not peer or self.sender.session:
                    continue
                if self.sender.job:
                    delay = 2
                    continue

            delay = max(0, self.auto_after - time.monotonic())
            if delay:
                continue
            if not self.connection_lock.acquire(blocking=False):
                delay = 1
                continue
            try:
                # Manual pairing or disconnect may have finished while we acquired the lock.
                delay = max(0, self.auto_after - time.monotonic())
                if delay:
                    continue
                if peer != self.saved:
                    continue
                with self.sender.lock:
                    if self.sender.session or self.sender.job:
                        delay = 2 if self.sender.job else None
                        continue
                delay = retry
                retry = min(AUTO_RETRY_MAX, retry * 2)
                if not self.call(self.paired_for_auto_connect(peer), timeout=3):
                    continue
                for destination in destinations:
                    if self.auto_stop.is_set():
                        return
                    try:
                        if self.sender.connect(peer, destination, self.radio.address, automatic=True):
                            delay, retry = None, AUTO_RETRY_MIN
                            destinations = [destination] + [other for other in destinations if other != destination]
                        break
                    except OSError as error:
                        if isinstance(error, TimeoutError) or error.errno in ADAPTER_UNREACHABLE:
                            # Let the other app go first next time if this probe could not reach the Nano.
                            destinations.reverse()
                            break
            except Exception as error:
                self.sender.status = f"Automatic connection will retry. {error}"
            finally:
                self.connection_lock.release()

    def call(self, coroutine, timeout=70):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    async def advertising(self, ble):
        try:
            await self.radio.advertise(ble)
        except Exception as error:
            prefix = "BLE advertising unavailable." if ble else "Use Mac Address on the Nano and enter the Pi address."
            self.radio_error = f"{prefix} {error}"
        else:
            self.radio_error = None

    async def radio_state(self):
        if self.radio is None:
            return {}
        return {
            "pi": self.radio.address, "pairCode": self.radio.prompt, "radioError": self.radio_error,
            "scanning": self.radio.scan_stop is not None, "pairing": self.radio.pair_task is not None,
            "bleReady": self.radio.tx.notifying, "bleReadReady": bool(self.radio.audio_read.read_at),
            "receivedMessage": self.radio.received_message, "bleLimit": self.radio.notification_limit,
            "devices": [{key: device[key] for key in ("address", "name", "paired")}
                        for device in await self.radio.devices()],
        }

    def remember(self, peer):
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=self.settings_file.parent, delete=False) as file:
            json.dump({"address": peer}, file)
            path = Path(file.name)
        try:
            path.replace(self.settings_file)
        finally:
            path.unlink(missing_ok=True)
        self.saved = peer
        self.auto_after = 0
        self.auto_wake.set()

    def options(self, values):
        destination = values.get("destination")
        if destination not in DEFAULTS:
            raise ValueError("Choose YT Music or YT Video.")
        defaults = DEFAULTS[destination]
        options = {key: values.get(key, value) for key, value in defaults.items()}
        playback = ("after", "live", "test") if destination == "youtubeAudio" else ("after", "live")
        if (options["audio"] not in media.AUDIO or options["playback"] not in playback
                or not isinstance(options["download"], bool)):
            raise ValueError("Choose a supported audio quality and playback mode.")
        if destination == "youtubeVideo" and (options["quality"] not in media.VIDEO_QUALITY
                or options["fps"] not in media.FRAME_RATES or type(options["fps"]) is not int
                or not isinstance(options["convert"], bool)):
            raise ValueError("Choose a supported video quality and frame rate.")
        with self.sender.lock:
            self.sender.options[destination] = options

    def state(self):
        return {**self.sender.snapshot(), **self.call(self.radio_state(), 5), "saved": self.saved,
                "autoConnect": self.auto_enabled}

    def command(self, action, payload):
        destination = payload.get("destination")
        if action == "settings":
            self.options(payload)
        elif action == "mode":
            if destination != "ble" and destination not in DESTINATIONS:
                raise ValueError("Choose a sender tab.")
            self.call(self.advertising(destination == "ble"))
        elif action == "scan":
            self.call(self.radio.scan())
        elif action == "stopScan":
            self.call(self.radio.stop_scan())
        elif action in ("pair", "connect"):
            peer = address(payload.get("address", ""))
            if destination not in DESTINATIONS:
                raise ValueError("Choose YT Music or YT Video.")
            if not self.connection_lock.acquire(timeout=12):
                raise RuntimeError("A Bluetooth connection is already being set up.")
            try:
                if action == "connect":
                    self.options(payload)
                    self.sender.status = "Pairing or connecting to the Nano…"
                self.call(self.radio.pair(peer), timeout=90)
                self.remember(peer)
                if action == "connect":
                    self.sender.connect(peer, destination, self.radio.address)
                else:
                    self.sender.status = ("Paired. Open YT Music or YT Video on the Nano to connect."
                                          if self.auto_enabled else
                                          "Paired. Open the matching app on the Nano and click Connect.")
            except Exception as error:
                self.sender.status = str(error)
                raise
            finally:
                self.connection_lock.release()
        elif action == "stopPair":
            self.call(self.radio.stop_pair())
        elif action == "confirm":
            self.call(self.radio.confirm(payload.get("accept") is True))
        elif action == "disconnect":
            with self.connection_lock:
                self.auto_after = time.monotonic() + AUTO_RETRY_MAX
                self.sender.disconnect()
                self.auto_wake.set()
        elif action == "cancel":
            with self.sender.lock:
                if self.sender.job:
                    self.sender.job.set()
        elif action in ("search", "link"):
            if destination not in DESTINATIONS:
                raise ValueError("Choose YT Music or YT Video.")
            self.options(payload)
            session = self.sender.require(destination)
            value = payload.get("value", "").strip()
            if action == "search":
                if not value or len(value.encode()) > 200:
                    raise ValueError("Enter a search query up to 200 bytes.")
                self.sender.launch(lambda folder, opts, check: self.sender.search(session, value, check), session)
            else:
                media.youtube_url(value)
                self.sender.launch(lambda folder, opts, check: self.sender.send_youtube(
                    session, value, folder, opts, check), session)
        elif action == "message":
            with self.sender.lock:
                if self.sender.job is not None:
                    raise RuntimeError("Wait for the current transfer to finish.")
            value = payload.get("value", "")
            if not isinstance(value, str) or not value or not all(32 <= ord(c) <= 126 for c in value):
                raise ValueError("Enter printable ASCII text.")

            async def notify():
                if len(value) > self.radio.notification_limit:
                    raise ValueError(f"Keep the message to {self.radio.notification_limit} characters or fewer.")
                self.radio.tx.notify(value.encode())
            self.call(notify())
        else:
            raise ValueError("Unknown action.")

    def upload(self, source, name, options, cleanup):
        destination = options.get("destination")
        if destination == "ble":
            quality = {"high": 1, "medium": 2, "low": 3}.get(options.get("audio"))
            mode = options.get("transport")
            if not quality or mode not in ("read", "notify", "fast"):
                raise ValueError("Choose a supported BLE audio quality and transport.")
            state = self.call(self.radio_state(), 5)
            if not state.get("bleReady") or (mode == "read" and not state.get("bleReadReady")):
                raise RuntimeError("Connect from BLE Chat or Bluetooth Test and wait for the Nano to be ready.")

            def send(folder, opts, check):
                self.sender.status = "Preparing BLE audio…"
                clip = media.ble_clip(source, quality, check)
                self.sender.status = "Sending BLE audio…"
                def progress(percent):
                    self.sender.progress = percent
                self.call(self.radio.ble_audio(*clip, quality, options.get("autoplay") is True, mode, check, progress),
                          timeout=2400)
                self.sender.progress = 100
                self.sender.status = "Audio sent."

            self.sender.launch(send, cleanup=cleanup)
            return
        self.options(options)
        session = self.sender.require(destination)

        def send(folder, opts, check):
            if destination == "youtubeVideo":
                self.sender.send_video(session, source, folder, opts, check)
            else:
                self.sender.send_audio(session, source, folder, opts, check, name)

        def report_failure(folder, opts, check):
            try:
                send(folder, opts, check)
            except Exception as error:
                if not session.closed.is_set():
                    self.sender.send_status(session, str(error), check)
                raise

        self.sender.launch(report_failure, session, cleanup=cleanup)


class Handler(BaseHTTPRequestHandler):
    def send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, value):
        self.send(status, "application/json", json.dumps(value).encode())

    def authorized(self):
        expected = self.server.authorization
        if expected and not secrets.compare_digest(self.headers.get("Authorization", "").encode(), expected):
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="Nano Sender"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        try:
            if self.path == "/":
                self.send(HTTPStatus.OK, "text/html; charset=utf-8", PAGE.read_bytes())
            elif self.path == "/state":
                self.json(HTTPStatus.OK, self.server.app.state())
            else:
                self.json(HTTPStatus.NOT_FOUND, {"error": "Page not found."})
        except Exception as error:
            self.json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})

    def do_POST(self):
        if not self.authorized():
            return
        if self.headers.get("X-Nano-Request") != "1":
            self.json(HTTPStatus.FORBIDDEN, {"error": "Open Nano Sender in your browser."})
            return
        try:
            path = urlsplit(self.path)
            length = int(self.headers.get("Content-Length", 0))
            if path.path == "/upload":
                if not 0 < length <= MAX_UPLOAD:
                    raise ValueError("Choose a nonempty file within the 4 GiB range.")
                params = parse_qs(path.query)
                options = json.loads(params.get("settings", ["{}"])[0])
                name = params.get("name", ["Media"])[0]
                folder = tempfile.TemporaryDirectory(prefix="nano-upload-")
                try:
                    source = Path(folder.name) / ("input" + Path(name).suffix[:12])
                    self.connection.settimeout(30)
                    with source.open("wb") as file:
                        while length:
                            block = self.rfile.read(min(length, 65536))
                            if not block:
                                raise OSError("The browser disconnected during upload.")
                            file.write(block)
                            length -= len(block)
                    self.server.app.upload(source, name, options, folder.cleanup)
                except BaseException:
                    folder.cleanup()
                    raise
            elif path.path.startswith("/api/"):
                if not 0 < length <= 8192:
                    raise ValueError("Invalid request size.")
                self.server.app.command(path.path.removeprefix("/api/"), json.loads(self.rfile.read(length)))
            else:
                raise ValueError("Unknown request.")
            self.json(HTTPStatus.OK, {"ok": True})
        except Exception as error:
            self.json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def log_message(self, format, *args):
        # Polling is frequent, and upload URLs contain local filenames.
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1", help="IPv4 bind address")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--adapter", default="hci0")
    parser.add_argument("--no-auto-connect", action="store_true",
                        help="Require Connect in the browser for Classic Bluetooth")
    args = parser.parse_args()
    bind = ipaddress.IPv4Address(args.listen)
    if not args.adapter.startswith("hci") or not args.adapter[3:].isdigit():
        parser.error("Use a BlueZ adapter name such as hci0.")
    with ThreadingHTTPServer((str(bind), args.port), Handler) as server:
        server.authorization = None
        if not bind.is_loopback:
            password = secrets.token_urlsafe(18)
            server.authorization = b"Basic " + base64.b64encode(f"nano:{password}".encode())
            print(f"Browser login is nano / {password}", flush=True)
        try:
            server.app = App(args.adapter, auto_connect=not args.no_auto_connect)
        except Exception as error:
            parser.exit(1, f"Bluetooth setup failed: {error}\n")
        print(f"Nano Bluetooth Link listening on {bind}:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.app.close()


if __name__ == "__main__":
    main()
