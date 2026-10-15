# Flight and USB video recording

The current mission takes off to 2 m, moves 2 m forward along the initial
heading, then uses RTL to return and land. It preserves the flight behavior
in your latest edited script. It does **not** upload a geofence or perform a
separate backward GUIDED leg. Its 3 m local-position check runs during GUIDED;
RTL uses the flight controller's existing navigation and failsafe settings.

## File tree

```text
all_drone/
├── scripts/
│   ├── flight_2m.py             # existing command, launches the mission
│   ├── record_video.py          # USB recording only, no drone connection
│   ├── test_flight_2m.py         # offline test launcher
│   └── flight_2m/
│       ├── __init__.py
│       ├── __main__.py
│       ├── mission.py           # flight logic and recording lifecycle
│       ├── recorder.py          # FFmpeg USB camera recorder
│       ├── README.md
│       ├── tests/
│       │   └── test_flight.py
│       └── archive/             # earlier backups, docs and tests
├── video/
│   ├── .gitkeep
│   ├── flight_<UTC timestamp>_<unique id>.mkv
│   └── flight_<UTC timestamp>_<unique id>.log
└── drone_project/picode/
    └── testforward.py           # shared MAVLink/MTF-01 flight helpers
```

Video and log files appear when recording starts; they are excluded from Git.
Unrelated setup, GCS, OLED and simulation files remain in their existing locations.
The editor swap file in `scripts` was left in place to preserve editor recovery.

## Run from the repository root

Install the USB camera recorder dependency on the Pi if needed:

```bash
sudo apt install ffmpeg
```

Print the flight plan without opening either the camera or the vehicle:

```bash
drone_project/pi_flight/.venv/bin/python scripts/flight_2m.py
```

Record a ten-second camera clip without connecting to the drone:

```bash
drone_project/pi_flight/.venv/bin/python scripts/record_video.py --duration 10
```

Run the mission with USB video recording enabled:

```bash
drone_project/pi_flight/.venv/bin/python scripts/flight_2m.py --execute
```

The default camera is `/dev/video0`, resolution is 640×480, and rate is 30 fps.
Keep the existing MAVProxy router running and close other vehicle command
interfaces before flight. The mission launches without an interactive prompt.

Choose another camera or a supported camera resolution/rate:

```bash
drone_project/pi_flight/.venv/bin/python scripts/flight_2m.py --execute \
  --camera /dev/video2 --video-size 1280x720 --video-fps 30
```

Inspect your camera's supported formats using FFmpeg's
[V4L2 input options](https://ffmpeg.org/ffmpeg-devices.html#video4linux2_002c-v4l2):

```bash
ffmpeg -f v4l2 -list_formats all -i /dev/video0
```

Use `--video-dir /path/to/folder` to change the output location. Use `--no-video`
to explicitly run the flight without recording. Recording is video only,
without audio. Matroska (`.mkv`) allows FFmpeg to write the recording as it runs.

## Recording and flight behavior

After preflight, the recorder starts in a separate process before GUIDED/arming.
The script waits up to ten seconds for video output. Missing FFmpeg, camera
permissions, a busy camera, or unsupported capture settings stop the mission
before arming. The `.log` beside the recording contains camera/encoder errors.
Close other programs using the USB camera before starting.

Recording continues through takeoff, forward travel and RTL/landing monitoring.
It is stopped and flushed on mission completion, errors, pilot takeover or
Ctrl+C, after flight cleanup. Unexpected recorder exit is reported; it does
not interrupt flight control. If the aircraft remains airborne after a pilot
takeover or monitoring timeout, recording ends when this program exits.

MTF-01 flow remains read from `OPTICAL_FLOW` (`opt_m_x`, `opt_m_y`, `opt_qua`),
with `OPTICAL_FLOW_RAD` fallback. Downward range uses `DISTANCE_SENSOR`.
The current mission does not gate preflight or flight on `opt_qua`; quality
is still displayed during preflight. Flow data must remain fresh and finite,
and EKF, rangefinder, battery and FC arming checks still apply.
No optical-flow or rangefinder configuration is changed.

The mission retains its existing flight checks and temporary 0.3 m/s speed
limits. It restores saved speed parameters after fresh telemetry confirms
disarm, or prints their original values if restoration must be deferred.
Takeoff position and heading are captured from fresh telemetry after arming,
immediately before issuing TAKEOFF. Camera startup and preflight delays are
therefore excluded from takeoff drift. Drift after this reference is still
limited to 0.5 m; the console reports its measured value and takeoff height.
Takeoff height confirmation uses the downward MTF-01 `DISTANCE_SENSOR`
reading, converted from centimetres and corrected for roll/pitch. A fresh
ground reading is saved before takeoff; a 2 m rise above that reading must
remain within ±0.2 m for two seconds. Missing, stale, wrongly oriented, or
out-of-range distance data aborts without falling back to GPS/barometric/NED
altitude. Range height also monitors overshoot and height during forward travel.
Horizontal drift continues to use local position. The forward NED height target
holds the local Z observed when rangefinder takeoff confirmation completes;
the script does not command an additional 2 m climb. TAKEOFF and RTL still use
the flight controller's configured EKF/navigation; no FC sensor parameters change.

## View recordings in your browser

```bash
python3 scripts/video_gallery.py
```

Open **http://localhost:8080** to play or download recordings. See the
[gallery instructions](../video_gallery/README.md) for Pi port forwarding
and configuration.

## Validation

```bash
drone_project/pi_flight/.venv/bin/python scripts/test_flight_2m.py
```

Tests use mocked aircraft connections. A recorder integration test encodes
generated frames with real FFmpeg, stops the process, then verifies the file
with FFprobe. It never opens a hardware camera or connects to the aircraft.
Real USB capture and flight still need validation on the Pi.
