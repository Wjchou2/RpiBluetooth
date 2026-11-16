# Nano Bluetooth Link for Raspberry Pi

The Raspberry Pi runs Bluetooth and media conversion. Use its browser interface on the Pi, or open it from your Mac, phone, or another device.

## Run on the Pi

In the Pi's `RpiBluetooth` directory:

```sh
sudo apt update
sudo apt install bluez ffmpeg nodejs python3-venv
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python server.py
```

Open `http://127.0.0.1:8765` on the Pi. For access from the same LAN, start the server with `python server.py --listen 0.0.0.0`, run `hostname -I` in a second terminal, and open `http://<Pi LAN IP>:8765` from your browser. Sign in as `nano` with the password printed by the server. LAN mode uses HTTP. For SSH access, keep the default bind address and run `ssh -L 8765:127.0.0.1:8765 YOUR_PI_HOST` on the other computer, then open `http://127.0.0.1:8765` there.

Use `--adapter hci1` to choose a second adapter. If the adapter cannot power on, check `rfkill list bluetooth`, `systemctl status bluetooth`, and `bluetoothctl show` on the Pi. For YouTube, put a Node.js version supported by yt-dlp on the Pi's PATH. Update the downloader with `python -m pip install -U 'yt-dlp[default]'` if extraction stops working.

## YT Music and YT Video

1. Enable Bluetooth on the Nano and open **YT Music** or **YT Video**. Start listening with **Find Mac**, a saved sender, or **Mac Address**. The Pi's Bluetooth address appears under **Device Setup** on the webpage.
2. For the first connection, open **Device Setup** on the webpage to find the Nano or enter its Classic Bluetooth address. Choose **Pair** and confirm the matching code. If discovery misses it, keep the Nano's Bluetooth settings open during discovery, then return to the receiving app.
3. Leave the server running. On later uses, open either app on the Nano and start listening. The Pi connects to the saved, paired Nano and selects the correct app automatically. Search and choose a result on the Nano without opening the webpage.

The Pi waits between connection attempts and stops probing when connected. The retry delay starts at three seconds and backs off to 30 seconds after repeated failures. After a long idle period, give it time for the next retry. **Connect** still works on the webpage and **Disconnect** pauses automatic attempts for 30 seconds. Use `--no-auto-connect` to require a manual connection.

Both apps default to **While Sending**, **Download during transfer**, and **44.1 kHz mono**. YT Video also defaults to **320 × 176**, **22 fps**, and **Convert during transfer**. Settings you change in the browser apply to searches from the Nano until you restart the server. Use current YT Music and YT Video builds for these options.

You can also send a local file or a YouTube link from the webpage. **After Transfer** saves the encoded media before playback. YT Music also offers **Receive Test**. Close playback on the Nano before sending another video.

### YouTube Video without JavaScript

If YouTube's JavaScript challenge repeatedly stalls or destabilizes the Pi, set
`NANO_YOUTUBE_VIDEO_NO_JS=1` for the server. YT Video searches and downloads
then run without a JavaScript runtime. **While Sending** and **Download during
transfer** can still be used together. If a combined stream is unavailable or
takes too long to start, the server downloads the full video before sending.
Some YouTube videos may have fewer formats with JavaScript disabled.

For a systemd service, run `sudo systemctl edit nano-sender` and add:

```ini
[Service]
Environment=NANO_YOUTUBE_VIDEO_NO_JS=1
```

Then run `sudo systemctl restart nano-sender`.

## BLE

Open the **BLE** tab in the browser and connect to **Nano Bluetooth Link** from **BLE Chat** or **Bluetooth Test**. Send and receive text. In Bluetooth Test, you can also send audio clips up to 30 seconds through **GATT Read**, **Notifications**, or **Fast Notifications**. The Pi uses the link's reported notification size when available and falls back to 20 bytes.
