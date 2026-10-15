# Mission takeoff and recovery policy

**Active local backend: Pi-driven GUIDED.** The LAND-on-GCS-loss policy below
applies to this backend. The optional [onboard AUTO setup](ONBOARD_AUTO_SETUP.md)
is inactive; do not apply settings from both policies.

Policy prepared on 2026-10-12, not applied to the FC and not a flight release.
Config selects `home_relative_land_v1`; fresh readbacks are enforced at full
preflight, final recheck and mission entry before any parameter write or arm.

The target is 2 m above the captured launch position. With home-relative
command semantics, command altitude is the current home-relative altitude
plus 2 m. Routes use launch-relative LOCAL_NED coordinates, hold 5 seconds,
and request LAND after completion. Sensor/limit failure requests LAND only
while the mission still owns a healthy GUIDED link. Pilot mode changes release
control. Lost-link recovery must be performed by the FC, not a disconnected Pi.

The proposed FC configuration for this policy is:

| Setting | Required | Last read-only audit, 2026-10-11 |
| --- | --- | --- |
| WP_RFND_USE | 0 | 1 |
| FS_GCS_ENABLE | 5 (LAND) | 0 (disabled) |
| FS_GCS_TIMEOUT | 5 seconds | 5 |
| MAV_GCS_SYSID | 254 (terminal GCS) | 255 |
| MAV_GCS_SYSID_HI | 0 | 0 |
| FS_OPTIONS | 16 | 16 |

Setting WP_RFND_USE=0 disables rangefinder terrain-following selection, not the
rangefinder driver or the independent altitude-sensor checks. The rangefinder
remains an EKF height source under the last-read EK3_SRC1_POSZ=2. Neither height
fusion nor physical recovery is certified by changing this policy.

No FC setting was written. Approval to apply these settings and a fresh
disarmed FC identity/readback check are still needed. The firmware behavior
approval remains false and the parameter-profile path remains unset until
their underlying setup/review is actually complete.

`parameter-profile.example.json` lists the 14 required audit categories with
unfilled values/types and `reviewed=false`. It is a template, never a passing
profile. The last audit had no FENCE_ENABLE/FENCE_RADIUS/FENCE_ALT_MAX/RTL_ALT_M
responses; determine supported parameters/capabilities before reviewing those
categories. No missing value may be treated as zero or disabled.

Sources for the reported dbe79216 build:
- https://github.com/ArduPilot/ardupilot/blob/dbe79216/ArduCopter/mode_guided.cpp#L143
- https://github.com/ArduPilot/ardupilot/blob/dbe79216/ArduCopter/Parameters.cpp#L85

Props-off RC recovery tests and the controlled 15 m clear radius are operator
confirmations. The code does not invent those confirmations. Final Enter is
required after all live checks and operator confirmations pass.
