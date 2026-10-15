# Mission refactor: test handoff

The local deployment selects the Pi-driven GUIDED backend. The sequence below
applies to it. The optional onboard AUTO backend is inactive and must not be
treated as a verified alternative without its separate commissioning.

Software regression run, 2026-10-12: 140 GCS tests, 174 OLED tests and
3 packaging tests passed. Two optional SITL tests were skipped. These are
synthetic/software tests, not proof of flight readiness.

## Mission sequence

1. Connect using the existing persistent router; select a mission with M.
2. Complete live preflight and the operator confirmations. Selection does not arm.
3. Press final Enter when offered. Fresh rechecks must pass before authorization.
4. Validate and freeze the selected route and launch references before any setting
   write or arm command. Reject invalid limits, route or required heading.
5. Request GUIDED and normal arming, take off to the configured 2 m launch-relative
   target, settle, then hold for 5 seconds in the terminal GCS workflow.
6. Hover: land after the hold. Forward: move 1 m forward, hold, return and hold.
   Box: four 1 m legs returning to launch, holding at each waypoint.
7. Request LAND and observe completion separately. Progress is monotonic and
   completion/stoppage is displayed explicitly. A finished mission cannot restart.

Pilot mode changes release mission ownership. Recovery command failures stop
mission sending and are reported; they do not establish that the aircraft landed.
Lost-link recovery depends on the flight controller's configured failsafe.

## Before any physical flight

The setup in MISSION_SETUP_PENDING.md remains unresolved: proposed FC failsafe /
takeoff-reference settings have not been applied, the reviewed live parameter
profile is missing, and firmware behavior approval is incomplete. GPS/EKF/home
readiness and battery must pass live checks. Physical RC recovery and clear-area
confirmations must be genuine. No check is bypassed by this refactor.

Restart `./gcs` only while safely disarmed to load the local changes. Perform a
props-off UI/link/recovery rehearsal, then SITL fault tests before considering a
controlled physical hover test. This update did not write FC parameters, arm,
start motors, or initiate a flight.
