# Pi / real flight-controller bring-up

The new [terminal GCS and flight timeline](FLIGHT_TIMELINE.md) provides mission
selection, a reviewed 5-second hold, small heading-relative routes, telemetry,
required-check pages, home map, ground-only DISARM and gated RTL. Preview with
`bash pi_flight/launch_gcs.sh --offline` from the project root. Commissioning
gates remain unresolved; offline software validation does not release flight.

The workspace-root `./gcs` now shares **UART telemetry with the OLED**.
Boot-enabled `mavproxy.service` alone opens `/dev/serial0` at **115200 baud**;
its persistent loopback outputs feed the GCS on **14550** and OLED on **14553**.
`config.toml` saves `udpin:127.0.0.1:14550`: no manual `output add` is needed.
Leave the router and OLED services running when opening `./gcs`. Closing the
GCS does not stop either service. Do not start another UART MAVProxy instance.
Checks and flight remain explicit, separately gated modes. Use only one UI
for vehicle commands at a time; shared telemetry is not a shared control lock.
`config.example.toml` retains standalone direct-UART operation for deployments
without the router/OLED. Never use that mode while the router owns the UART.
Press **R** in the dashboard to close/reopen and retry the configured connection.
UART-open/ownership errors are shown as CONNECTION ERROR and saved as
`connection_error` events in the session report. Reconnect never starts a
mission or sends an arm command. Close other UART owners yourself first.
The UART dashboard now sends only a GCS heartbeat and telemetry-stream requests
after receiving a valid configured FC heartbeat. It cannot arm, change modes,
write parameters, or send movement targets. Status distinguishes an FC heartbeat
from received sensor streams; missing/busy UART and mismatched FC IDs are shown
explicitly. Passive Transport monitoring remains available for diagnostic tests.
The aggregate `configuration/evidence` check and its blanket unresolved-setting
gate have been removed. Individual live sensor, identity, firmware, parameter,
route-review and operator-confirmation gates still apply; their failures must
not be treated as flight readiness.
Individual checks with unset acceptance limits now report UNKNOWN with the
missing field names, rather than FAIL implying a broken sensor. The details
panel includes measured evidence. Valid RC PWM channels are checked separately
from signal strength; unavailable RSSI (255) is informational, not an unbound
receiver diagnosis. The RSSI/LQ checklist entry has since been removed.
Legacy OPTICAL_FLOW readiness now evaluates opt_m_x/opt_m_y (compensated X/Y),
opt_qua, sensor ID, stream freshness/rate and stationary magnitude against
flow.max_compensated_m_s, without requiring RAD-only integration fields.
Axes, scale, compensation and fusion approval remain a separate flight gate.
Optical-flow telemetry and flow-stream checks are now omitted from the main
dashboard checklist at operator request. The values remain displayed, and
autonomous mission preflight still performs the flow checks. GPS check details
now list the precise failing criteria and worst fix/satellite/HDOP readings
across the sampled window, not just the most recent packet.

Manual arm control is separate from mission preflight: press **A** from the
dashboard to enter **ARM MODE**, then press **A** again to request normal arm.
After an armed FC heartbeat is confirmed, **A** requests normal ground disarm.
**Esc** returns to the main dashboard without disarming; **Q** disconnects.
The screen requires a new key after entry/each command, verifies fresh heartbeat
and on-ground telemetry, and leaves FC arming checks enabled (no force command).
Blocked commands remain visible in ARM MODE. No mission, takeoff, mode change,
parameter write or movement target is sent. This manual mode does not perform
the autonomous mission's full sensor/firmware preflight; arming is not evidence
of readiness for autonomous flight. Keep clear: motors may spin when armed.

Dashboard maintenance (disarmed FC only): **F** downloads the complete parameter
list, displays failsafe/arming settings and requests FC pre-arm checks (command
401). It saves FC status text and sensor-bit diagnostics under `pi_flight/logs/`.
This is reported telemetry, not a guarantee that every internal FC check is
exposed or that a command ACK means all checks passed. **C** toggles check
details; **↑/↓** selects individual checks; **PgUp/PgDn** changes pages.
**Esc** returns from maintenance and **Q** exits the GCS.

