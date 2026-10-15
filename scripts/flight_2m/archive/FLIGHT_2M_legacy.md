# 2 m out-and-back flight

`flight_2m.py` uses the repository's existing ArduCopter/pymavlink helpers.
The `scripts` folder already existed; its other tools are preserved.

Plan only (does not open a vehicle connection):

```bash
drone_project/pi_flight/.venv/bin/python scripts/flight_2m.py
```

To fly from the Pi, with the existing MAVProxy router running and other
command interfaces closed:

```bash
drone_project/pi_flight/.venv/bin/python scripts/flight_2m.py --execute
```

`--execute` configures the fence and launches without an interactive prompt.
It retains fresh telemetry, GPS stability, EKF, flow/range, battery and FC
pre-arm checks. It never force-arms or changes arming/failsafe settings.
MTF-01 flow is read from FC `OPTICAL_FLOW`: `flow_comp_m_x`/`flow_comp_m_y`
are the GCS `opt_m_x`/`opt_m_y` values and `quality` is `opt_qua`.
Fresh `OPTICAL_FLOW` takes precedence over `OPTICAL_FLOW_RAD`; RAD remains
a fallback. Downward range continues to use FC `DISTANCE_SENSOR` telemetry
with orientation 25 and centimetres converted to metres. No sensor parameters
are changed by this script.
The battery thresholds come from the existing 4S flight script (14.4 V, 50%).

Sequence: upload/read back a 3 m inclusion circle at recorded home, enable it,
enter GUIDED, arm normally, take off to 2 m, move 2 m along initial heading,
return to takeoff XY at 2 m, then RTL and monitor through disarm. Positions
use the local NED frame; home is checked against GPS coordinates. GPS sample
scatter must be at most 0.3 m, and launch must be within 0.3 m of home.
Changed home after arming aborts the mission rather than moving the fence.

The script uses MAVLink 2 inclusion-circle fence items because the documented
`FENCE_RADIUS` range for the simple home cylinder starts at 30 m. A 3 m fence
is below ArduPilot's recommended size, and GPS errors, drift and braking can
carry the aircraft beyond it. It is not a physical containment guarantee.
Use an open flight area with a pilot ready to recover control, and validate
in SITL before hardware flight. See the [ArduPilot fence guidance](https://ardupilot.org/copter/docs/common-polygon_fence.html).

Existing different fence items cause an abort; they are never overwritten.
**The uploaded circle and FENCE_TYPE/MARGIN/ACTION/ENABLE changes persist**,
including after an aborted mission. FENCE_ACTION is RTL with LAND fallback;
FENCE_MARGIN becomes 0.5 m. Inspect these before another flight. If setup
partly fails, some configuration changes may already have been saved.

Climb and horizontal speed are temporarily limited to 0.3 m/s. RTL_ALT is
temporarily 200 cm, RTL_CLIMB_MIN is zero, and RTL_ALT_FINAL is zero to land.
These navigation settings are restored only after fresh telemetry confirms
disarm. If restoration is deferred, original values are printed to stderr.

A fault while this script owns GUIDED requests LAND. Pilot/FC mode changes
release control. Link loss relies on the aircraft's configured failsafes;
the script cannot command a disconnected aircraft.

Offline tests (no aircraft connection):

```bash
drone_project/pi_flight/.venv/bin/python -m unittest discover -s scripts -p test_flight_2m.py
```
