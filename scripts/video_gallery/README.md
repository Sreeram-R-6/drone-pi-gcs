# Local video gallery

From the repository root:

```bash
python3 scripts/video_gallery.py
```

Open **http://localhost:8080**. Stop the server with Ctrl+C.
For automatic startup and links to the OLED/QR websites, use the
[startup web dashboard installer](../web/README.md).
The gallery reads the repository's `video/` directory and refreshes its library
every 15 seconds. Select a recording to play, seek with the player controls,
or download the original. No Python packages need installing.

## Manual recording

Use **Start recording** and **Stop recording** to capture USB-camera clips
independently of flight missions. Clips are saved as `manual_*.mkv` under
`video/manual/` and appear in the **Manual clips** tab. Flight recordings stay
in `video/` and appear in the **Flight recordings** tab. Stop a manual clip
before playing or downloading it. Recording continues if you close the browser;
reopen the page to see its status and stop it. Restarting/stopping the gallery
service also stops its manual recorder.
If the USB camera is disconnected, the page shows its missing device path
and enables Start recording automatically once that device appears.

The gallery uses `/dev/video0` at 640×480/30 fps by default. Set `--camera`
when launching the server to choose another V4L2 device. Both manual and flight
recorders share a camera lock under `video/.camera-locks/`; a busy camera causes
an error rather than stopping the other recorder. The buttons never issue
flight commands. Start/stop requests require the page's token and same origin,
and also work through the existing Tailscale website address.

## Navigation and deleting clips

**Back to dashboard** opens port 8070 on the same IP/hostname used for this
page, including when using Tailscale. Select a clip, then choose **Delete clip**
and confirm to permanently remove its original video and current cached MP4
copy. The library refreshes immediately. Diagnostic `.log` files are retained.
Deletion works in both libraries and rejects active manual or flight recordings.

MKV recordings are prepared for playback as cached MP4 files using FFmpeg.
This copies the video stream without re-encoding or altering the original.
The cache lives in `video/.web-cache/`. FFmpeg is already required by the
USB recorder; install it with `sudo apt install ffmpeg` if needed.
Wait for a recording to finish before viewing it. Other imported video files
must use a codec your browser supports; H.264 recordings from this project's
recorder are prepared as MP4 for playback.

The server binds only to `127.0.0.1`. If it runs on your Pi and you want to
view it from another computer, forward its port:

```bash
ssh -N -L 8080:127.0.0.1:8080 user@PI_ADDRESS
```

Then open http://localhost:8080 on that computer.

Optional arguments:

```bash
python3 scripts/video_gallery.py --port 8091 --video-dir /path/to/videos
```

Offline HTTP/media tests (generated test footage, no drone or camera):

```bash
python3 scripts/test_video_gallery.py
```