The RC display/check uses **CRSF link quality (LQ)**, not RSSI. The dashboard
reads `RC_OPTIONS` and `RSSI_TYPE` without changing them. ArduPilot must expose
LQ using `RSSI_TYPE=3` and `RC_OPTIONS` bit 11 (2048), preserving every other
option bit. The shared MAVLink field is decoded as LQ only after that mapping
is verified from live parameters; otherwise the UI says **not reported**.
The ArduPilot CRSF mapping uses `round(LQ * 2.5)`, so raw 250 means 100%, while
255 is not treated as a valid full-quality CRSF measurement. Missing LQ is
informational; RC channel validity and sensor health remain separate gates.

For deliberately tolerant **read-only bench diagnostics**, run `./gcs --bench`
from the workspace root. Limits are defined in `bench_limits.toml` and applied
only to the runtime copy; the flight configuration is unchanged. The dashboard
is explicitly marked BENCH ONLY / FLIGHT DISABLED. Motor commands, mission
launches, parameter writes and FC log erase are blocked. Bench EKF diagnostics
check attitude and vertical position, not horizontal flight readiness. Missing
RAD integration evidence and powered flight-log review are informational in
this profile only. These limits are not calibrated flight acceptance criteria.
The normal deployment config also now contains provisional numeric diagnostic
limits instead of zeros. Autonomous preflight separately requires flight-limit
review (`commissioning.thresholds_approved`), still false; do not mark it true
merely because telemetry passes permissive thresholds. Real hardware/log
review has not been performed. Flight-log review is informational on the main
monitoring dashboard but remains required for autonomous mission preflight.

CRSF LQ remains visible on the dashboard, but its checklist entry has been
removed entirely. RC channel validity and FC receiver-health checks remain.

**L** downloads all FC DataFlash logs to `pi_flight/fc_logs/<session>/`, with
byte-completeness checks, local SHA-256 hashes and a manifest. Interrupted logs
remain `.bin.part` and cannot authorize erasure. It never clears FC logs.
**X** requests clearing **all FC logs**: a complete latest backup, matching FC
UID and unchanged log inventory are required. The aircraft must be confirmed
on the ground and disarmed. Type **ERASE LOGS** at the confirmation prompt.
The inventory is rechecked after confirmation and erasure is verified by an
empty FC log list. Local backups and GCS reports are never removed. Download
again if any FC log was added or changed. Unavailable FC UID, unsupported log
protocol or incomplete transfer blocks erasure. No erase is sent automatically.

In the dashboard, press **M or Enter** on SELECT MISSION, choose a route, and
press Enter to return to the main dashboard and begin preflight. The dashboard
shows the selected mission and **WAITING FOR CHECKS** with start locked. After
all checks and human confirmations pass, **READY / PRESS ENTER** appears inline
on the dashboard. A fresh Enter starts the final recheck and flight.
Failed/unknown checks keep flight locked; after a blocked or finished mission,
the dashboard resumes live UART monitoring.

**Current status: direct FC UART telemetry verified; real flight LOCKED.** A
passive connection received heartbeat, attitude, position, battery, RC, range,
EKF and other messages with the FC in STABILIZE and disarmed. No arming, mode,
parameter or flight command was sent. Sensor/firmware commissioning remains
incomplete. Settings persist across launches/reboots; no boot service is installed.
Use `./gcs --offline` for a preview that opens no UART or UDP socket.

## Archived router bring-up report

The report below describes the earlier MAVProxy/UDP deployment. Its
two-process router instructions and old connection status are historical;
the current direct-UART commands are in [FLIGHT_TIMELINE.md](FLIGHT_TIMELINE.md).

The repository was cloned from `https://github.com/Sreeram-R-6/Drone-Sims.git`,
commit `ead7f0409e1e702bffad3f6a3ce1e4d4a30df95e`. The supplied reference paths
`drones/rescue_7inch/CALIBRATION.md` and `start/takeoff_hold.py` are absent at this
commit. The available README/status/cross-check, drone README/parameters and ROS
mission were inspected. ROS takes off to 10 m and is outside this workflow.
No existing repository files were modified.

