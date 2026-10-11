# Drone OLED front panel

This is a separate service for your SSD1306 128×64 display. It includes your DVG
FPV splash and the six buttons on BCM **26, 21, 20, 16, 19, 13**. Internal pull-ups
are always enabled. The supplied configuration is **read-only**: it never arms,
starts a mission or spins a motor. Flight controls require a separately reviewed
firmware profile and explicit feature enablement.

## What is implemented

| Area | Behaviour |
|---|---|
| Splash/AOD | Full-screen monochrome DVG FPV image until systemd reports running/degraded; always-on by default |
| Read-only UI | Home, five status pages, menu, mission list/detail, failsafe and calibration views |
| Navigation | Safe confirmation focus, wrapped lists, debounce, long Back, repeats only for navigation, old-event suppression |
| MAVLink | Loopback UDP only, per-field freshness, identity verification, acknowledged commands, observed-state checks |
| Missions | JSON and QGC SimpleItem import, INT upload, readback verification, countdown, start, abort |
| Safety | No force arm/disarm, landed-state disarm guard, FC motor-test timeout, emergency preemption |
| Phase 3 | Profile-gated parameter read/write/readback; gyro/baro requests; compass progress and explicit save |
| Diagnostics | Offline PNG/ASCII renderer, read-only `.tlog` replay, button tool, real system monitoring |
| Deployment | Checked MAVProxy launcher, two systemd units, installer, rotating logs |

Six-position accelerometer and optical-flow calibration remain **GCS-only**:
their aircraft-specific interaction/cancellation protocol has not been
commissioned. Gyro/barometer ACKs are labelled as acceptance, not proof of
successful physical calibration. A compass result needs a separate save action.
No code fabricates FC failsafe strings, cell voltages, GPS/home status or readiness.

## Wiring: BCM GPIO numbers, not header pin numbers

Power off before wiring. A normally-open switch connects its GPIO to a GND pin.
Never wire a button GPIO to 5 V. Four-leg tactile switches have internally joined
pairs; check continuity so you use opposite switching contacts.

| Function | BCM GPIO | Physical pin |
|---|---:|---:|
| UP | 26 | 37 |
| DOWN | 21 | 40 |
| LEFT | 20 | 38 |
| RIGHT | 16 | 36 |
| BACK | 19 | 35 |
| OK | 13 | 33 |
| OLED SDA | 2 | 3 |
| OLED SCL | 3 | 5 |
| OLED VCC | — | 1: 3.3 V, verify module |
| OLED/buttons GND | — | 6 or another GND pin |
| Pi TX → FC RX | 14 | 8 |
| Pi RX ← FC TX | 15 | 10 |

Verify OLED connector order and FC logic levels. Connect signal grounds. Do not
connect an FC power pad to the Pi from this signal table. GPIO pull-ups are applied
in `GPIOButtons`; debouncing occurs once in `InputInterpreter`. Held-at-start
buttons cannot activate actions until every button has been released.

## First run on a development computer

```sh
cd drone_project/drone_oled
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/drone-oled --config config.toml --validate
.venv/bin/drone-oled --config config.toml --render-all --output previews
.venv/bin/drone-oled --config config.toml --render --screen boot --output splash.png
.venv/bin/drone-oled --config config.toml --ascii --screen home
.venv/bin/pytest
.venv/bin/ruff check src tests
.venv/bin/mypy src/drone_oled
```

Read recorded telemetry without opening a live link:

```sh
.venv/bin/drone-oled --config config.toml --replay ../../mav.tlog --screen status --page 2 --output gps.png
```

Interactive SSH emulator (type `left`, `right`, `ok`, `back`, etc., then Enter):

```sh
.venv/bin/drone-oled --config config.toml --keyboard --no-display --frame-output live.png
```

Keyboard mode always disables flight controls. PNG/ASCII/replay modes construct no
live MAVLink sender and never access GPIO/I2C. The five supplied missions are
display-only examples, visibly blocked from execution.

## Raspberry Pi bring-up

1. Record `cat /proc/device-tree/model`, `cat /etc/os-release`, `uname -a`.
2. Run `sudo raspi-config`: Interface Options → I2C → Yes. Serial Port → login
   shell **No**, serial hardware **Yes**.
3. On Bookworm, inspect `/boot/firmware/config.txt`. Enable:

   ```ini
   dtparam=i2c_arm=on
   dtparam=i2c_arm_baudrate=400000
   enable_uart=1
   ```

