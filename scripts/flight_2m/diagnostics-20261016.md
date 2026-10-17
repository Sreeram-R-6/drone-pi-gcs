# Flight log diagnosis, 16 October 2026

Latest run: `video/.script-runs/4eb33ade578f4b44ac892ff01413a5d3.log`.
Previous run: `video/.script-runs/5cfce4f74f2742abbaa1f3cab0835db2.log`.

Both passed preflight, received an accepted TAKEOFF ACK, armed, and climbed.
The latest run reported range rises 0.56, 0.97, 1.38, 1.81, then 2.28 m,
followed by the altitude guard and confirmed LAND/disarm. The previous run
reported 1.94, 2.38, then 2.57 m before aborting. Its XY correction reduced
drift to 0.03 m while climbing; altitude overshoot was the immediate failure.
An earlier run stopped at 0.51 m drift while only 0.14 m above the initial
range reading, before airborne correction could start.

The old script used range to verify height but latched a local EKF Z target
once during ascent. It did not continuously correct vertical velocity from
range height. Its altitude guard allowed a 2 m target plus 0.6 m tolerance,
which violated the requested 2 m maximum. Targeting exactly the ceiling also
left no braking margin. RTL could independently command a climb later.

These are confirmed software limitations. The logs do not include simultaneous
local Z/vertical velocity or current onboard flight data needed to distinguish
estimator scale, sensor calibration, control tuning, command response and
physical causes of the measured overshoot. Available tlogs are older and do
not cover these flights. Do not infer faulty rangefinder or optical-flow
hardware from these logs alone.

The revised mission uses 0.8 m native bootstrap lift-off, live range-feedback
velocity control toward a 1.5 m rise, 0.15 m/s vertical requests, a 1.85 m
absolute range-clearance landing trigger and predictive climb protection.
It captures launch and destination coordinates, holds height during outbound
and return legs, then requests LAND instead of RTL. Diagnostic lines now
include range height, local Z, local vertical velocity and filtered range climb.
Software protection cannot guarantee physical containment without sensor and
controller validation. No aircraft was flown for this software diagnosis.
