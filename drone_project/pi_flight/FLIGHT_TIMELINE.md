# Supervised flight timeline and terminal GCS

Software is implemented and tested; direct UART telemetry is verified.
**Aircraft readiness is UNKNOWN;
real flight remains LOCKED.** A program cannot establish motor order, correct
propellers, sensor mounting/calibration, a good tune, an obstacle-free volume,
or working radio recovery from bench telemetry alone. Enter starts a mission
only after commissioning evidence, live checks, individual human confirmations
and a final fresh recheck all pass. The passive UART check observed STABILIZE
and disarmed state. No arming, mode changes, parameter writes or flight commands
were sent.

## Repository findings that affect flight

| Area | What the current source does | Consequence |
| --- | --- | --- |
| `pi_flight/` | Strict identity, parameter, stationary sensor and human gates; 2 m slow takeoff, hover, LAND, automatic disarm, parameter restoration | Foundation for the new GCS |
| `picode/testforward.py` | Separate 2 m forward/RTL controller with different gates | Do not run beside this GCS; both would own UDP 14550 |
| UART documentation | `picode/README.md` says GPIO4/5 and ttyAMA2; the later `pi_flight` config says GPIO14/15 and serial0, still unconfirmed | Physically verify the actual installation; do not mix instructions |
| `geotag_mission.py` in ROS | 10 m takeoff; 15 m by 10 m search, geotag approach, centering, descent, QR transmission, RTL | Separate later mission, outside this 2 m commissioning envelope |
| `autonomous_mission.py` in ROS | 8 m takeoff, search, target centering/descent, QR transmission, RTL | Also outside the initial test envelope |
| Simulation models/worlds and parameter files | Gazebo/SITL assets, including archived geometry versions | Simulation parameters and successes are not hardware release evidence |
| Simulation launcher | Has process-kill patterns including MAVProxy on port 14550 | Do not launch simulation during hardware operation |
| Vision and QR server | Camera/QR consumers and data display | They do not establish flight/sensor readiness |

The GCS runs one mission at a time over direct UART, protected by per-user
mission and resolved-device serial locks. The original `preflight_hover.py` entry point retains
its 10-second hover; the GCS runtime uses a separately reviewed 5-second hold.

## Commissioning order before any autonomous launch

1. **Unpowered inspection:** verify frame, motor/prop choices, wiring, sensor
   mounting, battery restraint and UART voltage. Record actual wiring.
2. **Props removed, powered connection:** verify Linux UART pinmux/device,
   sole MAVProxy ownership, crossed TX/RX and ground, exact FC target/version/UID,
   and UART-to-ArduPilot SERIAL mappings. Save the version/parameter exports.
3. **Props removed, sensor commissioning:** accelerometer/gyro and compass
   calibration; heading agrees with the real direction; MTF-01 translation and
   rotation signs, compensation, scale, sensor ID, flow quality and message rate;
   range against measured distances and known covariance/quality semantics;
   battery voltage/current against instruments; GPS stability; actual EKF source
   fusion. Establish thresholds rather than filling unknown values with guesses.
4. **Props removed, recovery commissioning:** RC switches, FC pre-arm checks,
   battery/RC/EKF/fence/GCS-loss behavior, loss of the mission process and loss of
   the Pi. Review the firmware-specific climb parameter and altitude reference.
   Verify RTL home, climb/clearance and final landing behavior separately.
5. **Supervised manual hover:** review tune, vibration, current draw, sensor/EKF
   logs, stability and available recovery modes. Save evidence in the configured
   commissioning report and exact firmware-matched parameter audit profile.
6. **Site and software gates:** controlled clear radius at least 15 m, suitable
   textured/illuminated surface, pilot/observer and RC ready. Run checklist-only.
   Resolve every required FAIL, WARN and UNKNOWN before attempting a flight.
7. **Release routes:** review `[gcs]` proposals in `config.toml`, including the
   5-second hold, 1 m route legs, speed/tolerances and timeout. Record evidence
   before setting `routes_reviewed=true`. `rtl_reviewed` is a separate gate.

The supplied config still has unconfirmed UART, unknown firmware identity,
unreviewed thresholds, missing report/profile and incomplete commissioning.
Changing just the GCS booleans cannot bypass these existing gates.

## Mission timeline

Select `hover`, then graduate to `forward`, then `box` after reviewing each
flight's logs. The GCS movements are **1 m**, chosen to fit the existing 2 m
abort radius with margin; the old independent forward script uses 2 m.
Forward/right refer to yaw captured immediately before arming. Targets use
the launch position in local NED; the displayed map uses FC home and global
position, which need not be the same point as the EKF origin.

