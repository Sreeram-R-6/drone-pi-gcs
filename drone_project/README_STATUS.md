# ADDC SIM — Autonomous 7-Inch Rescue Drone Simulation: Status Report & Readme

## 1. Executive Summary
The simulation project is currently in a controlled migration phase. The known-good Iris reference system remains fully intact and untouched. A new `rescue_7inch` vehicle model has been constructed and integrated with the ArduPilot modern Gazebo plugin. The model is syntactically valid and has had its ArduPilot plugin entity names corrected for direct-model resolution, setting the stage for runtime flight validation.

## 2. System Verification (As of Sept 13, 2026)
- **Gazebo Harmonic Version:** 8.15.0 (Verified)
- **Plugin Status:** `libArduPilotPlugin.so` is present, verified, and correctly linked (9.8 MB).
- **SDF Validation:** `rescue_7inch/model.sdf` is syntactically valid (Warnings regarding `gz_frame_id` persist but are non-fatal/non-blocking).
- **ArduPilot Integration:** Plugin entity names (`imu_sensor`, `rotor_0_joint`, etc.) are correctly set for direct model resolution.
- **Reference System:** Iris rescue models and worlds remain in place and verified.

## 3. Current Implementation Status
| Component | Status | Note |
| :--- | :--- | :--- |
| Iris Reference | Intact | Fully recoverable. |
| rescue_7inch SDF | Valid | Primitive body/rotor geometry; downward camera only. |
| Plugin Names | Corrected | Switched to model-relative names (`imu_sensor` vs `rescue_7inch::...`). |
| Propulsion | In-progress | Mapping established; thrust/PID tuning pending flight validation. |
| Sensor Architecture | In-progress | IMU and down-camera integrated; Baro/GPS/Compass pending validation. |

## 4. Next Engineering Steps
The following sequence must be followed strictly:
1.  **Gazebo Runtime Verification:** Launch Gazebo and verify the model loads without plugin lookup errors.
2.  **SITL Verification:** Connect ArduPilot SITL to verify sensor data exchange.
3.  **Flight Dynamics Validation:** Conduct individual motor/direction tests followed by hover testing.
4.  **Sensor/Environment/Mission:** Proceed with barometer/GPS/Compass validation, followed by the ROS 2 autonomous pipeline integration and disaster environment testing.

## 5. Development Principles & Safety
- **No Blind Edits:** Always verify the actual file content before modification.
- **Maintain Rollback:** The Iris reference must remain functional at all times.
- **Incremental Validation:** One subsystem change at a time (Model -> Plugin -> Motors -> Sensors -> Mission).
- **Backup Protocol:** Timestamped backups (`.backup-YYYYMMDD-HHMMSS`) are mandatory before any existing file modification.
- **Environment:** ArduPilot/Gazebo Harmonic only; avoid legacy/PX4 components.
