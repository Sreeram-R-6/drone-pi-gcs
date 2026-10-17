# Flight and USB video recording

The current mission captures launch coordinates and heading, lifts off, uses
continuous rangefinder feedback to hold a 1.5 m rise above the ground reading,
flies to its destination, returns to the captured launch XY, and requests LAND.
The default destination is 2 m forward along the initial heading. The 2 m limit
is a ceiling, not the height target: the lower target leaves braking margin.
It does not use RTL, which can climb to a configured return altitude. The 3 m
local-position guard remains active throughout GUIDED flight. No fence is
uploaded or enabled automatically.

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

Recording continues through takeoff, destination travel, return and landing monitoring.
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

The mission temporarily sets navigation climb speed to 0.15 m/s and horizontal
speed to 0.2 m/s. It also temporarily sets `WP_NAVALT_MIN=0` if needed, so native
takeoff does not postpone XY navigation. Original values are restored after
fresh telemetry confirms disarm; deferred restoration is reported in the log.
`FENCE_AUTOENABLE` must be 0 or 3: modes 1/2 depend on native takeoff completion,
which the early range-control transition would skip. Fence parameters are not
changed by this mission.

Takeoff reference XY, initial heading and downward ground range are captured
from fresh telemetry after arming. Native TAKEOFF requests only 0.8 m to perform
spool-up and initial lift-off. After at least 0.3 m clearance above the captured
ground reading for 0.3 seconds, all three GUIDED velocity axes are updated at
5 Hz. Horizontal velocity uses fixed-coordinate position error with measured
velocity damping, limited to 0.2 m/s. Vertical velocity uses **live rangefinder
height error and filtered range climb rate**, limited to 0.15 m/s up/down. A
fixed EKF Z target is no longer used to control height. Range is converted from
centimetres and corrected for roll/pitch; local Z and local vertical velocity
are logged for comparison. Missing/stale/invalid range readings abort.

Measured vertical ground clearance of 1.85 m triggers LAND before the 2 m
ceiling. Predictive protection also requests LAND earlier if measured upward
motion projects clearance to 1.95 m. These are software guards, not a certified
physical stop: sensor error, controller response, inertia, terrain changes or
an independent FC failsafe can still cause overshoot. The height target is a
1.5 m rise above the initial ground reading; the ceiling guard uses absolute
range clearance, so the sensor mounting height consumes part of the margin.
No claim of a guaranteed 2 m physical maximum is made without flight validation.

Height must settle within 0.1 m, launch XY within 0.15 m, and horizontal/vertical
speed within 0.15 m/s for two seconds before departure. A separate launch hover
rechecks stability. Drift over 0.5 m during takeoff/launch hover aborts; the
3 m local-position limit applies to outbound and return legs. Pilot/FC mode
changes relinquish control. Destination and return both use the same live
range-height controller; LAND handles final descent. Autonomous FC/RC/GCS
failsafes retain their own configuration and can choose other modes.

An optional GPS destination can be supplied as a latitude/longitude pair:

```bash
drone_project/pi_flight/.venv/bin/python scripts/flight_2m.py --execute \
  --target-latitude YOUR_LATITUDE --target-longitude YOUR_LONGITUDE
```

Replace both placeholders with decimal degrees. The destination is converted
once into local NED coordinates using fresh launch GPS and must be within
2 m of launch. The script prints launch GPS and local launch/destination
coordinates. GPS error can be significant at these small distances. The web
launcher uses the default 2 m forward destination.

Offline tests use a simplified controller response, not an aircraft model.
They cover range-feedback command signs/limits, wrong local Z, early correction,
ceiling and predictive aborts, coordinate conversion, return/LAND sequencing,
pilot takeover, stale sensors, and cleanup. They do not establish real flight
stability. References: [ArduPilot GUIDED commands](https://ardupilot.org/dev/docs/copter-commands-in-guided-mode.html)
and [RTL climb behavior](https://ardupilot.org/copter/docs/rtl-mode.html).

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