Before Enter: connect within the configured deadline, request streams, download
parameters completely (60-second deadline), audit live parameters, sample the
stationary aircraft for 30 seconds, then collect the human confirmations.
Queued keyboard input is discarded before each prompt. At final Enter, renew
the area/pilot/observer confirmations, audit live parameters again and sample
for 3 seconds. Failure here cancels without arming. Parameter audit time is
additional and varies with the link; it is not a fixed 3-second launch delay.

Times below start **at takeoff**, after accepted/observed GUIDED and normal
arming, and are approximate for the default 0.30 m/s proposal.

| Approximate time | Action | Required evidence to advance |
| --- | --- | --- |
| 0–6.7 s | Slowly climb to 2 m above launch | All required flight streams healthy; altitude/radius/tilt/speed limits valid |
| 6.7–9.7 s | Settle at 2 m | Continuously within ±0.20 m and vertical speed ≤0.15 m/s for 3 s |
| 9.7–14.7 s | **Hold position for 5 s** | Altitude band, XY launch tolerance and horizontal speed all valid; leaving hold band fails |
| 14.7–18.0 s | Forward 1 m | Position target advances at proposed 0.30 m/s, sent at 5 Hz |
| 18.0–21.0 s | Settle at waypoint 1 | Measured XY error ≤0.20 m, horizontal speed ≤0.15 m/s and valid altitude/vertical speed for 3 s |
| 21.0–26.0 s | Hold waypoint 1 for 5 s | Continuous valid hold; failure requests recovery |
| 26.0–37.3 s | Next 1 m leg, settle 3 s, hold 5 s | Same measured arrival/hold gates |
| 37.3–48.7 s | Box only: backward leg, settle, hold | Same gates |
| 48.7–60.0 s | Box only: left leg to launch, settle, hold | Same gates |
| After final hold | LAND | Accepted command and observed LAND mode; FC handles descent |
| After landing | Wait for automatic disarm, restore temporary climb parameter | Fresh disarmed heartbeat and verified restoration; only then COMPLETE |

`hover` goes directly to LAND after the initial 5-second hold. `forward` goes
forward, then backward to launch and LAND. `box` goes forward, right, backward,
left and LAND. Each route waypoint holds for 5 seconds after settling.
Slow response shifts all later times. Takeoff/settle deadline is 35 seconds;
each route leg including settle/hold has a 30-second deadline. Landing and
disarm are observed, not inferred from elapsed time. Exceeding the 120-second
disarm deadline marks failure and keeps monitoring a connected armed vehicle
for pilot intervention.

The target advances at 0.30 m/s; this is **not a guaranteed physical speed
limit**. The existing measured horizontal-speed abort limit still applies.
The route does not lock yaw or implement obstacle avoidance. A normal mission
lands at its final launch-position waypoint; the RTL button is an interruption
that delegates return/landing to the FC and marks the interrupted test failed.

## Terminal operation

`./gcs` from the workspace root defaults to **direct UART receive-only monitoring**
on `/dev/serial0` at **115200 baud**. It opens the serial port and receives
telemetry without sending heartbeats, parameter/stream requests, mode changes
or flight commands. It can display telemetry
while commissioning fields are unresolved. The dashboard waits for FC telemetry
and marks link loss/stale data explicitly. Q closes and releases the UART.

The working MAVProxy connection demonstrated the correct UART and baud. The
GCS now opens the same device directly; do not run MAVProxy at the same time.
From the workspace root, the complete normal launch is:

```bash
./gcs
```

The device and baud are saved in `pi_flight/config.toml`; no command-line
connection arguments or UDP outputs are required on later launches/reboots.
This does not install a boot service. A busy UART produces a clear error instead
of opening a second competing reader.

Mission selection/preflight uses `./gcs --check-once`; offline preview uses
`./gcs --offline`. `./gcs --check-once --flight` still requires the complete
commissioning gates. `--monitor --flight` is rejected.

The normal `./gcs` dashboard also has a **SELECT MISSION** control. Press
**M or Enter** to open the live menu, then Up/Down to choose Hover, Forward/Return
or Box. Enter selects the mission and begins the gated preflight workflow; it
does not arm the aircraft. Escape/Q returns from the menu to telemetry, and
telemetry keeps being received while the menu is open. Already-armed aircraft
blocks mission selection; a missing/stale heartbeat blocks preflight/start.

After selection: commissioning/route gates → full parameter/sensor preflight →
individual operator confirmations → **READY / PRESS ENTER** → fresh parameter
and sensor recheck → GUIDED/arm/takeoff. The final start screen accepts only a
new empty Enter; typed text or Escape cancels. Buffered menu keys are discarded.
Q during automatic preflight cancels it. No required FAIL/WARN/UNKNOWN can
advance to flight. Current unresolved commissioning items appear as blockers;
the menu does not change them to PASS.

A blocked, cancelled or completed mission returns to the live dashboard;
selecting another mission starts a new preflight. The monitor releases its UART
and lock before gated preflight opens it, and preflight closes it before
monitoring resumes. There is never a second concurrent UART reader or an
automatic restart of the selected flight.

