# Nano Sender for Raspberry Pi

This browser app sends media to the existing iPod nano 7 NanoApps over the same Bluetooth Classic L2CAP and BLE UART protocols as Nano UART Sender for Mac. Run it on a Raspberry Pi with Raspberry Pi OS and a Bluetooth adapter that supports Classic and BLE. Use the Nano builds that support the Mac sender's current video and YouTube features.

## Run on the Pi

From the `RpiBluetooth` directory on the Pi:

```sh
sudo apt update
sudo apt install bluez ffmpeg python3-venv
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python server.py
```

Open `http://127.0.0.1:8765` on the Pi. To use the browser on another computer, forward the port over SSH with `ssh -L 8765:127.0.0.1:8765 YOUR_PI_HOST` and open that same address on your computer. You can also launch with `--listen 0.0.0.0` on a trusted LAN. Open port 8765 on the Pi's IP address in your browser and use the login printed in the Pi terminal. LAN mode uses HTTP. Use `--adapter hci1` if your Bluetooth adapter is the second BlueZ adapter.

## Pair and send

1. Turn on Bluetooth on the Nano. Open the matching Nano app and use **Find Mac** or enter the Pi's Bluetooth address from the browser manually. The name on the Nano still says Mac. Enable listening and pairing if prompted.
2. Click **Find devices** in the browser, select the Nano, and click **Pair**. Confirm that both screens show the same code. The Pi remembers the Nano's address.
3. Click **Connect**. The tab and the Classic Files destination must match the app open on the Nano.

| Browser tab | Nano app | Actions |
| --- | --- | --- |
| Classic Files | Bluetooth Test | Send an audio file to the Classic inbox. Play it in Audio Player. |
| Classic Files | VideoStream | Send an H.264 video for playback during or after the transfer. |
| Stream | Stream | Send an audio file or YouTube link. Choose when playback starts. |
| YouTube Audio | YouTube Audio | Search from either screen and select a result on the Nano. The browser also accepts a YouTube link or audio file. |
| YouTube Streamer | YouTube Streamer | Search from either screen and select a result on the Nano. The browser also accepts a YouTube link or video file. |
| BLE | Bluetooth Test | Scan BLE on the Nano and connect to **Nano UART**. Send text or a short clip through GATT Read or notifications. |

Video honors the selected frame rate and quality. **Convert during transfer** starts sending as FFmpeg produces groups. With the option off, the Pi prepares the video on disk first. For YouTube, install Deno or Node.js on the Pi and put it on the sender's PATH. If extraction stops working, update the downloader with `python -m pip install -U 'yt-dlp[default]'`. [yt-dlp runtime setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS)

The browser's **Disconnect** closes the Classic link. For apps that receive a single transfer per connection, start listening again on the Nano and reconnect before sending another file.

For a first test, send a short local audio clip from **Classic Files**. Then try a YouTube search or send a short video at 5 FPS using **After transfer**. BLE notifications use conservative packet sizes on the Pi, so use Classic for longer audio transfers.

The Linux transport uses [BlueZ L2CAP](https://github.com/bluez/bluez/blob/master/doc/l2cap-protocol.rst), [BlueZ pairing](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Agent.rst), and [BlueZ GATT](https://github.com/bluez/bluez/blob/master/doc/org.bluez.GattCharacteristic.rst).
