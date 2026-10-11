"""Validated local missions; display fixtures cannot silently become flight plans."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path

ALLOWED = {16, 20, 21, 22, 19, 93, 112, 178}
LOCATION = {16, 19, 21, 22}


class MissionInvalid(ValueError):
    pass


def display_string(text: str, maximum: int = 16) -> str:
    if not isinstance(text, str) or not text or len(text) > maximum:
        raise MissionInvalid(f"Text must contain 1..{maximum} characters: {text!r}")
    if text != text.upper() or any(not 32 <= ord(char) <= 90 for char in text):
        raise MissionInvalid(f"Unsupported bitmap-font text: {text!r}")
    return text


@dataclass(frozen=True)
class MissionItem:
    seq: int
    command: int
    frame: int
    params: tuple[float, float, float, float]
    x: int
    y: int
    z: float

    def wire(self) -> tuple:
        return (self.command, self.frame, *self.params, self.x, self.y, self.z)

    def label(self) -> tuple[str, str]:
        altitude = f"ALT {self.z:g}M"
        names = {
            22: ("TAKEOFF", altitude),
            16: (f"GOTO WP{self.seq}", altitude),
            20: ("RTL", "FC RTL PROFILE"),
            21: ("LAND", "FC LAND PROFILE"),
            19: ("HOVER", f"HOLD {self.params[0]:g}S"),
            93: ("DELAY", f"WAIT {self.params[0]:g}S"),
            112: ("COND DELAY", f"WAIT {self.params[0]:g}S"),
            178: ("SET SPEED", f"SPD {self.params[1]:g}M/S"),
        }
        return names.get(self.command, (f"CMD {self.command}", "UNREVIEWED"))


@dataclass(frozen=True)
class MissionDefinition:
    name: str
    items: tuple[MissionItem, ...] = ()
    steps: tuple[tuple[str, str], ...] = ()
    errors: tuple[str, ...] = ()
    content_hash: str = ""

    @property
    def executable(self) -> bool:
        return bool(self.items and not self.errors)


@dataclass
class MissionStore:
    directory: Path
    maximum: int = 99
    missions: list[MissionDefinition] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def load_all(self) -> list[MissionDefinition]:
        self.missions, self.errors = [], []
        if not self.directory.exists():
            return []
        for path in sorted(self.directory.iterdir()):
            if path.suffix.lower() not in (".json", ".plan") or not path.is_file():
                continue
            try:
                self.missions.append(self.load(path))
            except (ValueError, KeyError, TypeError, OverflowError, OSError) as exc:
                self.errors.append(f"{path.name}: {exc}")
        return self.missions

    def load(self, path: Path) -> MissionDefinition:
        if path.stat().st_size > 1024 * 1024:
            raise MissionInvalid("Mission file exceeds 1 MiB")
        raw = path.read_bytes()
        data = json.loads(raw)
        name = display_string(data.get("name", path.stem.upper()))
        if data.get("display_only") is True:
            steps = tuple(
                (display_string(x["name"]), display_string(x.get("params", "-")))
                for x in data["steps"]
            )
            if not 1 <= len(steps) <= self.maximum:
                raise MissionInvalid("Invalid step count")
            return MissionDefinition(
                name,
                steps=steps,
                errors=("DISPLAY ONLY",),
                content_hash=hashlib.sha256(raw).hexdigest(),
            )
        incoming = data.get("items")
        qgc = path.suffix == ".plan"
        if qgc:
            if data.get("fileType") != "Plan" or data.get("version") != 1:
                raise MissionInvalid("Unsupported QGC plan version")
            qmission = data["mission"]
            if (
                qmission.get("version") != 2
                or qmission.get("firmwareType") != 3
                or qmission.get("vehicleType") != 2
            ):
                raise MissionInvalid(
                    "QGC mission must target ArduPilot quadcopter, mission version 2"
                )
            if any(data.get("geoFence", {}).get(k) for k in ("circles", "polygons")) or data.get(
                "rallyPoints", {}
            ).get("points"):
                raise MissionInvalid(
                    "Fence/rally import is unsupported; do not silently discard safety data"
                )
            incoming = qmission["items"]
            # ArduPilot mission sequence zero represents home. Do not invent launch coordinates.
            home = data["mission"].get("plannedHomePosition")
            if not home or len(home) != 3:
                raise MissionInvalid("QGC planned home is required")
            incoming = [
                {"type": "SimpleItem", "command": 16, "frame": 0, "params": [0, 0, 0, 0, *home]}
            ] + incoming
        if not isinstance(incoming, list) or not 3 <= len(incoming) <= self.maximum + 1:
            raise MissionInvalid("Explicit home, takeoff and terminal landing/RTL required")
        items, errors = [], []
        for seq, entry in enumerate(incoming):
            if qgc and entry.get("type") != "SimpleItem":
                raise MissionInvalid("QGC complex items must be expanded before import")
            command, frame = entry["command"], entry["frame"]
            if type(command) is not int or not 0 <= command <= 65535:
                raise MissionInvalid("Invalid command ID")
            if (
                type(frame) is not int
                or frame not in (0, 2, 3, 5, 6)
                or (frame == 2 and command in LOCATION)
            ):
                raise MissionInvalid("Unsupported mission frame for command")
            if entry.get("autoContinue", True) is not True:
                raise MissionInvalid("Paused/non-autocontinue items are unsupported")
            p = list(entry["params"])
            null_yaw = len(p) == 7 and p[3] is None and command in LOCATION
            if null_yaw:
                p[3] = 0  # Validate all other values first; preserve 'current yaw' as NaN on wire.
            if len(p) != 7 or any(
                not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v)
                for v in p
            ):
                raise MissionInvalid("Seven finite numeric params required")
            if frame == 2:
                if any(p[4:]):
                    raise MissionInvalid("Non-location frame requires zero unused x/y/z")
                x, y = 0, 0
            elif qgc or frame in (0, 3):
                x, y = round(p[4] * 1e7), round(p[5] * 1e7)
                frame = 5 if frame in (0, 5) else 6
            else:
                if not isinstance(p[4], int) or not isinstance(p[5], int):
                    raise MissionInvalid("INT frame requires integer lat/lon E7")
                x, y = p[4], p[5]
            if not -900000000 <= x <= 900000000 or not -1800000000 <= y <= 1800000000:
                raise MissionInvalid("Invalid coordinates")
            wire_params = (
                float(p[0]),
                float(p[1]),
                float(p[2]),
                math.nan if null_yaw else float(p[3]),
            )
            item = MissionItem(seq, command, frame, wire_params, x, y, float(p[6]))
            if command not in ALLOWED:
                errors.append(f"CMD {command} UNREVIEWED")
            if command in (19, 93, 112) and p[0] < 0:
                errors.append("ABSOLUTE/NEGATIVE DELAY UNREVIEWED")
            if command == 178 and (p[0] not in (0, 1) or p[1] <= 0):
                errors.append("SPEED TYPE/VALUE UNREVIEWED")
            label, params = item.label()
            display_string(label)
            display_string(params)
            items.append(item)
        if items[0].command != 16 or items[1].command != 22 or items[-1].command not in (20, 21):
            errors.append("HOME/TAKEOFF/END INVALID")
        if any(i.command in (16, 19, 22) and i.seq > 0 and i.z <= 0 for i in items):
            errors.append("NONPOSITIVE FLIGHT ALT")
        return MissionDefinition(
            name,
            tuple(items),
            tuple(i.label() for i in items[1:]),
            tuple(errors),
            hashlib.sha256(raw).hexdigest(),
        )
