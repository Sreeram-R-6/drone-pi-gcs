#!/usr/bin/env bash

# Publish a time-limited, changing physical wind profile to Gazebo Harmonic.
# Direction is degrees in the Gazebo XY plane: 0=+X, 90=+Y.
# Each new wind vector is applied abruptly every four seconds.

set -u

WORLD_NAME="rescue_7inch_disaster"
# WindEffects subscribes to /wind. /wind_info is a query service, not a
# command topic.
WIND_TOPIC="/world/${WORLD_NAME}/wind"
STRENGTH="${1:-}"
DIRECTION="${2:-}"
STRENGTH_RANDOMNESS="${3:-}"
DIRECTION_RANDOMNESS="${4:-}"
DURATION="${5:-}"

if [[ -z "$STRENGTH" || -z "$DIRECTION" || -z "$STRENGTH_RANDOMNESS" || -z "$DIRECTION_RANDOMNESS" || -z "$DURATION" ]]; then
  echo "Usage: $0 STRENGTH_MPS DIRECTION_DEG +/-STRENGTH_MPS +/-DIRECTION_DEG DURATION_SEC"
  echo "Example: $0 10 90 10 20 60"
  exit 2
fi

if ! awk -v s="$STRENGTH" -v d="$DIRECTION" -v sr="$STRENGTH_RANDOMNESS" -v dr="$DIRECTION_RANDOMNESS" -v t="$DURATION" \
  'BEGIN { exit !(s >= 0 && sr >= 0 && dr >= 0 && t > 0 && d == d) }'; then
  echo "Strength/randomness values must be >= 0, direction must be numeric, duration must be > 0."
  exit 2
fi

publish_wind() {
  local x="$1"
  local y="$2"
  gz topic -t "$WIND_TOPIC" -m gz.msgs.Wind \
    -p "linear_velocity: {x: ${x}, y: ${y}, z: 0}, enable_wind: true" \
    >/dev/null 2>&1 || true
}

calm() {
  publish_wind 0 0
}

trap calm INT TERM EXIT

echo "Wind topic: $WIND_TOPIC"
echo "Requested: ${STRENGTH} m/s +/- ${STRENGTH_RANDOMNESS} m/s"
echo "Direction: ${DIRECTION} deg +/- ${DIRECTION_RANDOMNESS} deg"
echo "Duration: ${DURATION} sec"

end_time="$(awk -v d="$DURATION" 'BEGIN { print systime() + d }')"

while awk -v end="$end_time" 'BEGIN { exit !(systime() < end) }'; do
  read -r x y <<EOF
$(awk -v s="$STRENGTH" -v deg="$DIRECTION" -v sr="$STRENGTH_RANDOMNESS" -v dr="$DIRECTION_RANDOMNESS" 'BEGIN {
  pi=atan2(0,-1); srand();
  mag=s + (rand()*2.0 - 1.0)*sr;
  if (mag < 0) mag=0;
  angle=(deg + (rand()*2.0 - 1.0)*dr)*pi/180.0;
  printf "%.4f %.4f", mag*cos(angle), mag*sin(angle)
}')
EOF
  publish_wind "$x" "$y"
  echo "Wind: x=${x} y=${y} m/s"
  sleep 4
done

calm
echo "Wind duration complete; returned to calm."
