# FC-resident AUTO migration — not yet a physical-flight release

**Inactive:** the operator selected Pi-driven GUIDED again. Local config uses
`execution_backend="guided"` and `mission_policy="home_relative_land_v1"`.
The implementation and commissioning requirements below are retained only for
an explicitly selected future AUTO deployment.

This supersedes the GCS-loss LAND proposal in MISSION_SETUP_PENDING.md for
the local `onboard_auto_v1` deployment. Do not mix its policy with GUIDED routes.

## What changed

- Local config selects `gcs.execution_backend="onboard_auto"` and
  `firmware.mission_policy="onboard_auto_v1"` together. Mismatches are rejected.
- Selection and final Enter still require the existing preflight and operator
  confirmations. Upload happens only after authorization, while disarmed.
- The controller uploads integer global mission items, requires upload ACK,
  downloads every item and compares it with the plan. Failed/partial transfer
  never authorizes arming. An attempted upload may replace the FC's previous
  mission; check the stored mission before using another ground station.
- Item zero is home. Executable items are takeoff to 2 m above home, initial
  hold (5 seconds in terminal workflow), selected route with waypoint holds,
  and LAND at the captured launch coordinates. No terrain-relative frame is used.
- The controller selects item 1, rechecks live sensors/policy, arms normally in
  GUIDED without takeoff, verifies the post-arm home reference, then issues a
  single MISSION_START command. That firmware command enters AUTO. No GUIDED
  takeoff or continuous Pi position targets are used.
- Once started, the FC owns route execution and landing. Terminal/Pi loss stops
  monitoring, not the onboard mission. Completion becomes UNKNOWN; never infer
  landing or automatically resend start. RC mode changes release control.
- Terminal DISARM remains ordinary, ground-confirmed disarm only. RTL remains
  separately reviewed/gated. Battery, EKF and RC failsafes remain required.

## Proposed readbacks (not written by this update)

| Parameter | Onboard policy |
| --- | --- |
| WP_RFND_USE | 0 |
| FS_GCS_ENABLE | 0 — no GCS-loss action, including outside AUTO |
| FS_GCS_TIMEOUT | 5 (inactive with FS_GCS_ENABLE=0) |
| MAV_GCS_SYSID / MAV_GCS_SYSID_HI | 254 / 0 |
| FS_OPTIONS | 16 |
| ARMING_SKIPCHK | 0 |
| FS_THR_ENABLE | 1 — RC-loss RTL |
| FS_EKF_ACTION | 1 — EKF-loss LAND |
| WP_SPD_UP | 0.3 m/s |
| MIS_RESTART | 1 — re-entering AUTO restarts the mission; RC pilot must know this |

Disabling GCS failsafe is deliberately NOT a replacement for an onboard fence,
valid RC-loss/low-battery recovery and pilot takeover. Loss of the terminal means
its UI emergency controls cannot work. RC control is independent and essential.

## Release blockers

`commissioning.onboard_auto_reviewed=false` intentionally prevents a physical
start. Do not flip it merely to remove UNKNOWN. The reviewed parameter profile
and firmware behavior approval are also still incomplete. No physical FC
parameters were written and no motors were started by this migration.

Required evidence before release:

1. Exact-firmware SITL: real mission protocol upload/readback for hover, forward
   and box; verify home/item-zero/frame conversions and post-arm home handling.
2. SITL: observed 2 m takeoff, full 5-second initial hold, waypoint holds, final
   landing and disarm. Verify configured climb/speed and fence behavior.
3. SITL: terminate the GCS after start, and separately interrupt its link. Observe
   independently that the FC continues and lands. Repeat interrupted upload,
   rejected start, lost start ACK, GPS/EKF failure, battery and RC recovery cases.
4. Props-off physical readbacks, correct firmware identity, RC takeover/failsafe
   and arming verification. Obtain real outdoor GPS/EKF/home readiness and review
   onboard fence, RTL and landing parameters. Do not assume unavailable values.
5. Existing operator clear-area/observer confirmations and previous successful
   manual altitude/position-hold flight remain necessary before controlled AUTO.

Software results: 155 GCS tests, 174 OLED tests and 3 packaging tests passed.
The onboard tests use synthetic controllers; they do not test real firmware.
Two optional OLED SITL tests were skipped. A native ArduCopter SITL binary was
not found in the checked workspace/standard installation locations, so the AUTO
backend has NOT completed its required simulator verification. One-metre GPS
routes also do not imply one-metre absolute positioning accuracy.

Read-only MAVProxy commands for collecting the proposed settings:

```text
param show WP_RFND_USE
param show FS_GCS_ENABLE
param show FS_GCS_TIMEOUT
param show MAV_GCS_SYSID
param show MAV_GCS_SYSID_HI
param show FS_OPTIONS
param show ARMING_SKIPCHK
param show FS_THR_ENABLE
param show FS_EKF_ACTION
param show WP_SPD_UP
param show MIS_RESTART
```

Protocol and firmware references:
- https://mavlink.io/en/services/mission.html
- https://ardupilot.org/copter/docs/auto-mode.html
- https://github.com/ArduPilot/ardupilot/blob/dbe79216/ArduCopter/GCS_MAVLink_Copter.cpp

Restart `./gcs` while safely disarmed to load local changes. Do not upload/start
from OLED and terminal concurrently: sharing telemetry does not arbitrate
control ownership between applications.