4. Pi 4: preserve your working `/dev/serial0` mapping first. Optional PL011
   migration: `dtoverlay=disable-bt`, disable `hciuart`, reboot and reverify.
   Do not make this change blindly if the existing UART is reliable.
5. Pi 5: add `dtoverlay=uart0-pi5`. Leave CTS/RTS off: CTS would conflict with your
   GPIO16 RIGHT button. Verify the header UART, commonly `/dev/ttyAMA0`;
   `/dev/serial0` may instead be the separate `/dev/ttyAMA10` debug UART.
6. Inspect `/boot/firmware/cmdline.txt` and remove only serial `console=...`
   tokens for the selected FC UART; preserve the single line and other options.
   Disable its serial getty. Reboot and verify:

   ```sh
   readlink -f /dev/serial0
   pinctrl get 14,15
   ls -l /dev/i2c-1 /dev/gpiochip* /dev/ttyAMA* /dev/ttyS*
   sudo i2cdetect -y 1
   ```

7. Verify OLED address `3c` and run the button test with no flight application
   active. An SSD1306/SH1106 mismatch can shift or corrupt the image; use the
   correct configured driver, not coordinate offsets as a workaround.
8. On the FC, identify the connected UART before reading `SERIALn_PROTOCOL` and
   `SERIALn_BAUD`. Expected MAVLink2/115200 encodings are `2`/`115`; saved repo
   evidence suggests SERIAL4, but this is not a live wiring verification.

