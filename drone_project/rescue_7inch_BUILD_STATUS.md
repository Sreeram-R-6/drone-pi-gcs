# Rescue 7-Inch Simulation Build Status

## Stage 1 — Project Isolation
- [x] rescue_7inch directory
- [x] Iris reference preserved

## Stage 2 — Physical Airframe
- [x] 1.500 kg total mass target
- [x] 7-inch geometry assumption
- [x] 320 mm initial motor diagonal
- [x] symmetric motor layout
- [ ] final measured geometry
- [ ] final measured inertia

## Stage 3 — Propulsion
- [ ] 2807 1350KV physics
- [ ] 7x4 propulsion tuning
- [ ] motor direction validation
- [ ] thrust validation

## Stage 4 — Sensors
- [x] IMU
- [ ] barometer validation
- [ ] GPS validation
- [ ] compass validation
- [x] front camera removed
- [x] downward camera

## Stage 5 — ArduPilot
- [ ] ArduPilotPlugin validation
- [ ] JSON connection
- [ ] IMU namespace validation
- [ ] motor channel validation

## Stage 6 — Flight
- [ ] spawn
- [ ] connect
- [ ] arm
- [ ] takeoff
- [ ] hover
- [ ] position control
- [ ] RTL

## Stage 7 — Environment
- [ ] terrain
- [ ] structures
- [ ] debris
- [ ] QR target

## Stage 8 — Wind
- [ ] 0 m/s
- [ ] 5 m/s
- [ ] gusts
- [ ] 10 m/s

## Stage 9 — ROS 2
- [ ] MAVROS
- [ ] camera bridge
- [ ] QR detector
- [ ] mission controller

## Stage 10 — Mission
- [ ] search
- [ ] geotag
- [ ] QR
- [ ] UDP
- [ ] RTL
