#!/usr/bin/env bash

echo "======================================"
echo " STARTING RESCUE 7-INCH DRONE SIM"
echo "======================================"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DRONE_DIR="$PROJECT_DIR/drones/rescue_7inch"
SESSION="$DRONE_DIR/start/rescue_7inch.kitty-session"

# Patterns identify this simulation by its world, parameter file, or ports.
# They do not match arbitrary gz, Python, or ArduPilot processes.
SIM_PATTERNS=(
  "$DRONE_DIR/world/rescue_7inch.sdf"
  "$DRONE_DIR/config/rescue_7inch.parm"
  "$PROJECT_DIR/worlds/iris_rescue.sdf"
  "$PROJECT_DIR/start_sitl.sh"
  'gz sim'
  'gzserver'
  'gz-gui'
  'gz-sim-server'
  'arducopter.*--sim-port-out=9003'
  'mavproxy.py.*14560'
  'mavproxy.py.*14550'
  'mavros_node.*14561'
  'mavros_node.*14551'
  "parameter_bridge.*$DRONE_DIR/start/camera_bridge.yaml"
  "parameter_bridge.*$PROJECT_DIR/camera_bridge.yaml"
  'camera_view.py'
  "${DRONE_DIR}/start/wind_control.sh"
  'qr_gcs_receiver.py'
  'ros2 run rescue_control qr_detector'
  'ros2 run rescue_control geotag_mission'
)

stop_processes() {
  local signal="$1"
  echo "Stopping rescue simulation processes ($signal)..."
  for pattern in "${SIM_PATTERNS[@]}"; do
    command pkill "-$signal" -f -- "$pattern" 2>/dev/null || true
  done
}

cleanup_simulation() {
  echo
  echo "======================================"
  echo " STOPPING RESCUE 7-INCH DRONE SIM"
  echo "======================================"
  stop_processes TERM
  sleep 2
  stop_processes KILL
  echo "RESCUE 7-INCH SIM STOPPED"
}

trap 'cleanup_simulation; exit 130' INT TERM

echo "[1/2] Stopping old matching simulation processes..."
stop_processes TERM
sleep 2
stop_processes KILL

export GZ_VERSION=harmonic
export GZ_SIM_RESOURCE_PATH="$DRONE_DIR/models:$DRONE_DIR/world/models:$PROJECT_DIR/models:$HOME/gz_ws/src/ardupilot_gazebo/models:$DRONE_DIR/world"
export GZ_SIM_SYSTEM_PLUGIN_PATH="$HOME/gz_ws/src/ardupilot_gazebo/build:$HOME/gz_ws/src/ardupilot_gazebo/build/lib:${GZ_SIM_SYSTEM_PLUGIN_PATH:-}"

echo "[2/2] Starting fresh rescue_7inch simulation..."
echo "World: $DRONE_DIR/world/rescue_7inch.sdf"
echo "JSON: 127.0.0.1:9003"
echo "MAVLink: 14560 QGC / 14561 MAVROS"
echo "Wind starts after a 60-second initialization delay"