| Component | Evidence / status |
|---|---|
| Pi | Raspberry Pi 4 Model B Rev 1.5, BCM2711 |
| OS | Ubuntu 22.04.5, aarch64, 64 bit |
| Kernel | 5.15.0-1106-raspi |
| Python | 3.10.12; project `.venv` |
| MAVProxy / pymavlink | 1.8.74 / 2.4.49 |
| PyYAML / future / tomli | 6.0.3 / 1.0.0 / 2.4.1 |
| Terminal | SSH, tmux 3.2a; no desktop |
| Boot configuration | `/boot/firmware/config.txt` already enables UART3 and UART5; no serial console is configured |
| Pi-to-FC UART | GPIO14/15: mini-UART serial0, `/dev/serial0` → `/dev/ttyS0` → `serial@7e215040`; DT function 2 |
| Bluetooth / serial alias | PL011 Bluetooth is `/dev/ttyAMA0`; `serial0` getty is inactive |
| Pinmux | Physical register/tool verification outstanding; device tree is not proof |
| Serial ownership | UART3 getty inactive; no holders visible to this user; dialout access present |
| FC | User-reported BLITZ Mini F745; exact target/version/UID not read |
| Flow/range | User-reported original MTF-01; actual sensor commissioning incomplete |

The operator explicitly chose to **keep the UART device physically unconfirmed**.
Both configs now identify GPIO14/15 header serial0 as `/dev/serial0`, while retaining
`device_confirmed=false`.
All hardware launchers refuse this setting. Do not create a symlink to disguise
the mapping. GPIO14/15 serial0 is already active, and Bluetooth is on ttyAMA0; no boot,
Bluetooth, console, or getty setting has been changed.

## Wiring, only after props-off confirmation

Disconnect Pi power and LiPo before wiring. Confirm props removed immediately
before any powered work. GPIO UART uses non-inverted **3.3 V TTL, not 5 V**.
There is **no power pin** in the Pi–FC three-wire connection.

| Pi physical pin | Function | FC pad |
|---|---|---|
| 8 / GPIO14 | Pi serial0 TX | Physical UART4 RX |
| 10 / GPIO15 | Pi serial0 RX | Physical UART4 TX |
| 6 | Ground | Ground |

The MTF-01 connects independently to FC physical UART5 with crossed data lines
and its documented power supply. Its supply design is not the Pi data connection.
Physical UART labels do not establish ArduPilot SERIAL parameter numbering.

```text
verified Pi serial0 device → MAVProxy (pane 1, sole serial owner)
                           ↓ loopback UDP 127.0.0.1:14550
                         preflight_hover.py (pane 2)

MTF-01 → FC UART5 (separate from Pi link)
```

MAVProxy's outbound UDP socket is also explicitly bound to loopback, because it
receives commands from the mission. There is no GCS network output. Per-user
locks in `/tmp/pi-flight-UID` prevent duplicate cooperating launchers across
checkouts. `fuser` also checks other visible serial holders. This cannot prevent
an unrelated program opening the port later; keep all other serial software off.

## Commands available now

From the project root:

```bash
bash pi_flight/install_pi.sh
bash pi_flight/inspect_uart.sh --offline
bash pi_flight/configure_uart.sh --offline
bash pi_flight/test.sh
bash pi_flight/launch_mission.sh --offline
bash pi_flight/launch_mavproxy.sh --offline
bash pi_flight/start_tmux.sh --offline
```

Inspection/configuration currently return **3** for the unconfirmed UART mismatch.
This is an intentional failure, not a request to change the node automatically.
The offline checklist returns 0 to indicate the offline run completed, while
printing FAIL/UNKNOWN and FLIGHT LOCKED. It is not a flight PASS.

`start_tmux.sh` creates `pi-flight` with exactly two titled panes; an existing
session is reattached without starting anything new. An offline session has
inert shell prompts after both commands finish. Detach with Ctrl+B then D.
Reattaching does not change an offline session into a live one. `--detach` allows
testing the offline layout without attaching. No process respawns.

Installation uses apt only for missing OS prerequisites and pip only in `.venv`.
Resolved versions are in `requirements.lock.txt`; downloaded artifact hashes
are in `requirements.artifacts.sha256`, with cached wheels in ignored
`wheelhouse/`. ARM64 wheels were available, so no extra compiler was installed.
MAVProxy imports `future` at CLI startup even though its package metadata did
not install it; it is explicitly included. No GUI plotting packages or desktop
were installed. Reruns use the exact lock. For an internet-disconnected reinstall
after OS prerequisites exist, run from the project root:

```bash
pi_flight/.venv/bin/python -m pip install --no-index --find-links pi_flight/wheelhouse -r pi_flight/requirements.lock.txt
```

## Later props-off connection — not performed yet

