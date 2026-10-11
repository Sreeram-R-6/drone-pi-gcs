# ADDC SIM — Autonomous 7-Inch Rescue Drone Simulation

**Complete Project Reference & Status Document**  
*Cross-verified against actual implementation as of 2026-09-13*

---

## 1. PROJECT OVERVIEW

This project implements a **Gazebo Harmonic + ArduPilot ArduCopter SITL** simulation of an autonomous 7-inch rescue quadrotor. The architecture uses the **modern ArduPilot Gazebo JSON interface** (not legacy PX4 or khancyr plugins).

```
┌──────────────────────┐
│  Gazebo Harmonic     │  Physics + Terrain + Wind
│  (8.15.0)            │
└──────────┬───────────┘
           │ ArduPilot JSON (UDP 9002)
           ▼
┌──────────────────────┐
│  ArduCopter SITL     │  Flight Controller
│  (commit a5cd5f7)    │
└──────────┬───────────┘
           │ MAVLink/UDP
     ┌─────┴─────┐
     ▼           ▼
QGroundControl  MAVROS (14551)
                    │
                    ▼
              ROS 2 Humble
                    │
         ┌──────────┴──────────┐
         ▼                     ▼
   QR Detector           geotag_mission
   (/camera/down/image)  (GUIDED autonomous)
         │                     │
         ▼                     ▼
   /rescue/qr_detected    UDP:5005 → GCS
```

**Target Real Aircraft:**
- 7-inch quadrotor, 1.5 kg AUW
- 4 × 2807 1350KV motors, 7×4 props, Zeus 50A 4-in-1 ESC
- 4S 5000 mAh 10C LiPo (300 g, included in 1.5 kg)
- iFlight BLITZ Mini F745 (STM32F745, ICM42688, DPS310, QMC5883 ext. compass)
- Raspberry Pi companion, **downward camera only**
- ArduCopter on hardware; ArduCopter SITL in simulation

---

## 2. VERIFIED SYSTEM STATE

| Component | Version/Path | Verified |
|---|---|---|
| **OS** | Ubuntu 22.04.5 LTS | ✓ |
| **Gazebo** | Harmonic 8.15.0 | ✓ `gz sim --versions` |
| **ArduPilot** | `~/ardupilot` (commit `a5cd5f7981`) | ✓ `git rev-parse` |
| **ArduCopter SITL** | `~/ardupilot/build/sitl/bin/arducopter` | ✓ 5.8 MB binary |
| **Gazebo Plugin** | `~/gz_ws/src/ardupilot_gazebo/build/libArduPilotPlugin.so` | ✓ 9.8 MB, `gz::sim::v8` symbols |
| **ROS 2** | Humble (`/opt/ros/humble`) | ✓ |
| **Workspace** | `~/drone_project/ros2_ws` | ✓ Built |

---

## 3. DIRECTORY STRUCTURE (Actual)

```
~/
├── ardupilot/                          # ArduPilot source & SITL build
├── gz_ws/
│   └── src/ardupilot_gazebo/           # Modern Gazebo plugin (built)
├── drone_project/                      # MAIN PROJECT
│   ├── models/
│   │   ├── iris_rescue/                # Known-good reference (untouched)
│   │   ├── iris_with_standoffs_rescue/ # Reference airframe (untouched)
│   │   ├── qr_target/                  # QR landing target
│   │   └── rescue_7inch/               # NEW 7-inch vehicle (active)
│   │       ├── model.sdf               # Primary artifact (635 lines)
│   │       ├── model.config            # Model metadata
│   │       └── *.backup-*              # Timestamped backups (8+)
│   ├── worlds/
│   │   └── iris_rescue.sdf             # Test world (now includes rescue_7inch)
│   ├── config/
│   │   ├── rescue_7inch.parm           # Minimal (BATT_CAPACITY,5000)
│   │   └── rescue_7inch_spec.txt       # Full specification
│   ├── ros2_ws/src/rescue_control/     # Autonomy stack
│   │   └── rescue_control/
│   │       ├── geotag_mission.py       # Active mission (2544 lines)
│   │       ├── qr_detector.py          # QR decoder (pyquirc)
│   │       └── autonomous_mission.py   # Reference mission
│   ├── vision/camera_view.py           # Dual-camera GUI (ROS)
│   ├── camera_bridge.yaml              # ros_gz_bridge config (NEEDS UPDATE)
│   ├── start_bridge.sh                 # Bridge launcher
│   ├── start_sitl.sh                   # SITL launcher (-f gazebo-iris)
│   ├── qr_server.py / show_qr_value.py # Utilities
├── simscripts/
│   ├── start_sim.sh                    # Master Kitty launcher
│   ├── rescue_drone.kitty-session      # Session definition
│   └── stop_sim.sh                     # Cleanup
└── qr_gcs_receiver.py                  # GCS UDP listener (:5005)
```

