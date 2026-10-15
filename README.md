# Raspberry Pi Drone GCS + OLED

Terminal ground-control station, SSD1306 OLED front panel, local web mirror,
MAVLink UART router, commissioning tools, tests, and simulation sources.
Target: Raspberry Pi 4 Model B, 64-bit Raspberry Pi OS Bookworm or Ubuntu
22.04/24.04, Python 3.10+. A fresh Pi installation has not yet been tested.
Dependencies alone cannot establish correct aircraft wiring or flight readiness.

The local deployment uses Pi-driven GUIDED missions. The Pi/link is required
throughout execution; disabling GCS-loss failsafe does not make it independent.
The optional onboard AUTO backend is inactive and remains commissioning-locked.

## Layout

- `gcs`: terminal launcher (mission picker, telemetry, map, checks).
- `drone_project/pi_flight`: mission controller and flight checks.
- `drone_project/drone_oled`: OLED/buttons, web mirror, systemd deployment.
- `drone_project/ros2_ws`, `models`, `worlds`, `drones`, `vision`: optional
  simulation/vision sources; these need separate ROS/Gazebo/camera setup.
- `scripts`: installation, tests, and public-release preparation.

The organized [2 m flight and USB recording tools](scripts/flight_2m/README.md)
live under `scripts/flight_2m/`. Flight recordings are saved in `video/`.
Run `python3 scripts/video_gallery.py` and open **http://localhost:8080** to
[browse and play recordings](scripts/video_gallery/README.md).

The [startup web dashboard](scripts/web/README.md) at **http://localhost:8070**
links to the Pi/MAVProxy/Codex terminal (8071), videos (8080), OLED panel (8090),
and QR receiver (5000), with a mobile flight-script page and live service status.
Concurrent MAVLink clients use TCP 5760 locally or through Tailscale.
Install its boot services using `sudo python3 scripts/web/install_services.py --user user`.

## Install on a Pi 4B

Clone this repository, enter its directory, and run as your normal user:

```bash
bash scripts/setup_pi.sh
./gcs --offline
```

The installer creates the GCS virtual environment and a **local**, unreviewed
configuration if missing. It does not arm, write FC parameters, change GPIO
overlays, reboot, or start a mission. Existing configuration is preserved.
`requirements.txt` is the runtime dependency entry point; OLED hardware/service
dependencies are installed separately below. Python virtual environments, local
hardware approvals, downloaded FC logs and credentials are not distributed.

## Hardware and persistent OLED + GCS

Use 3.3 V UART logic only. Cross Pi GPIO14/TX (physical pin 8) to FC RX and
Pi GPIO15/RX (pin 10) to FC TX; connect common ground. Do not connect a 5 V
serial signal. Configure the chosen FC serial port for MAVLink 2 at 115200.
Enable Pi UART, disable the serial login console, and verify the pinmux/device
on that Pi. Do not copy another aircraft's EKF, failsafe or arming settings.

The default OLED is SSD1306, 128×64, I2C bus 1, address `0x3c`: SDA GPIO2
(pin 3), SCL GPIO3 (pin 5), common ground, and module-compatible supply.
Buttons connect their configured BCM pins to ground with pull-ups:
up 26, down 21, left 20, right 16, back 19, OK 13. Check your actual hardware.
Enable I2C and reboot if the OS needs it before installing the OLED service.

```bash
sudo bash drone_project/drone_oled/deploy/install.sh
# Props removed: inspect UART before enabling the router.
sudo -u drone-router /opt/drone-oled/venv/bin/drone-oled-router \
  --config /etc/drone-oled/router.toml --inspect
```

Review wiring, UART ownership, serial-console state and GPIO pinmux; only then
set `hardware_confirmed = true` in `/etc/drone-oled/router.toml`.

```bash
sudo systemctl enable --now mavproxy.service
sudo bash drone_project/drone_oled/deploy/install-web.sh
./gcs
```

One router owns `/dev/serial0` at 115200. Local-only outputs are **14550 GCS**,
**14553 OLED**, and **14552 diagnostics**. No repeated `output add` is needed.
Do not launch another UART MAVProxy instance or two GCS instances on 14550.
Use only one interface for vehicle commands at a time: the UIs do not share
a cross-application flight-control lock. Leave the router running on GCS exit.

```bash
systemctl status mavproxy.service drone-oled.service drone-oled-web.service
journalctl -u drone-oled.service -u mavproxy.service -n 40 --no-pager
ssh -N -L 8090:127.0.0.1:8090 user@PI_ADDRESS
```

Open `http://localhost:8090` through the SSH tunnel. The provided web unit binds
localhost; no machine-specific IP or public control endpoint is required.

## Checks and controls

`M` selects a mission; selection is not a launch. `P` shows issues, measured
values and limits. Follow displayed confirmations and final Enter. `A`/`D`
are deliberate arm controls, not a mission shortcut. `Q` exits the monitor;
follow the on-screen recovery workflow during an active mission.
GPS/battery readings alone do not prove EKF position validity, working altitude
reference, RC recovery or failsafes. Flight gates remain enforced. New installs
must commission their own aircraft rather than inheriting this Pi's approvals.

## Tests and publication

```bash
drone_project/pi_flight/.venv/bin/python -m pip install -r requirements-dev.txt
bash scripts/test_all.sh
python3 scripts/prepare_public_release.py
```

The exporter creates a new, clean `public-release/` directory without copying
Git history, secrets, local configs, telemetry, backups, or recorded hardware
identity/approvals. It refuses to overwrite an existing release and scans for
common credential patterns. Review its output before publishing; automated
scanning cannot guarantee that a file is safe to make public.
See [PUBLICATION.md](PUBLICATION.md) for the push procedure and licensing notes.

No live autonomous flight has been validated by the installation or software
tests. Keep propellers removed during setup and bench diagnostics.

Large third-party simulator meshes/textures are not duplicated in the public
snapshot. Obtain them from the original simulation repository
`https://github.com/Sreeram-R-6/Drone-Sims` and check its asset licensing.
The Pi GCS/OLED does not need those simulator assets to run.