First confirm the actual GPIO TX/RX function with `pinctrl get 4 5` or
`raspi-gpio get 4 5` once the appropriate tool is installed. Then explicitly
confirm the resolved device and wiring/props-off checklist in `config.toml`.
The config must record evidence, not a guessed approval. If boot changes become
necessary, `configure_uart.sh --apply` checks the detected Pi, local overlay
help and confirmed node, backs up the boot file, and uses a managed block. It
refuses conflicting existing settings/includes. Only an exact selected-device
getty may be disabled. Reboot manually when requested, then inspect again.

After confirmation, the bounded **checklist-only** entry point is:

```bash
bash pi_flight/start_tmux.sh --check-once
```

Alternatively, use `launch_mavproxy.sh --check-once` and
`launch_mission.sh --check-once` in separate SSH sessions. The mission binds only
loopback and waits for a valid heartbeat, which synchronizes the panes without
an arbitrary startup sleep. First-connect deadline defaults to 5 seconds.
`--wait-for-fcu --timeout 30` explicitly permits a longer bounded wait. Neither
launcher retries forever. `--check-once` limits connection establishment; once
connected, a checklist includes bounded stream requests, a parameter export and
the stationary sampling window. MAVProxy then remains the live telemetry router
until stopped or its link goes stale.

Exit codes: **0** completed operation; **2** usage/environment error; **3** safety
gate/configuration failure; **4** NOT CONNECTED. A disconnected command prints
NOT CONNECTED and exits; installation never invokes it. Root mission/proxy/tmux
execution is refused.

The first connected checklist saves AUTOPILOT_VERSION and a complete parameter
snapshot with count/index checks (60-second export deadline). Missing packets
produce an explicitly incomplete export and block progression. It does not write
FC parameters. Inspect firmware target/board evidence separately; an ArduPilot
quad heartbeat does not by itself distinguish real hardware from SITL.

## Configuration and required evidence

`config.toml` contains the confirmed physical profile and is ignored by Git.
`config.example.toml` documents all fields. Zero limits and negative sensor IDs
mean **unreviewed**, never unrestricted. The 5 m reported clear radius fails the
15 m gate. The firmware, parameter profile, live identities, sensor limits and
commissioning evidence deliberately remain unset.

| Check | Measurements / required review |
|---|---|
| Link | target system/component, MAVLink2 heartbeat, arrival rate, age, maximum gap |
| Identity | ArduPilot quad, exact version/board/UID, real target evidence, disarmed, expected launch mode |
| Safety parameters | live name/value/type readback against a firmware-matched profile; ARMING_CHECK=1 |
| GPS | 3D fix, satellites, known HDOP, WGS84 NE conversion, median center, RMS/p95/max/drift/step/speed; no outlier removal |
| Positions | global/local freshness, home/origin, altitude stability, reboot/origin discontinuities |
| EKF | attitude/HV/VV/absolute HP/VP flags; no constant-position/uninitialized state; bounded variances |
| Flow | correct ID, stream gaps, integration duration, finite flow/gyro, quality min/median, compensated stationary bias |
| Range | correct ID, downward orientation, measured outdoor envelope, covariance known and within limit |
| Battery | calibrated 4S voltage/current/remaining, unknown sentinel values rejected |
| Attitude | stationary tilt mean/RMS/max, heading span, human real-direction check |
| RC | fresh channels, plausible first four channels, known nonzero RSSI, separately verified mode switch |
| Faults | latched STATUSTEXT prearm/failsafe/error indications; no automatic clearing |
| Vibration | telemetry recorded if available; manual powered-hover log review mandatory |
| Human | individual timestamped confirmations of props, frame, power, sensors, surface, area, people, RC, pilot, observer, heading, wind, permission and loose leads |

All critical WARN and UNKNOWN states block flight. Ordinary bench telemetry
cannot prove optical-flow axes, calibration, outdoor range, correct compass
heading, or actual EKF fusion. Those require the saved commissioning report and
firmware-specific review. The current range gate conservatively rejects unknown
covariance; if MTF-01 does not supply it, review its actual quality semantics
before implementing a documented alternative. Do not invent values to pass.