**Note:** `worlds/rescue_7inch.sdf` does **not yet exist** — the current test world `iris_rescue.sdf` has been repurposed to spawn `rescue_7inch`. A dedicated disaster world is a future deliverable.

---

## 4. RESCUE_7INCH MODEL — TECHNICAL SPECIFICATION

### 4.1 Model Hierarchy (Flat, Direct Children of `rescue_7inch`)

```
rescue_7inch
├── base_link
│   ├── down_camera (sensor, 640×480, 20 Hz, 90° FOV, pose: 0 0 -0.10 0 1.5708 0)
│   ├── base_collision (box 0.18×0.14×0.045)
│   └── 4× landing leg collisions (cylinders at ±0.075, ±0.055, -0.11)
├── imu_link
│   └── imu_sensor (IMU, 1000 Hz, pose: 0 0 0 180 0 0 degrees)
├── rotor_0 (link + rotor_0_joint)  @ +0.113137, -0.113137, 0.030
├── rotor_1 (link + rotor_1_joint)  @ -0.113137, +0.113137, 0.030
├── rotor_2 (link + rotor_2_joint)  @ +0.113137, +0.113137, 0.030
├── rotor_3 (link + rotor_3_joint)  @ -0.113137, -0.113137, 0.030
└── ArduPilotPlugin
```

### 4.2 Mass Properties (Simplified, Total ≈ 1.500 kg)

| Link | Mass (kg) | Inertia (kg·m²) |
|---|---|---|
| base_link | 1.35 | ixx=0.020, iyy=0.020, izz=0.035 |
| imu_link | 0.010 | ixx=1e-5, iyy=2e-5, izz=2e-5 |
| rotor_0..3 | 0.035 each | ixx=9.75e-6, iyy=1.667e-4, izz=1.676e-4 |

**Total:** 1.35 + 0.010 + 4×0.035 = **1.500 kg** ✓

> ⚠ **Important:** Battery mass (300 g) is **included** in the 1.5 kg AUW. Do not add it again.

### 4.3 Motor Geometry

- **Diagonal:** 0.320 m (320 mm) — *simulation assumption, not measured*
- **Motor coordinate magnitude:** 0.113137 m (derived from 320/√2/2)
- **Rotor height:** 0.030 m above base_link origin
- **Propeller visual:** 0.1778 m (7 inch) × 0.008 m box approximation

### 4.4 ArduPilotPlugin Configuration (Critical)

```xml
<plugin name="ArduPilotPlugin" filename="ArduPilotPlugin">
  <fdm_addr>127.0.0.1</fdm_addr>
  <fdm_port_in>9002</fdm_port_in>
  <lock_step>1</lock_step>
  <no_time_sync>1</no_time_sync>

  <!-- Frame transforms (do not change casually) -->
  <modelXYZToAirplaneXForwardZDown degrees="true">0 0 0 180 0 0</modelXYZToAirplaneXForwardZDown>
  <gazeboXYZToNED degrees="true">0 0 0 180 0 90</gazeboXYZToNED>

  <!-- SENSOR NAMES — MODEL-RELATIVE (corrected from fully-scoped) -->
  <imuName>imu_sensor</imuName>

  <!-- CONTROL CHANNELS (4 motors) -->
  <control channel="0"><jointName>rotor_0_joint</jointName> <multiplier>838</multiplier>  ...</control>
  <control channel="1"><jointName>rotor_1_joint</jointName> <multiplier>838</multiplier>  ...</control>
  <control channel="2"><jointName>rotor_2_joint</jointName> <multiplier>-838</multiplier> ...</control>
  <control channel="3"><jointName>rotor_3_joint</jointName> <multiplier>-838</multiplier> ...</control>
</plugin>
```

> **CRITICAL FIX APPLIED:** Plugin entity names were originally fully-scoped (`rescue_7inch::rotor_0_joint`, `rescue_7inch::imu_link::imu_sensor`). Because the model has a **flat hierarchy** (no nested `iris_with_standoffs_rescue` wrapper), the plugin could not resolve them. They have been corrected to **model-relative names** (`rotor_0_joint`, `imu_sensor`). SDF validates.

