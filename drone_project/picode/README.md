# Raspberry Pi flight-code setup and operation

This directory contains the two real-vehicle entry points:

- `testforward.py` — the first supervised test. It checks the vehicle, waits
  for Enter, slowly climbs to 2 m, flies 2 m forward in the takeoff-heading
  direction, selects RTL, and monitors until disarm.
- `geotag_mission.py` — a wrapper around the repository's canonical ROS 2
  geotag mission. **Its current source climbs to 10 m and searches a 15 m by
  10 m area. It is not the 2 m commissioning test.**

The target computer is a 64-bit Ubuntu 22.04 Raspberry Pi 4B. These steps are
for ArduCopter and MAVLink 2 at 115200 baud.

## Known wiring and ownership

Power everything off before connecting or changing UART wires.

| Signal | Pi GPIO | Pi physical pin | Connect to FC |
| --- | ---: | ---: | --- |
| Pi TX | GPIO4 | 7 | FC UART4 RX |
| Pi RX | GPIO5 | 29 | FC UART4 TX |
| Ground | — | 6 | FC ground |

The expected Linux device is `/dev/ttyAMA2`. The UART is non-inverted 3.3 V
TTL. Cross TX to RX, connect one common ground, and do **not** connect a UART
power pin between the Pi and flight controller. Confirm the actual FC pad
labels and voltage in its hardware manual before applying power.

The MicoAir MTF-01 optical-flow/rangefinder unit is connected directly to FC
UART5 and mounted facing downward in its documented default orientation. It
does not connect to this Pi serial port.

MAVProxy is the only process allowed to open `/dev/ttyAMA2`. It forwards local
UDP to either `testforward.py` or MAVROS. Never let MAVROS and MAVProxy both
open the serial device, and never run both missions at once.

## Brand-new Pi installation

First clone this complete repository onto the Pi. The examples use
`~/drone_project`; use your real path if different.

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

After reconnecting over SSH:

```bash
sudo apt install -y git
git clone YOUR_REPOSITORY_URL ~/drone_project
cd ~/drone_project
chmod +x picode/setup_pi.sh
./picode/setup_pi.sh
sudo reboot
```

The installer is intentionally limited to 64-bit Ubuntu 22.04 on a Raspberry
Pi 4. It installs tmux, Python build/runtime packages, ROS 2 Humble, MAVROS,
`cv_bridge`, GeographicLib data, MAVProxy, pymavlink, and the QR decoder. It
builds `ros2_ws`, enables the Pi `uart3` overlay, disables an exact
`ttyAMA2` serial-getty if present, and adds the current user to `dialout`.

The reboot is required for the boot overlay and new group membership. The
installer does not configure the flight controller, MTF-01, camera driver,
battery monitor, RC receiver, or ArduPilot failsafes.

## Post-reboot checks — propellers removed

Do all checks in this section with propellers removed. Do not use a loop that
waits forever for hardware: every command below either exits or is bounded.

```bash
cd ~/drone_project
test -e /dev/ttyAMA2 && echo "UART device: PASS" || echo "UART device: FAIL"
ls -l /dev/ttyAMA2
id -nG | tr ' ' '\n' | grep -qx dialout && echo "dialout: PASS" || echo "dialout: FAIL"
```

Check that nothing owns the port before MAVProxy starts:

```bash
sudo fuser -v /dev/ttyAMA2 || true
```

Power the FC and start a temporary bounded MAVProxy connection test:

```bash
cd ~/drone_project
timeout 20s picode/.venv/bin/mavproxy.py \
  --master=/dev/ttyAMA2 \
  --baudrate=115200 \
  --mav20
```

A heartbeat and vehicle identification indicate a working link. A timeout is
normal after 20 seconds. Garbled/no messages usually mean swapped wiring,
wrong FC SERIAL mapping/baud/protocol, no common ground, a console owning the
port, or the wrong Linux device.

On the flight controller, verify against the actual board documentation—not
only the printed `UART4` label—that its pad group maps to the intended
`SERIALx` parameters. Configure that port for MAVLink 2 and 115200 baud. Save,
reboot the FC, then confirm heartbeats again. Do not guess a `SERIALx` number;
the mapping depends on the FC board.

## ArduPilot commissioning gate

Software cannot make an uncommissioned aircraft safe. Complete every item
before installing props:

- Correct frame class/type and current stable ArduCopter firmware.
- Accelerometer level calibration and all required compass calibration.
- RC calibration; deliberate arm/disarm control; a tested RTL mode switch.
- Motor order and direction verified with props removed; correct propellers
  later fitted to the correct motors.
- Battery voltage/current monitor calibrated and low/critical battery
  failsafes configured.
- RC-loss and GCS/companion-link failsafes deliberately chosen and bench
  tested. RTL must have a valid home/global position and suitable RTL altitude.
- MTF-01 flow/rangefinder parameters, orientation, scale, and valid range
  configured on FC UART5 using the MicoAir and ArduPilot documentation.
- EKF source selection configured so the intended GPS/flow/range data is
  actually fused; no EKF lane errors; low vibration in a manual hover log.
- Stable manual hover already demonstrated by a pilot before autonomous tests.

Use Mission Planner/QGroundControl or MAVProxy status messages to verify these
live conditions at the test site:

- GPS fix is 3D or better, at least 10 satellites, good HDOP, and the reported
  position does not wander materially while the aircraft is stationary.
- Home is set at the correct location and RTL behavior is already tested under
  pilot control.
- `OPTICAL_FLOW_RAD.quality` is stable above 50 over the tiled surface and flow
  direction agrees with hand motion (props removed).
- `DISTANCE_SENSOR.current_distance` follows measured height, has the correct
  orientation, and remains inside the MTF-01's commissioned min/max range.
- Local position remains stable, attitude is level, EKF is healthy, battery is
  safely above the script threshold, and there are no pre-arm failures.

## First flight: `testforward.py`

This test is outdoors on flat, textured/tiled ground with good lighting and a
clear radius of at least 5 m. A 5 m radius is only a minimum for this 2 m test;
increase it whenever possible. Keep a trained pilot, observer, RC transmitter,
and physical perimeter in place. Use the tested RTL switch as recovery. Also
retain an independent way to stop propulsion after landing. Do not test inside
a building where a flyaway puts people or property at risk.

Start one named tmux session with two windows:

```bash
cd ~/drone_project
tmux new-session -d -s drone -n mavproxy
tmux new-window -t drone -n mission
tmux send-keys -t drone:mavproxy \
  "cd ~/drone_project && mkdir -p picode/logs && picode/.venv/bin/mavproxy.py --master=/dev/ttyAMA2 --baudrate=115200 --mav20 --out=udp:127.0.0.1:14550 --state-basedir=picode/logs" C-m
tmux select-window -t drone:mavproxy
tmux attach -t drone
```

Wait for MAVProxy to show FC heartbeats. Detach with `Ctrl-b d`, then run:

```bash
tmux send-keys -t drone:mission \
  "cd ~/drone_project && picode/.venv/bin/python picode/testforward.py" C-m
tmux select-window -t drone:mission
tmux attach -t drone
```

The program samples telemetry for 10 seconds and exits on missing/bad data; it
does not sit in an infinite FC connection loop. Read every printed check. It
will not force-arm. It waits for a fresh Enter press only after checks pass,
then rechecks flight-critical telemetry for three seconds. Defaults are 2.0 m
altitude, 2.0 m forward, 0.30 m/s climb, and 0.50 m/s forward. On an error
while it owns GUIDED, it requests LAND. An unexpected FC/pilot mode change
causes it to release control. After forward travel it restores temporary
speed parameters, selects RTL, and waits up to 180 seconds for disarm.

Stop before pressing Enter if any displayed fact is unexpected. `Ctrl-C`
before arming exits; during a mission it requests LAND only if it still owns
GUIDED. The RC recovery method remains primary—SSH is not an emergency stop.

Useful non-flight checks:

```bash
picode/.venv/bin/python picode/testforward.py --help
python3 -m py_compile picode/testforward.py picode/geotag_mission.py
```

## Full geotag mission — only after separate validation

Do not run this as the first real flight. Review and validate the constants in
`ros2_ws/src/rescue_control/rescue_control/geotag_mission.py` for the actual
site and aircraft. At present they include a 10 m takeoff, 15 m search length,
Y from -5 m to +5 m, 1.2 m QR search height, a 640×360 image assumption, and a
90-degree horizontal field of view. The mission sends confirmed QR text to
UDP `127.0.0.1:5005` and eventually requests RTL.

The exact downward camera was not specified, so its driver is deliberately not
invented by the installer. Install the vendor-supported Ubuntu 22.04/ROS 2
driver and remap or configure it to publish `sensor_msgs/msg/Image` on
`/camera/down/image`. Verify real frames before flight:

