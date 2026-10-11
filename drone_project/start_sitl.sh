#!/usr/bin/env bash

cd ~/ardupilot

./Tools/autotest/sim_vehicle.py \
  -v ArduCopter \
  -f gazebo-iris \
  --model JSON \
  --console \
  --map \
  --out=127.0.0.1:14550 \
  --out=127.0.0.1:14551