### 4.5 Motor Direction Mapping (Quad-X)

| ArduPilot Channel | Rotor Joint | Multiplier | Intended Spin |
|---|---|---|---|
| 0 | rotor_0_joint | +838 | CCW |
| 1 | rotor_1_joint | +838 | CCW |
| 2 | rotor_2_joint | -838 | CW |
| 3 | rotor_3_joint | -838 | CW |

**Validation required at runtime:** Symmetric throttle must produce **zero net yaw torque**.

---

## 5. SENSOR ARCHITECTURE STATUS

| Sensor | Simulated | SDF Status | Notes |
|---|---|---|---|
| **IMU (ICM42688)** | ✅ | `imu_link::imu_sensor` @ 1000 Hz | Configured, plugin-connected |
| **Barometer (DPS310)** | ❌ | Not in SDF | Future: add to `base_link` or dedicated link |
| **GPS/NavSat (BZ251)** | ❌ | Not in SDF | Future: add `<sensor type="navsat">` |
| **Compass (QMC5883 ext.)** | ❌ | Not in SDF | Future: external compass simulation |
| **Downward Camera** | ✅ | `base_link::down_camera` | 640×480, 20 Hz, 90° FOV |
| **Front Camera** | ❌ | **Absent** (correct) | Real aircraft has none |

---

## 6. ROS 2 AUTONOMY STACK

### 6.1 Topics Used

| Topic | Type | Direction | Node |
|---|---|---|---|
| `/mavros/state` | `mavros_msgs/State` | Sub | geotag_mission |
| `/mavros/local_position/pose` | `PoseStamped` | Sub | geotag_mission |
| `/mavros/setpoint_position/local` | `PoseStamped` | Pub | geotag_mission |
| `/mavros/setpoint_raw/local` | `PositionTarget` | Pub | geotag_mission (centering) |
| `/mavros/cmd/arming` | `CommandBool` | Srv | geotag_mission |
| `/mavros/cmd/command` | `CommandLong` | Srv | geotag_mission (takeoff) |
| `/mavros/set_mode` | `SetMode` | Srv | geotag_mission |
| `/camera/down/image` | `Image` | Sub | geotag_mission, qr_detector |
| `/camera/down/camera_info` | `CameraInfo` | Sub | (available) |
| `/rescue/qr_detected` | `String` | Pub | qr_detector → geotag_mission |

### 6.2 Mission Phases (geotag_mission.py)

```
WAIT_FCU → GUIDED → ARM → TAKEOFF (10m) → SEARCH (lawnmower)
    ↓
WHITE TARGET FOUND → GEOTAG (calculate ground position)
    ↓
GO_TO_GEOTAG → GEOTAG_SCAN (5s high-alt QR scan)
    ↓                              │
    QR found ──────────────────────→ SEND_QR (UDP×3) → RTL
    │
    QR not found
    ↓
CENTER (visual servo on white target) → DESCEND (step to 1.2m)
    ↓                              │
    QR found ──────────────────────→ SEND_QR → RTL
    │
    timeout (30s)
    ↓
RESUME SEARCH (next waypoint)
```

**Key constants:**
- `TAKEOFF_ALTITUDE = 10.0 m`
- `QR_SEARCH_ALTITUDE = 1.2 m`
- `SEARCH_LENGTH = 15 m`, `SEARCH_TRACK_SPACING = 2 m`
- `QR_CONFIRMATIONS_REQUIRED = 5` (consecutive frames)
- GCS UDP: `127.0.0.1:5005`

### 6.3 QR Detector (qr_detector.py)

- Independent node subscribing to `/camera/down/image`
- Uses `pyquirc` (not OpenCV QRCodeDetector)
- 5 consecutive identical payloads → confirmed → publishes `/rescue/qr_detected` + sends UDP to GCS

---

## 7. CAMERA BRIDGE — NEEDS UPDATE FOR 7-INCH

**Current `camera_bridge.yaml` maps Iris topics:**

```yaml
- ros_topic_name: "/camera/down/image"
  gz_topic_name: "/world/iris_runway/model/iris_rescue/model/iris_with_standoffs_rescue/link/base_link/sensor/down_camera/image"
```

**Required for rescue_7inch (flat hierarchy):**

```yaml
- ros_topic_name: "/camera/down/image"
  gz_topic_name: "/world/iris_runway/model/rescue_7inch/link/base_link/sensor/down_camera/image"
```