Proposed timing/mission thresholds (not a validated aircraft envelope): full
sampling 30 s, final sampling 3 s, heartbeat minimum 0.7 Hz, position and sensor
streams minimum 2 Hz, maximum message age 2 s. Mission: 2 m AGL, 0.30 m/s climb,
±0.20 m settle/hold band, vertical speed ≤0.15 m/s to settle for 3 s, then 10 s
valid hold; position targets 5 Hz. Abort proposals: altitude >2.5 m, horizontal
radius >2 m, tilt >15°, horizontal speed >1 m/s, vertical speed >0.60 m/s, or
altitude disagreement >0.40 m. Command ACK deadline 5 s; takeoff/settle deadline
35 s; disarm deadline 120 s. These margins are explicit review proposals, not
SITL-derived approval. Every GPS/flow/battery/stationary threshold still needs
approval based on live firmware parameters, environment and measured sensors.

Create a reviewed JSON `parameter_audit.profile_path` after reading live values.
Its `flight_sw_version` must equal the recorded integer version. `categories`
must contain nonempty maps for `arming`, `serial_pi`, `serial_flow`, `ekf`, `flow`,
`range`, `battery`, `rc_failsafe`, `gcs_failsafe`, `ekf_failsafe`, `fence`, `rtl`,
`landing`, `climb`. Each map has parameter names with `{"value": ..., "type": ...}`.
Review completeness against the actual release, including active EKF source set,
GPS_HDOP_GOOD, calibrated battery actions/thresholds, RC switches, land speeds,
fence behavior and GCS source identity. Category names alone do not prove that
the profile covers the right firmware parameters. Any proposed FC change must
show old value, new value, units, effect, source and rollback and receive explicit
operator approval. This workflow never uploads repository parameter files.

The mission emits GCS heartbeat as system 254, component 191; MAVProxy's own
heartbeat is disabled so it cannot hide mission failure. Review and test which
GCS identity the exact FC firmware monitors and verify that losing the mission
process and losing the whole Pi both invoke LAND. Do not assume the settings
are correct from their names. Existing radio/battery/EKF/fence behavior also
needs review. Optional RTL remains a pilot navigation-dependent option only.

## Flight lifecycle — commissioning release still pending

`--flight` is an explicit additional flag to the bounded connected launcher. It
is currently refused before opening telemetry because commissioning is incomplete.
Do not enable it or install props based on software tests. Before release:

1. Confirm exact firmware/target and FC UART4/UART5 SERIAL mappings.
2. Save props-off wiring, UART pinmux and bidirectional non-actuating link evidence.
3. Confirm MicoAssistant output/ID/rate; audit sensor parameters without writing.
4. Commission translation/rotation signs, gyro compensation, height changes,
   range envelope and optical-flow calibration; review dataflash evidence.
5. Review manual hover, real tune, vibration, battery calibration and all failsafes.
6. Test STABILIZE / ALT_HOLD / LOITER recovery switches and mission release;
   confirm pilot/observer/weather/permission and at least 15 m controlled radius.
7. Review the exact firmware implementation of slow GUIDED takeoff and altitude
   reference. `climb_parameter` is intentionally blank; do not assume WPNAV_SPEED_UP
   controls this release. Record units, type, range and source. Approve the exact
   temporary change. Validate the live mission adapter props-off without arming.

After all automated checks and human confirmations pass, the final fresh Enter
starts a new parameter audit and fast sample. Any text cancels. Telemetry keeps
being processed during prompts, and old buffered keyboard input is discarded.
The Enter also reconfirms that the area and pilot/observer remain ready.

GUIDED must be acknowledged and observed. Normal arming needs accepted ACK and
armed heartbeat. The original NE point and altitude baselines are captured
before arming. Local NED target is `(original_N, original_E, original_D - 2)`;
mask 3576 ignores velocity, acceleration, yaw and yaw rate. Firmware review must
establish whether takeoff altitude is relative to home or above launch. A
temporary climb parameter is journaled, checked by type/value and restored only
after disarm. Hold starts after settling; leaving its band fails the test.

Ctrl+C/SIGTERM request cleanup, not immediate exit. A fault while still owning
GUIDED requests LAND once. Pilot/FC mode changes stop mission commands and enter
read-only monitoring. Link loss stops sending and reports LAND unconfirmed;
the already verified FC failsafes and pilot own recovery. No force-arm, force-
disarm, RTL recovery, or in-air disarm path exists. A disarm timeout fails the
test and continues read-only monitoring while the armed live link remains.
Repeated Ctrl+C cannot skip that monitor. A missing restoration is a failed test,
with the original value/type in `restore-parameter.json` for later reviewed recovery.

## Reports and troubleshooting