References: [Pi serial configuration](https://www.raspberrypi.com/documentation/computers/configuration.html#configuring-uarts),
[official overlays](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README),
[GPIO pull-ups](https://gpiozero.readthedocs.io/en/stable/api_input.html#button).

## Install and start on the actual Pi

The installer changes the Pi, installs packages/users/units and enables the OLED
for the next boot. It does not change FC parameters, UART overlays or arming
settings; it does not overwrite existing configuration or mission files.

```sh
cd drone_project/drone_oled
sudo bash deploy/install.sh
sudo -u drone-oled /opt/drone-oled/venv/bin/drone-oled --config /etc/drone-oled/config.toml --button-test
sudo systemctl start drone-oled.service
journalctl -u drone-oled.service -f
```

Configure `/etc/drone-oled/router.toml` after props-off checks. Set the verified
UART device, then explicitly set `hardware_confirmed=true`. Stop old direct-UART
GCS/MAVProxy processes first. Inspect and start:

```sh
sudo -u drone-router /opt/drone-oled/venv/bin/drone-oled-router --config /etc/drone-oled/router.toml --inspect
sudo systemctl enable --now mavproxy.service
journalctl -u mavproxy.service -f
```

MAVProxy owns the UART. OLED listens on `udpin:127.0.0.1:14551`; the existing GCS
must use its supported `udpin:127.0.0.1:14550` endpoint instead of direct UART.
The existing `pi_flight` bounded diagnostic launcher is not replaced or modified.
Router startup refuses unknown pinmux, active getty, busy serial, root execution
and unexpected MAVProxy init scripts. MAVProxy's default periodic stream requests
and heartbeat are disabled; the OLED requests individual message intervals.

Read-only link check, with the router already running and OLED temporarily stopped
to free its UDP port:

```sh
sudo systemctl stop drone-oled.service
/opt/drone-oled/venv/bin/python -c 'from pymavlink import mavutil; c=mavutil.mavlink_connection("udpin:127.0.0.1:14551"); print(c.wait_heartbeat(timeout=5)); c.close()'
sudo systemctl start drone-oled.service
```

The UI account needs `i2c`/`gpio`; router needs `dialout`. Do not use `chmod 777`
on devices or run the UI as root. Use the OS's existing udev permissions; add
narrow rules only if device evidence shows they are missing.

## Splash timing and always-on limits

The splash is the first frame written by the Linux UI service. It persists until
systemd reaches `running` or `degraded`, with a 45-second escape timeout. OK skips
the image only. Boot completion is independent of FC/GPS availability. The UI
does not wait for WiFi, network-online or the router before showing the image.

**An SSD1306 connected only to the Pi cannot show this picture at the instant power
is applied.** The Pi must first execute software and initialise I2C/OLED. Earlier
display requires a separate controller and electrical bus handover design, which
is outside this wiring. The display is also cleared on graceful shutdown, even
if its supply remains present. Sudden power loss cannot be handled in software.

`always_on=true` disables automatic dim/blank. Optional saver settings exist, with
first wake key consumed; emergencies/armed/run/test/countdown remain visible.
Set contrast conservatively and inspect static OLED ageing on the real panel.

## Enabling aircraft commands

Do this only after simulator tests and props-off hardware commissioning. The
example profile is intentionally invalid/unapproved. Populate live identity,
reviewed battery/EKF limits, motor sequence mapping and recovery policy; no generic
pack-cell or low-voltage assumption is made. Set `firmware_profile` to that JSON
file and enable only the features commissioned. Do not copy test fixtures into a
hardware profile. Restart the UI after deliberate configuration changes.

The default expected vehicle is ArduPilot quadcopter. An unsupported identity can
be displayed diagnostically but cannot execute controls. Unknown command outcomes
block new flight starts for that process session; inspect the FC, reconcile the
state and restart deliberately. A UI restart never resumes a countdown/mission
start or sends an automatic mode/arming command.

Readiness requires fresh sensors/prearm/GPS/EKF/battery data. Six satellites is
only one check, not proof that navigation is safe. FC arming checks remain in
force. The existing saved FC setup contains unresolved source/failsafe decisions;
this package does not mark any existing commissioning flags approved.

Motor-test mapping is **test sequence 1..4**, not assumed FC output numbering.
Tests require a disarmed/landed aircraft, props-removal confirmation, a reviewed
position map, percentage ≤10%, and an FC timeout ≤3 seconds. Back sends a priority
zero-timeout stop. A broken Pi/FC link cannot guarantee immediate stopping; the
FC timeout is the independent fallback and must be demonstrated on the bench.

Running abort requests reviewed RTL, with LAND fallback when RTL prerequisites
are missing or RTL is rejected. It does not override a pilot/FC that already left
AUTO. In-flight disarm is never exposed. Emergency Back acknowledges only; OK
opens a safe-default LAND confirmation.

Relevant protocols: [commands](https://mavlink.io/en/services/command.html),
[missions](https://mavlink.io/en/services/mission.html),
[stream intervals](https://ardupilot.org/dev/docs/mavlink-requesting-data.html),
[Copter mission commands](https://ardupilot.org/copter/docs/common-mavlink-mission-command-messages-mav_cmd.html).

## Executable mission JSON

`items[0]` is the ArduPilot home placeholder; visible steps start at item 1. Each
item contains a command, frame and seven params. For frame 0/3, latitude/longitude
are degrees and are converted to INT frames 5/6. For frame 5/6, params 5/6 must
already be integer degrees×1e7. Unknown commands are visible but block execution.

```json
{
  "name": "EXPLICIT PLAN",
  "items": [
    {"command": 16, "frame": 3, "params": [0,0,0,0,10.2,76.3,0]},
    {"command": 22, "frame": 3, "params": [0,0,0,0,10.2,76.3,3]},
    {"command": 19, "frame": 3, "params": [30,0,0,0,10.2,76.3,3]},
    {"command": 21, "frame": 3, "params": [0,0,0,0,10.2,76.3,0]}
  ]
}
```

These are illustrative coordinates, **not a flight-ready route**. File name/name
and generated labels must fit the supplied uppercase font. QGC complex surveys
must be expanded first. Mission upload begins only after countdown T-0 and final
prechecks; a failed upload can still modify the FC mission and is reported as such.

## Validation and remaining commissioning

See `VALIDATION.md` for checks actually run, `tests/test_sitl.py` for owned-process
SITL integration, and `docs/commissioning.md` for hardware/soak/failure injection.
No deployment or physical flight result is implied by passing offline tests.

### Current Pi session

The detected machine runs **Ubuntu 22.04.5 on Pi 4**, rather than the planned Bookworm default. See [VALIDATION.md](VALIDATION.md) for the actual checks and remaining commissioning. The read-only OLED preview is running as a user service with your six button pins and internal pull-ups. The local configuration uses the measured 100 kHz bus and a 5 FPS cap. Install the system service with `sudo bash deploy/install.sh` for early boot; that installer transfers ownership from the user preview and keeps all flight controls disabled. UART pinmux is not yet verified, so FC routing is intentionally not started.

### Verified three-output routing on this Pi

The actual FC link has now been tested successfully through MAVProxy. Outputs are `127.0.0.1:14550`, `127.0.0.1:14552`, and **`127.0.0.1:14551` for the OLED**. Output order does not require consecutive UDP port numbers. To add the OLED to an existing interactive MAVProxy instance, enter `module load output`, `output list`, then `output add 127.0.0.1:14551` only if it is absent. Enter these in MAVProxy, not Bash. For persistence add `--out=udp:127.0.0.1:14551` to that instance's startup command, preserving its other outputs.

The tested headless startup command is in `deploy/mavproxy-verified.service`. Install that unit as `mavproxy.service` on this verified Pi, stop `drone-oled-router-preview.service` first, then enable/start the system unit. Only one process may own the UART. Existing applications configured to open `/dev/serial0` directly must instead use their own forwarded UDP endpoint. Flight controls remain disabled in the OLED.

### Updated OLED endpoint: 14553

The OLED's requested dedicated endpoint is now **`udpin:127.0.0.1:14553`**. Prepared configurations and the verified router unit use this port; older examples above mentioning OLED port 14551 are superseded. Other router outputs remain 14550 and 14552. Update `/etc/drone-oled/config.toml` and install the updated verified router unit before restarting both system services. Stop the transient preview router before starting the system router. For interactive access, stop the system router first, launch MAVProxy with all three outputs and the output module, then restore the system router after exiting.

### FC-managed arm/disarm

`[safety] arm_policy = "fc_managed"` permits deliberate arm/disarm requests without a commissioned hardware identity or Pi-defined battery/GPS readiness thresholds. It still requires a fresh ArduPilot Copter heartbeat, a disarmed/landed state before arming, safe-default confirmation, normal command 400 with force parameter zero, acknowledgement plus observed armed state, and confirmed landed state before disarming. The FC decides whether its own arming conditions pass. No FC safety parameters are changed. Home's conservative readiness indicator is advisory under this arm policy and may remain NOT READY without a commissioned profile.

Apply with `sudo bash deploy/enable-fc-managed-arm.sh`. This updates only the OLED package/configuration/service, leaves the MAVProxy unit and UDP 14553 intact, and does not send an arm request. Mission starts, motor tests, calibration and parameter writes remain disabled; their reviewed-profile requirements are unchanged. A separate `firmware_profile` file is unnecessary for this arm policy. The setup backs up the previous config under `/etc/drone-oled/config.toml.before-fc-managed-arm.*`.

### Local OLED website

Install the four-button mirror using:

```sh
cd /home/user/all_drone/drone_project/drone_oled
sudo bash deploy/install-web.sh
sudo systemctl status drone-oled.service drone-oled-web.service --no-pager -l
```

On the Pi, open **http://localhost:8090**. On your laptop, keep this SSH tunnel open:

```sh
ssh -N -L 8090:127.0.0.1:8090 user@PI_ADDRESS
```

Then open **http://localhost:8090** on the laptop. Replace the Pi IP if it changes. Both services start at boot; the SSH tunnel is only needed to view the localhost page remotely.

The page mirrors the exact 128×64 one-bit framebuffer at the OLED render rate (currently 5 FPS) and marks display-write failures/disconnections. It has PREV, NEXT, OK, BACK. PREV/NEXT select Home blocks, move through lists/pages, and cycle all four motor cells. Physical buttons and browser clicks share the same FSM, confirmations, stale-link guards and command permissions. No arm request is sent by installation. The install preserves MAVProxy routing, UDP 14553, FC-managed arming and existing action flags.

The web service listens on 127.0.0.1 only and communicates with the OLED process over an owner-only Unix socket. Browser button requests require the page's token and matching localhost origin. Duplicate, stale-screen, excessive-rate and old queued clicks are rejected; presses never auto-repeat. When the OLED service is unavailable the page shows disconnected and disables its buttons.

Logs: `sudo journalctl -u drone-oled-web.service -u drone-oled.service -f`. UI logs record accepted virtual key events; telemetry and physical input remain in the existing OLED process.

### Direct Tailscale access

The supplied web unit binds localhost. Use the SSH tunnel above for remote
access. For deliberate Tailscale-only access, configure a systemd override
with the Pi's own verified address and an explicit `--allow-host`; do not copy
another Pi's address or expose flight-control buttons to the public Internet.