**Front camera entries should be removed** (7-inch has no front camera).

---

## 8. LAUNCHER STATUS

### 8.1 Manual Commands (Reference System — Iris)

```bash
# Terminal 1: Gazebo (Iris world)
cd ~ && gz sim -v4 -r ~/drone_project/worlds/iris_rescue.sdf

# Terminal 2: ArduPilot SITL
cd ~/ardupilot && ./Tools/autotest/sim_vehicle.py -v ArduCopter -f gazebo-iris --model JSON:127.0.0.1 --console --map

# Terminal 3: MAVROS
source /opt/ros/humble/setup.zsh && ros2 run mavros mavros_node --ros-args -p fcu_url:=udp://127.0.0.1:14551@

# Terminal 4: Camera Bridge
~/drone_project/start_bridge.sh

# Terminal 5: Camera View
source /opt/ros/humble/setup.zsh && python3 ~/drone_project/vision/camera_view.py

# Terminal 6: GCS UDP
python3 ~/qr_gcs_receiver.py

# Terminal 7: QR Detector + Mission
source /opt/ros/humble/setup.zsh && source ~/drone_project/ros2_ws/install/setup.zsh
ros2 run rescue_control qr_detector &
ros2 run rescue_control geotag_mission
```

### 8.2 Master Launcher (Kitty) — Currently Uses Iris World

```bash
cd ~ && ~/simscripts/start_sim.sh
```

Opens 7 OS windows via Kitty. **Do not switch to rescue_7inch world until Gates 1-5 pass.**

---

## 9. ENVIRONMENT (Required Exports)

Already in `~/.zshrc`:

```bash
source /opt/ros/humble/setup.zsh
export GZ_VERSION=harmonic
export GZ_SIM_SYSTEM_PLUGIN_PATH="$HOME/gz_ws/src/ardupilot_gazebo/build"
export GZ_SIM_RESOURCE_PATH="$HOME/drone_project/models:$HOME/gz_ws/src/ardupilot_gazebo/models:$HOME/drone_project/worlds:$HOME/gz_ws/src/ardupilot_gazebo/worlds"
source "$HOME/drone_project/ros2_ws/install/setup.zsh"
```

---

## 10. VALIDATION GATES (Must Pass In Sequence)

| Gate | Test | Pass Criteria |
|---|---|---|
| **0** | Iris reference | `gz sim iris_rescue.sdf` + SITL + MAVROS + mission all work |
| **1** | rescue_7inch loads | `gz sim -v4 -r iris_rescue.sdf` → no SDF errors, model appears |
| **2** | Plugin initializes | No "Joint not found" / "IMU not found" / "abort ArduPilot plugin" |
| **3** | SITL connects | "Connected to ArduPilot controller @ 127.0.0.1:9002" |
| **4** | Motor mapping | Each RC channel → correct rotor, correct direction, no stray yaw |
| **5** | Hover | Arm → takeoff 2m → hold position ±0.2m for 30s → land |
| **6** | Position/RTL | GUIDED position commands → RTL returns to origin |
| **7** | Sensors | IMU/GPS/Baro/Compass data in MAVROS; down camera publishes |
| **8** | Camera bridge | `/camera/down/image` visible in `camera_view.py` |
| **9** | QR detector | Detects `86` on qr_target → publishes `/rescue/qr_detected` |
| **10** | Full mission | Autonomous search → geotag → QR → RTL completes |

---

## 11. DEVELOPMENT RULES (Non-Negotiable)

1. **Preserve Iris reference** — Never modify `iris_rescue/`, `iris_with_standoffs_rescue/`, or `iris_rescue.sdf`
2. **Backup before edit** — `cp file file.backup-$(date +%Y%m%d-%H%M%S)` for any existing file
3. **One subsystem at a time** — Model → Plugin → Motors → Sensors → Mission → Terrain → Wind
4. **No blind edits** — Read actual file before modifying
5. **No PX4 / Gazebo Classic / legacy khancyr plugin** — ArduPilot + Harmonic + JSON only
6. **No invented thrust curves** — 838 multiplier is a placeholder; calibrate later
7. **No wind before hover** — Calm → 5 m/s → gusts → 10 m/s
8. **No mission changes for airframe swap** — ROS interfaces stay stable
9. **Clean shutdown** — `Ctrl+C` in each terminal; avoid broad `pkill` during debugging
10. **Shell conventions** — Use `cd ~` prefix; `command grep` not `grep`; `/usr/bin/find` not `find`

