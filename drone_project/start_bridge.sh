#!/usr/bin/env bash

source /opt/ros/humble/setup.bash

exec ros2 run ros_gz_bridge parameter_bridge \
  --ros-args \
  -p config_file:=$HOME/drone_project/camera_bridge.yaml
