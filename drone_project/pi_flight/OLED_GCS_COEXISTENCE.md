# Shared OLED and terminal GCS connection

Saved configuration: `./gcs` listens on `udpin:127.0.0.1:14550`.
The existing boot-enabled `mavproxy.service` is the single UART owner:
`/dev/serial0`, 115200 baud. Its installed command already forwards to
14550 (GCS), 14552 (diagnostics), and 14553 (OLED). No service restart or
manual output command is needed for this change.

On 2026-10-11, a passive test using the actual GCS `Transport` received
FC 1:1 heartbeat, attitude, battery, GPS, EKF and RC telemetry while the
OLED web bridge reported successful physical-display writes and frame ages
around 0.15 seconds before and during the GCS connection. Heartbeat age was
0.12 seconds. The test sent no MAVLink commands and did not open the UART.
The FC was disarmed; GPS reported fix 1 and zero satellites at test time.
This verifies simultaneous telemetry and display write success, not physical
pixel appearance, calibration, or flight readiness.

Run `./gcs` from the workspace root. Leave `mavproxy.service`,
`drone-oled.service`, and `drone-oled-web.service` running. Exit/relaunch an
already-open GCS to load the changed configuration. Do not run a second
GCS on port 14550 or open the FC UART in a second MAVProxy process.

Use only one control interface for vehicle actions at a time. The two UIs
have distinct MAVLink source IDs (254 terminal, 253 OLED), but do not share
a cross-application command-ownership lock. All existing launch checks are
unchanged. Closing the terminal does not stop OLED telemetry.
