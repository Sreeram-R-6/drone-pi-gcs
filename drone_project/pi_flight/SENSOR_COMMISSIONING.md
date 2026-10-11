# Remaining sensor readiness evidence

Flow and downward range sensor IDs were corrected to 0 based on the saved
UART packets in `logs/20261009T120424-87efe3fc/events.jsonl` and
`logs/20261009T120334-20590bfa/events.jsonl`. This identifies received packets;
it does not approve axes, scaling, range accuracy or flight readiness.

The deployment now has provisional tolerant GPS, battery, attitude, EKF, range
and flow diagnostic limits. They are NOT aircraft-approved flight criteria.
Obtain approved limits from the aircraft/sensor specifications and commissioning
tests, then update the corresponding config.toml fields. Autonomous preflight
requires `commissioning.thresholds_approved`, which remains false until review.
Do not derive acceptable limits simply by loosening them until current data
passes. Stationary readings and GPS 3D lock are insufficient for flight approval.

The captured downward range packet reported a 2 cm distance, equal to its
advertised minimum; test measured distances throughout the intended flight
height range, including near ground. The optical-flow stream is legacy
OPTICAL_FLOW; display values do not independently establish correct axes,
compensation or EKF fusion. Verify these against sensor/firmware documentation.

`RC signal strength` with RSSI 255 is optional information, not a mission
blocker. Valid RC channels and FC sensor health are evaluated separately.
The standalone powered vibration/log-review and flow calibration/fusion review
checklist entries have been removed at the operator's request. Live flow and EKF
checks remain. Removing review entries is not proof of calibration or flight
readiness. See FC_EVIDENCE_REVIEW.md for the saved identity and configuration audit.

Autonomous flight additionally requires exact firmware/identity evidence,
reviewed live parameters, valid origin/home, and operator confirmations. The
mission start prompt is intentionally unavailable until required checks pass.
