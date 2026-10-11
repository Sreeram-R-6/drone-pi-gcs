#!/usr/bin/env python3
"""Run the project's existing ROS 2 geotag mission from ``picode``.

This deliberately delegates to the canonical implementation so the Pi entry
point cannot silently drift away from the simulation-tested mission.  The
repository's ROS 2 workspace must be built and sourced, MAVROS must be
connected to the FC, and the real downward camera must publish
``/camera/down/image`` before this entry point is used.
"""

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROS_SOURCE = PROJECT_ROOT / "ros2_ws" / "src" / "rescue_control"

if not ROS_SOURCE.is_dir():
    raise SystemExit(f"rescue_control source package not found: {ROS_SOURCE}")

sys.path.insert(0, str(ROS_SOURCE))

try:
    from rescue_control.geotag_mission import main
except ImportError as error:
    raise SystemExit(
        "Unable to import the geotag mission. Source ROS 2 Humble and the "
        "built workspace before running this file. Original error: "
        f"{error}"
    ) from error


if __name__ == "__main__":
    main()