---

## 12. IMMEDIATE NEXT STEPS (Exact Commands)

### Step 1: Clean Gazebo Launch
```bash
cd ~/drone_project
export GZ_SIM_RESOURCE_PATH="$HOME/drone_project/models:$HOME/gz_ws/src/ardupilot_gazebo/models:$HOME/drone_project/worlds:$HOME/gz_ws/src/ardupilot_gazebo/worlds"
export GZ_SIM_SYSTEM_PLUGIN_PATH="$HOME/gz_ws/src/ardupilot_gazebo/build:$HOME/gz_ws/src/ardupilot_gazebo/build/lib:${GZ_SIM_SYSTEM_PLUGIN_PATH}"
gz sim -v4 -r ~/drone_project/worlds/iris_rescue.sdf
```
**Watch for:** No "Joint with name [rescue_7inch::rotor_0_joint] not found" or "imu_sensor [...] not found"

### Step 2: ArduPilot SITL (separate terminal)
```bash
cd ~/ardupilot
./Tools/autotest/sim_vehicle.py -v ArduCopter -f gazebo-iris --model JSON:127.0.0.1 --console --map
```
**Watch for:** "Connected to ArduPilot controller @ 127.0.0.1:9002" and cessation of "No JSON sensor message received, resending servos"

### Step 3: Verify Motor Response (via MAVProxy console)
```
mode guided
arm throttle
rc 3 1500   # throttle ~50%
```
Observe: All 4 rotors spin, vehicle lifts symmetrically, no yaw.

---

## 13. KNOWN ISSUES TO RESOLVE BEFORE PRODUCTION

1. **`camera_bridge.yaml`** — Update Gazebo topic paths for `rescue_7inch` flat hierarchy
2. **`rescue_7inch.parm`** — Expand with full ArduPilot parameter set (FRAME, MOTORS, BATT, GPS, COMPASS, EKF, NAV, FAILSAFES)
3. **Dedicated world** — Create `worlds/rescue_7inch.sdf` with disaster terrain, structures, wind plugin
4. **Barometer/GPS/Compass** — Add sensors to SDF (NavSat, barometer, external compass)
5. **Kitty launcher** — Update `start_sim.sh` and session to use `rescue_7inch.sdf` and new bridge YAML

---

## 14. REFERENCE DOCUMENTS

- **Cross-Check Report:** `~/drone_project/REPORT_CROSS_CHECK.md` (this verification)
- **Build Status:** `~/drone_project/rescue_7inch_BUILD_STATUS.md`
- **Status Summary:** `~/drone_project/README_STATUS.md`
- **Hardware Spec:** `~/drone_project/config/rescue_7inch_spec.txt`
- **Original Handoff:** (User-provided ADDC_SIM document)

---

## 15. OFFICIAL TECHNICAL REFERENCES

- ArduPilot Gazebo (modern): https://github.com/ArduPilot/ardupilot_gazebo
- SITL with Gazebo: https://ardupilot.org/dev/docs/sitl-with-gazebo.html
- JSON Interface: https://ardupilot.org/dev/docs/sitl-with-JSON.html
- Gazebo Harmonic: https://gazebosim.org/docs/harmonic/
- BLITZ F745/Mini: https://ardupilot.ardupilot.org/sub/docs/common-blitz-f745.html

---

## 16. QUICK REFERENCE — KEY FILE PATHS

| Purpose | Path |
|---|---|
| **Primary vehicle SDF** | `~/drone_project/models/rescue_7inch/model.sdf` |
| **Test world** | `~/drone_project/worlds/iris_rescue.sdf` |
| **SITL launcher** | `~/drone_project/start_sitl.sh` |
| **Camera bridge config** | `~/drone_project/camera_bridge.yaml` |
| **Bridge launcher** | `~/drone_project/start_bridge.sh` |
| **Mission code** | `~/drone_project/ros2_ws/src/rescue_control/rescue_control/geotag_mission.py` |
| **QR detector** | `~/drone_project/ros2_ws/src/rescue_control/rescue_control/qr_detector.py` |
| **Camera viewer** | `~/drone_project/vision/camera_view.py` |
| **GCS receiver** | `~/qr_gcs_receiver.py` |
| **Master launcher** | `~/simscripts/start_sim.sh` |

---

*This document is the authoritative reference for the current implementation state. It was generated by cross-verifying the original handoff documentation against the actual filesystem and running binaries. All verification commands are reproducible.*