From the project root, preview without connecting:

```bash
bash pi_flight/launch_gcs.sh --offline
```

The GCS uses a segmented dashboard with colored borders, separate flight,
navigation/sensors and power/link panels, a mission timeline, route progress,
home map, required-check counts/pages, highlighted control buttons and an event
log. Altitude history appears after a launch baseline is available. Human
confirmations appear in a wrapped modal while telemetry continues updating.

Use a full-size SSH terminal, preferably **120 columns by 40 rows**. At 105+
columns, map, mission and checks are shown alongside each other. Narrower
terminals switch the mission/checks panel with C; terminals shorter than 34
rows hide the event log. Below 76 columns or 24 rows, a compact fallback keeps
the selected control and resize instructions visible. Check-page capacity
adjusts to panel height so no checks are skipped during paging.
Up/Down selects a mission; Enter opens the checklist. This menu Enter cannot
arm anything. During flight, arrows select **1 DISARM** or **2 RTL**, and Enter
requests the selected control. `C` switches the map/checklist panel;
Page Up/Down browses check pages. `Q` during flight requests cancellation and
the controller's recovery; after the run, Q closes the display. Ctrl+C/SIGTERM
during flight also request controller cleanup.

The display includes mode/arming/link age, GPS, EKF flags, launch height,
heading, N/E/D velocity, battery, RC RSSI, flow quality, downward range,
landed state, action, waypoint/error, route progress, required-check states,
home-relative map and FC faults. Missing/stale data is shown as unknown/stale.
The percentage is **route progress**, not a claim that landing is complete.
All checklist measurements and detailed telemetry are saved under `logs/`.
Checklist-only runs are bounded; after completion the socket closes and the
display is a report, not a continuing live connection.

After the hardware gates have actually been commissioned, use one terminal:

```bash
# Direct UART: checks only, never arms.
bash pi_flight/launch_gcs.sh --check-once
```

Only after release, at the supervised flight site:

```bash
bash pi_flight/launch_gcs.sh --check-once --flight
```

Never run MAVProxy, MAVROS, `launch_mission.sh`, `testforward.py` or another
flight controller connection concurrently on the same UART. Do not use a
simulation launcher during hardware operation. The old loopback UDP transport
remains available only if explicitly selected in configuration.

## Emergency behavior and limitations

**DISARM** sends normal `MAV_CMD_COMPONENT_ARM_DISARM` with `[0, 0]` only when
fresh `EXTENDED_SYS_STATE` explicitly says ON_GROUND; it rechecks immediately
before sending. No force-disarm magic value or flight termination is used.
Airborne, unknown or stale landed state rejects the button. Pilot takeover
prevents GCS ground disarming during the takeover monitoring phase.

**RTL** requires explicit RTL review, fresh GPS/absolute-position EKF and a home
received on this connection. The request is acknowledged and RTL/LAND must be
observed before releasing GUIDED. No route targets are sent afterwards.
ArduPilot may climb to its configured return altitude, and some configurations
end in a hover rather than landing. Review the exact live configuration:
[ArduPilot Copter RTL](https://ardupilot.org/copter/docs/rtl-mode.html).

Controls are queued by the telemetry/UI pump and handled by the mission loop;
an in-progress command can delay handling up to its configured ACK deadline
(default 5 s). Terminal controls are not a substitute for the independent RC
recovery switch. If the link fails, this program cannot confirm RTL or LAND;
it stops sending and relies on commissioned FC failsafes and the pilot.
Any pilot/FC mode change stops autonomous targets. A sensor/limit/route fault
while GUIDED remains owned requests LAND. It never force-arms, disables
ArduPilot arming checks, restarts a failed mission or uploads simulation params.

Position commands follow the documented local NED position-only mask:
[ArduPilot Guided commands](https://ardupilot.org/dev/docs/copter-commands-in-guided-mode.html).

## Later full rescue mission

After these commissioning flights pass, the separate ROS mission progression
is: validate camera/QR and geographic conversion props-off; review its 10 m
climb and larger search envelope; supervised search/waypoint test; target
approach; centering; staged descent; QR confirmation/transmission; RTL and
landing. Each is a separate release with aircraft/site limits and recovery
verified first. The new GCS does not launch the current 8/10 m ROS scripts from
the commissioning menu; they need a dedicated adapter and validation before
sharing its controls and gates.

Offline tests exercise all three complete routes, hold timing, coordinate
rotation, timeout landing/restoration, pilot takeover, normal ground disarm,
unsafe/stale disarm rejection, RTL acknowledgement/observation, keyboard
selection, home-map reference and no-socket/offline gates. They do not validate
actual firmware dynamics, motor outputs, radio failsafes or physical sensors.