if [[ $# -ge 5 ]]; then
  WIND_STRENGTH="$1"
  WIND_DIRECTION="$2"
  WIND_STRENGTH_RANDOMNESS="$3"
  WIND_DIRECTION_RANDOMNESS="$4"
  WIND_DURATION="$5"
else
  read -r -p "Wind strength in m/s [0]: " WIND_STRENGTH
  read -r -p "Wind direction in degrees, 0=+X, 90=+Y [0]: " WIND_DIRECTION
  read -r -p "Wind strength randomness (+/- m/s) [0]: " WIND_STRENGTH_RANDOMNESS
  read -r -p "Wind direction randomness (+/- degrees) [0]: " WIND_DIRECTION_RANDOMNESS
  read -r -p "Wind duration in seconds [60]: " WIND_DURATION
  WIND_STRENGTH="${WIND_STRENGTH:-0}"
  WIND_DIRECTION="${WIND_DIRECTION:-0}"
  WIND_STRENGTH_RANDOMNESS="${WIND_STRENGTH_RANDOMNESS:-0}"
  WIND_DIRECTION_RANDOMNESS="${WIND_DIRECTION_RANDOMNESS:-0}"
  WIND_DURATION="${WIND_DURATION:-60}"
fi

if ! [[ "$WIND_STRENGTH" =~ ^[0-9]+([.][0-9]+)?$ && "$WIND_DIRECTION" =~ ^-?[0-9]+([.][0-9]+)?$ && "$WIND_STRENGTH_RANDOMNESS" =~ ^[0-9]+([.][0-9]+)?$ && "$WIND_DIRECTION_RANDOMNESS" =~ ^[0-9]+([.][0-9]+)?$ && "$WIND_DURATION" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "Invalid wind input. Use numeric strength, direction, and duration."
  exit 2
fi

cat > "$SESSION" <<SESSION_EOF
new_tab
title Gazebo
cd ~
launch --type=os-window --title="Rescue 7-inch - Gazebo" bash -lc 'source /opt/ros/humble/setup.bash; export GZ_VERSION=harmonic; export GZ_SIM_RESOURCE_PATH="$DRONE_DIR/models:$DRONE_DIR/world/models:$PROJECT_DIR/models:$HOME/gz_ws/src/ardupilot_gazebo/models:$DRONE_DIR/world"; export GZ_SIM_SYSTEM_PLUGIN_PATH="$HOME/gz_ws/src/ardupilot_gazebo/build:$HOME/gz_ws/src/ardupilot_gazebo/build/lib:\${GZ_SIM_SYSTEM_PLUGIN_PATH:-}"; gz sim -v4 -r "$DRONE_DIR/world/rescue_7inch.sdf"; exec bash'

new_tab
title ArduPilot SITL
cd ~
launch --type=os-window --title="Rescue 7-inch - ArduCopter SITL" bash -lc 'sleep 3; "$HOME/ardupilot/build/sitl/bin/arducopter" --model JSON:127.0.0.1 --speedup 1 --sim-port-out=9003 --defaults "$DRONE_DIR/config/rescue_7inch.parm" --sim-address=127.0.0.1 -I0; exec bash'

new_tab
title MAVProxy
cd ~
launch --type=os-window --title="Rescue 7-inch - MAVProxy" bash -lc 'sleep 8; mavproxy.py --retries 5 --master tcp:127.0.0.1:5760 --sitl 127.0.0.1:5501 --out 127.0.0.1:14560 --out 127.0.0.1:14561; exec bash'

new_tab
title MAVROS
cd ~
launch --type=os-window --title="Rescue 7-inch - MAVROS" bash -lc 'sleep 10; source /opt/ros/humble/setup.bash; ros2 run mavros mavros_node --ros-args -p fcu_url:=udp://127.0.0.1:14561@; exec bash'

new_tab
title Camera Bridge
cd ~
launch --type=os-window --title="Rescue 7-inch - Camera Bridge" bash -lc 'sleep 10; source /opt/ros/humble/setup.bash; ros2 run ros_gz_bridge parameter_bridge --ros-args -p config_file:="$DRONE_DIR/start/camera_bridge.yaml"; exec bash'

new_tab
title Camera View
cd ~
launch --type=os-window --title="Rescue 7-inch - Camera View" bash -lc 'sleep 12; source /opt/ros/humble/setup.bash; python3 "$PROJECT_DIR/vision/camera_view.py"; exec bash'

new_tab
title Wind Controller
cd ~
launch --type=os-window --title="Rescue 7-inch - Wind Controller" bash -lc 'echo "Waiting 60 seconds for SITL/GPS/EKF initialization..."; sleep 60; "$DRONE_DIR/start/wind_control.sh" "$WIND_STRENGTH" "$WIND_DIRECTION" "$WIND_STRENGTH_RANDOMNESS" "$WIND_DIRECTION_RANDOMNESS" "$WIND_DURATION"; exec bash'

new_tab
title GCS
cd ~
launch --type=os-window --title="Rescue 7-inch - GCS" bash -lc 'python3 "$HOME/qr_gcs_receiver.py"; exec bash'

new_tab
title Mission
cd ~
launch --type=os-window --title="Rescue 7-inch - Mission" bash -lc 'sleep 14; source /opt/ros/humble/setup.bash; source "$PROJECT_DIR/ros2_ws/install/setup.bash"; ros2 run rescue_control qr_detector & ros2 run rescue_control geotag_mission; exec bash'
SESSION_EOF

chmod +x "$SESSION"
echo "Kitty session: $SESSION"
echo "Close the Kitty windows or press Ctrl+C here to stop the simulation."

kitty --session "$SESSION"

cleanup_simulation