Each run has an independent timestamp/UUID directory in `logs/`, containing
configuration, JSON checklist, text checklist log and flushed `events.jsonl`.
Connected runs also record raw received messages, GPS JSON/CSV, version metadata,
parameter snapshots, commands, state transitions and restoration journal. MAVProxy
telemetry logs are under `logs/mavproxy/<run>/`. Offline output is intentionally
FAIL/UNKNOWN for hardware. Only COMPLETE, successful landing/disarm/restoration
can produce a flight PASS. A safe landing after an abort is still a failed test.

| Symptom | Action |
|---|---|
| No device / unexpected node | Inspect DT mapping and overlay; confirm actual node, never fake a symlink |
| Permission denied | Check current dialout membership; log out/in after adding it |
| Port busy | Stop only the identified competing owner; never open another serial client |
| NOT CONNECTED | Check props-off crossed wiring, mapped FC protocol/baud; rerun only deliberately |
| Wrong ID / MAVLink1 | Audit exact FC identity and Pi SERIAL settings; do not weaken identity gates |
| Prearm failure | Read ACK/STATUSTEXT and fix the cause; never arm as a diagnostic |
| GPS drift | Recheck stationary placement, sky view, multipath, HDOP and antenna installation |
| Poor flow / invalid range | Verify MTF-01 setup, texture, lighting, orientation and outdoor range |
| EKF unhealthy | Review source selection and innovation logs before changing anything |
| Battery failure | Verify actual charge and calibrated readings/failsafes |
| Mode/arm rejected | Preserve exact ACK and text; remain disarmed or recover if arm state ambiguous |
| tmux issue | Use SSH `tmux attach -t pi-flight`; no graphical emulator is required |
| Does not disarm | Pilot intervention; continue monitoring, never force-disarm in air |
| Parameter restore failed | Keep aircraft grounded; use saved original value/type in a reviewed repair |

## Removal and verification boundaries

No boot file was edited in this build. Future boot edits save an exact adjacent
timestamped backup; compare it with the current file before restoring so newer
unrelated settings are preserved. Reboot after a reviewed restore. Remove only
this project's `.venv`/wheelhouse if desired, after stopping its processes;
retain logs and config as evidence. Apt prerequisites may be shared by other
software; do not blindly uninstall them or autoremove unrelated packages.

Software/unit tests and offline tmux are verified separately from props-off
hardware, manually commissioned aircraft and supervised real flight. The latter
three are **not verified by this build**. Reported manual-hover commissioning is
operator information pending log review. No claim of real hover performance is
made. Firmware-specific integration remains blocked until the actual FC is read.

## Primary references consulted

- [Pi UART documentation](https://www.raspberrypi.com/documentation/computers/configuration.html#configure-uarts)
- [BCM2711 UART register mapping](https://github.com/raspberrypi/linux/blob/rpi-5.15.y/arch/arm/boot/dts/bcm2711.dtsi)
- [MAVProxy installation](https://ardupilot.org/mavproxy/docs/getting_started/download_and_installation.html)
- [MAVProxy CLI](https://ardupilot.org/mavproxy/docs/getting_started/starting.html), checked against installed 1.8.74 `--help` and source
- [BLITZ board](https://ardupilot.org/copter/docs/common-blitz-f745.html)
- [Telemetry ports](https://ardupilot.org/copter/docs/common-telemetry-port-setup.html)
- [Prearm checks](https://ardupilot.org/copter/docs/common-prearm-safety-checks.html)
- [Preflight checklist](https://ardupilot.org/copter/docs/checklist.html)
- [MTF-01](https://ardupilot.org/copter/docs/common-mtf-01.html)
- [Flow setup/testing](https://ardupilot.org/copter/docs/common-optical-flow-sensor-setup.html)
- [EKF sources](https://ardupilot.org/copter/docs/common-ekf-sources.html)
- [GUIDED commands](https://ardupilot.org/dev/docs/copter-commands-in-guided-mode.html)
- [MAVLink definitions](https://mavlink.io/en/messages/common.html)
- [Parameter protocol](https://ardupilot.org/dev/docs/mavlink-get-set-params.html)

The current MTF-01 guidance uses mav-apm output, a MAVLink1 sensor port and
version-dependent forwarding/system-ID precautions. It also warns about reversed
orientation feedback. These are audit hypotheses pending the exact live release,
not permission to write parameters. No documentation example that disables any
arming check is adopted by this system.