```bash
source /opt/ros/humble/setup.bash
source ~/drone_project/ros2_ws/install/setup.bash
timeout 10s ros2 topic hz /camera/down/image
timeout 5s ros2 topic echo --once /camera/down/image
```

For the geotag mission use three tmux windows (MAVProxy, MAVROS, mission). Start
MAVProxy on a different local UDP port from `testforward.py`:

```bash
cd ~/drone_project
tmux new-session -d -s geotag -n mavproxy
tmux new-window -t geotag -n mavros
tmux new-window -t geotag -n mission
tmux send-keys -t geotag:mavproxy \
  "cd ~/drone_project && mkdir -p picode/logs && picode/.venv/bin/mavproxy.py --master=/dev/ttyAMA2 --baudrate=115200 --mav20 --out=udp:127.0.0.1:14551 --state-basedir=picode/logs" C-m
tmux send-keys -t geotag:mavros \
  "source /opt/ros/humble/setup.bash && source ~/drone_project/ros2_ws/install/setup.bash && ros2 run mavros mavros_node --ros-args -p fcu_url:=udp://127.0.0.1:14551@" C-m
tmux select-window -t geotag:mavproxy
tmux attach -t geotag
```

After MAVProxy has heartbeats, detach with `Ctrl-b d`. Inspect the MAVROS log,
then verify its topics from the SSH shell:

```bash
tmux capture-pane -p -t geotag:mavros -S -50
source /opt/ros/humble/setup.bash
source ~/drone_project/ros2_ws/install/setup.bash
timeout 5s ros2 topic echo --once /mavros/state
timeout 5s ros2 topic echo --once /mavros/local_position/pose
```

Start the separately installed camera publisher, confirm its topic, and only
then launch the mission interactively:

```bash
tmux send-keys -t geotag:mission \
  "cd ~/drone_project && source /opt/ros/humble/setup.bash && source ros2_ws/install/setup.bash && picode/.venv/bin/python picode/geotag_mission.py" C-m
tmux select-window -t geotag:mission
tmux attach -t geotag
```

The node waits for MAVROS and then waits for Enter before requesting GUIDED and
arming. It does not reproduce `testforward.py`'s comprehensive preflight gate,
so perform the commissioning checklist independently. Do not press Enter until
the site, camera, GPS, flow/range, EKF, battery, RC RTL recovery, and perimeter
are all confirmed.

## Updating and troubleshooting

After pulling code changes, rerun the idempotent installer or only rebuild:

```bash
cd ~/drone_project
git pull --ff-only
./picode/setup_pi.sh
```

Common checks:

```bash
# Who owns the serial port?
sudo fuser -v /dev/ttyAMA2 || true

# Was UART3 applied at boot?
grep -n 'dtoverlay=uart3' /boot/firmware/config.txt
dmesg | grep -E 'ttyAMA|uart' | tail -n 30

# Can the venv import its packages?
picode/.venv/bin/python -c 'import pymavlink, quirc, cv2; print("PASS")'

# Are tmux sessions still alive after SSH disconnects?
tmux list-sessions

# Stop a non-flying checkout session cleanly.
tmux send-keys -t drone:mission C-c
tmux send-keys -t drone:mavproxy C-c
```

Never kill MAVProxy or the Pi merely to stop an aircraft in flight. Use the
tested RC recovery procedure. After every test, save the FC `.bin` log and
review GPS accuracy, EKF innovations, flow quality, range, vibration, battery,
mode changes, failsafes, and the commanded versus actual path before expanding
the envelope.

## Authoritative setup references

- [ROS 2 Humble on Ubuntu 22.04](https://docs.ros.org/en/humble/Installation/Ubuntu-Install-Debs.html)
- [Official ROS apt-source packages](https://github.com/ros-infrastructure/ros-apt-source)
- [MAVROS connection URLs and required GeographicLib data](https://docs.ros.org/en/humble/p/mavros/__README.html)
- [MAVProxy Linux installation](https://ardupilot.org/mavproxy/docs/getting_started/download_and_installation.html)
- [MAVProxy startup and serial options](https://ardupilot.org/mavproxy/docs/getting_started/quickstart.html)
- [Raspberry Pi additional UART overlays](https://www.raspberrypi.com/documentation/computers/configuration.html#enable-additional-uarts)
