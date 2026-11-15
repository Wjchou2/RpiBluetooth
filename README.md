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

1. Enable Bluetooth on the Nano, open **YT Music** or **YT Video**, and choose the matching browser tab.
2. On the Nano, use **Find Mac** or enter the Pi's Bluetooth address under **Mac Address**, then start listening. The browser shows the Pi's Bluetooth address under **Device Setup**.
3. On the browser, open **Device Setup** to find the Nano or enter its Classic Bluetooth address. If a scan misses it, keep the Nano's Bluetooth settings screen open during discovery, then return to the receiving app before connecting. Choose **Pair**, confirm that the codes match, and click **Connect**. The Pi remembers the address.
4. Send a local file, paste a YouTube link, or search from either screen and pick a result on the Nano.

**After Transfer** saves the encoded media before playback. **While Sending** starts playback after buffering. YT Music also offers **Receive Test**. **Convert during transfer** lets YT Video send each H.264 group as it finishes encoding. With **While Sending** selected, **Download during transfer** overlaps downloading, converting, and sending a YouTube link or search result. Update YT Music on the Nano to use that option.

YT Music offers 7.35, 11.025, 22.05, and 44.1 kHz mono, plus 44.1 kHz stereo. YT Video has five presets from **128 × 72** through **432 × 240**, 5, 10, 15, 22, and 30 FPS where supported by the receiver, and a separate audio quality setting. Close playback on the Nano before sending another video.

## BLE

Open the **BLE** tab in the browser and connect to **Nano Bluetooth Link** from **BLE Chat** or **Bluetooth Test**. Send and receive text. In Bluetooth Test, you can also send audio clips up to 30 seconds through **GATT Read**, **Notifications**, or **Fast Notifications**. The Pi uses the link's reported notification size when available and falls back to 20 bytes.
