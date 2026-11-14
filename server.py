"""Run Nano Sender on Raspberry Pi OS and operate it from a browser."""
import argparse
import asyncio
import base64
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import secrets
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

import media
from sender import ADDRESS, DESTINATIONS, VIDEO, YOUTUBE, Sender

PAGE = Path(__file__).with_name("index.html")
MAX_UPLOAD = media.UINT32_MAX


def address(value):
    value = value.upper().strip()
    if not ADDRESS.fullmatch(value):
        raise ValueError("Enter a Bluetooth address such as AA:BB:CC:DD:EE:FF.")
    return value


class App:
    def __init__(self, adapter):
        self.sender = Sender()
        self.loop = asyncio.new_event_loop()
        self.settings_file = Path.home() / ".config" / "nano-sender" / "device.json"
        try:
            self.saved = address(json.loads(self.settings_file.read_text())["address"])
        except (OSError, KeyError, ValueError, TypeError, AttributeError):
            self.saved = ""
        self.radio = None
        self.radio_error = None
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        if adapter is not None:
            self.call(self.start_radio(adapter))

    async def start_radio(self, adapter):
        from bluez import Radio

        self.radio = Radio(adapter)
        await self.radio.start()
        try:
            await self.radio.advertise(False)
        except Exception as error:
            self.radio_error = f"BLE discovery unavailable. Enter the Pi address manually. {error}"

    def call(self, coroutine, timeout=70):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    async def radio_state(self):
        if self.radio is None:
            return {}
        return {"pi": self.radio.address, "pairCode": self.radio.prompt, "radioError": self.radio_error,
                "bleReady": self.radio.tx.notifying,
                "devices": [{"address": d["address"], "name": d["name"], "paired": d["paired"]}
                            for d in await self.radio.devices()]}

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

    def options(self, values):
        playback = values.get("playback", "after")
        fps = int(values.get("fps", 5))
        audio, quality = values.get("audio", "low"), values.get("quality", "balanced")
        convert = values.get("convert", False)
        if audio not in media.AUDIO or quality not in media.CRF or fps not in (5, 10, 15, 30):
            raise ValueError("Choose a supported quality and frame rate.")
        if playback not in ("after", "live") or not isinstance(convert, bool):
            raise ValueError("Invalid playback settings.")
        with self.sender.lock:
            self.sender.options = {"audio": audio, "quality": quality, "fps": fps,
                                   "playback": playback, "convert": convert}

    def state(self):
        return {**self.sender.snapshot(), **self.call(self.radio_state(), 5), "saved": self.saved}

    def command(self, action, payload):
        destination = payload.get("destination")
        if action == "settings":
            self.options(payload)
        elif action == "mode":
            self.call(self.radio.advertise(destination == "ble"))
        elif action == "scan":
            self.call(self.radio.scan())
        elif action == "pair":
            peer = address(payload.get("address", ""))
            self.call(self.radio.pair(peer))
            self.remember(peer)
        elif action == "confirm":
            self.call(self.radio.confirm(payload.get("accept") is True))
        elif action == "connect":
            peer = address(payload.get("address", ""))
            if destination not in DESTINATIONS:
                raise ValueError("Choose a Classic destination.")
            self.options(payload)
            self.sender.connect(peer, destination, self.radio.address)
            self.remember(peer)
        elif action == "disconnect":
            self.sender.disconnect()
        elif action == "cancel":
            with self.sender.lock:
                if self.sender.job:
                    self.sender.job.set()
        elif action in ("search", "link"):
            if action == "search" and destination not in YOUTUBE:
                raise ValueError("Choose a YouTube tab.")
            if action == "link" and destination not in (*YOUTUBE, "stream"):
                raise ValueError("Choose Stream or a YouTube tab.")
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
            value = payload.get("value", "")
            if not 1 <= len(value) <= 20 or not all(32 <= ord(c) <= 126 for c in value):
                raise ValueError("Enter 1–20 printable ASCII characters.")

            async def notify():
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
            if not self.call(self.radio_state(), 5).get("bleReady"):
                raise RuntimeError("Connect from Bluetooth Test and wait for the Nano to subscribe.")

            def send(folder, opts, check):
                self.sender.status = "Preparing BLE audio…"
                clip = media.ble_clip(source, quality, check)
                self.sender.status = "Sending BLE audio…"
                self.call(self.radio.ble_audio(*clip, quality, options.get("autoplay") is True, mode, check),
                          timeout=1200)
                self.sender.progress = 100
                self.sender.status = "Audio sent."

            self.sender.launch(send, cleanup=cleanup)
            return
        self.options(options)
        session = self.sender.require(destination)

        def send(folder, opts, check):
            if destination in VIDEO:
                self.sender.send_video(session, source, folder, opts, check)
            else:
                self.sender.send_audio(session, source, folder, opts, check, name)

        self.sender.launch(send, session, cleanup=cleanup)


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
            server.app = App(args.adapter)
        except Exception as error:
            parser.exit(1, f"Bluetooth setup failed: {error}\n")
        print(f"Nano Sender listening on {bind}:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.app.sender.disconnect()


if __name__ == "__main__":
    main()
