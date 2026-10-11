# Commissioning and fault injection

No offline check substitutes for these actual-Pi/FC results. Record firmware,
config/profile hashes, UTC time and evidence paths for each result.

## M0–M3: display and read-only UI

- Check BCM/physical pin table, button continuity, active LOW, internal pull-ups.
- Hold OK through boot: no activation until all buttons release.
- Test all six keys, bounce, chords, held navigation and long Back.
- Verify all OLED edge pixels, rotation, controller/address, 100/400kHz timings.
- Cold-power video: measure power→first image and service-start→first image.
- Verify splash during systemd starting; degraded produces warning; missing FC/GPS
  does not prevent Home; persistent starting escapes after 45 seconds.
- Compare real telemetry units to GCS. Unplug FC: fields become `--`, then link alarm.
- Verify cell/pack-only reporting, GPS versus relative altitude, nonzero home,
  and EKF source validity. GPS satellites alone must not mark READY.

## M4–M6: simulation, then props-off bench

- Run owned-process SITL tests; capture exact STATUSTEXT for each fault and place
  reviewed matchers in the profile. Never guess RC failsafe strings.
- Verify arm rejection, ACK loss, heartbeat loss, stale landed state, no force command.
- Remove all propellers. Verify motor test sequence versus RR/FR/RL/FL; do not
  confuse test order with output numbering.
- Demonstrate Back stop, Pi power loss, router kill and UART disconnect mid-test.
  Motor output must expire via the FC's ≤3-second timeout without Pi keepalives.
- Show every uploaded mission item and compare readback. Abort countdown at its
  boundary: no FC changes. After T-0, report partial uploads honestly.
- Verify supported start mode and first-item behaviour for the exact firmware.
- Check pilot mode takeover, RTL route/altitude/home, rejected RTL and LAND fallback.
- Emergency Back changes no vehicle behaviour. LAND needs a separate confirmation.
- Parameter writes: backup old value, verify type/new readback, disarmed restart
  persistence test. No changes during motor test/calibration/mission transaction.
- Compass: verify selected mask, progress for all sensors, separate SAVE, cancellation
  and reboot instructions. Accel/flow stay GCS-only until protocol commissioning.

## M7: 24-hour soak

- Read-only telemetry and navigation, rotating logs, RSS/CPU sampled each minute.
- Target service-first-frame ≤1s, cold-power image ≤10s (measure, not guarantee),
  process CPU average <5% of one core, RSS <100MiB, no upward growth trend.
- Kill/restart UI and router. Reconnect must never repeat a command/countdown.
- Inject I2C NACK/unplug: retries/backoff; telemetry and stop inputs continue.
- Flood GPIO edges and event queue: controls disable, pending follow-ups cancel.
- Simulate disk full/logging failure, hung `iw`/systemctl, invalid mission/config.
- Confirm service account device access without root/sudo or world-writable devices.
- Verify actual GCS/RC failsafes; companion heartbeat must not mask the true GCS.
- Test shutdown clear and document the unavoidable pre-Linux/power-loss blank period.

## Suggested evidence commands

```sh
systemd-analyze critical-chain drone-oled.service
systemctl show drone-oled.service -p ExecMainStartTimestampMonotonic -p MainPID
journalctl -u drone-oled.service -u mavproxy.service --since today
ps -C drone-oled -o pid,etime,%cpu,rss,args
```

Record limitations openly. Physical stopping, flight safety, environmental limits
and regulatory permissions remain aircraft/operator responsibilities.
