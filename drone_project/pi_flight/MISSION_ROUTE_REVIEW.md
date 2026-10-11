# Software mission route review

Scope: supplied Hover, Forward and Box routes, for software/bench testing.
This is not certification of the aircraft, sensors, firmware or flight area.

All missions take off to 2 m above the verified launch reference, settle for
3 seconds and hold for 5 seconds. They finish with LAND and observed disarm.
The configured climb rate is 0.30 m/s. Coordinates are launch-heading-relative.

| Mission | Horizontal route after initial hover | Maximum target radius |
| --- | --- | --- |
| Hover | No horizontal legs | 0 m |
| Forward | Forward 1 m, return to launch | 1 m |
| Box | Forward 1 m, right 1 m, backward 1 m, left to launch | 1.414 m |

Each horizontal leg uses 0.30 m/s setpoint interpolation, a 0.20 m position
tolerance, a 0.15 m/s arrival-speed limit, 3 seconds settling and 5 seconds
holding. A one-metre leg therefore needs at least 11.33 seconds; its timeout
is 30 seconds. Box radius plus tolerance is 1.614 m, below the 2 m abort radius.
Runtime validation checks these constraints and rejects impossible timeouts.

Tests exercise all three full routes, hold durations, landing/disarm,
parameter restoration, waypoint timeout recovery and pilot takeover. They use
mock aircraft telemetry, not a physical flight controller or SITL.

`gcs.routes_reviewed` is enabled for these software-reviewed routes in the
deployment configuration. `gcs.rtl_reviewed` remains false. Firmware identity,
sensor checks, parameter review and operator confirmations still gate flight.
Bench-test with propellers removed first. Do not interpret route approval or
passing mocked tests as permission to fly with unresolved live checks.
