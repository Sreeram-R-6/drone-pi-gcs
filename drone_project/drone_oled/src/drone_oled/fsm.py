"""Pure navigation/effects; rendering cannot send vehicle commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from .input import KeyEvent
from .model import Snapshot, identity_ok, readiness, fc_arm_available
from .missions import MissionDefinition
from .emergency import EmergencyEvent
from .font import wrap_text

MENU = ("ARM DRONE", "MISSIONS", "TEST MOTORS", "SENSOR CAL", "FAILSAFE", "RESERVED")
CAL = ("GYRO", "ACCEL 6-AXIS", "COMPASS", "BARO ZERO", "OPTICAL FLOW")
PARAMS = ("FS_THR_ENABLE", "FENCE_ENABLE", "FENCE_RADIUS", "FENCE_ALT_MAX", "BATT_LOW_VOLT")


@dataclass(frozen=True)
class Effect:
    kind: str
    data: Any = None


@dataclass
class ScreenState:
    id: str = "boot"
    generation: int = 0
    changed_at: float = 0
    focus: int = 0
    page: int = 0
    mission_index: int = 0
    step: int = 0
    motor: int = 0
    caller: str = "home"
    confirm_kind: str = ""
    pending: bool = False
    operation: str = ""
    phase: str = ""
    message: str = ""
    alert_title: str = "NOTICE"
    deadline: float = 0
    started_at: float = 0
    emergency: EmergencyEvent | None = None
    return_id: str = "home"
    draft: tuple[str, float] | None = None
    acknowledged: set[int] = field(default_factory=set)


class FSM:
    def __init__(self, config: dict, profile: dict, missions: list[MissionDefinition]):
        self.c, self.profile, self.missions = config, profile, missions
        self.state = ScreenState()
        self.faults: list[str] = []

    def move(self, target: str, now: float, **values) -> None:
        s = self.state
        s.id, s.generation, s.changed_at = target, s.generation + 1, now
        for key, value in values.items():
            setattr(s, key, value)

    def alert(self, message: str, now: float, caller: str = "home", title: str = "NOTICE"):
        self.move(
            "alert", now, message=message, caller=caller, alert_title=title, pending=False, page=0
        )

    def mission(self) -> MissionDefinition | None:
        return self.missions[self.state.mission_index] if self.missions else None

    def guard(self, kind: str, snap: Snapshot) -> list[str]:
        if not self.c["safety"]["commands_enabled"]:
            return ["CONTROLS DISABLED"]
        flag = {
            "arm": "arm_enabled",
            "disarm": "arm_enabled",
            "start": "mission_enabled",
            "spin": "motor_test_enabled",
            "land": "commands_enabled",
            "cal": "calibration_enabled",
            "mag_accept": "calibration_enabled",
            "write": "parameter_write_enabled",
        }[kind]
        if not self.c["safety"][flag]:
            return ["FEATURE DISABLED"]
        delegated = fc_arm_available(snap, self.c, kind)
        if not delegated and not identity_ok(snap, self.profile):
            return ["FC IDENTITY UNREADY"]
        if kind == "land":
            return [] if snap.get("armed") is True else ["VEHICLE NOT ARMED"]
        if kind == "disarm":
            return [] if snap.get("landed") == 1 else ["DISARM BLOCKED"]
        if snap.get("armed") is not False or snap.get("landed") != 1:
            return ["MUST BE DISARMED/LANDED"]
        if kind in ("spin", "cal", "mag_accept", "write"):
            if kind == "spin" and not self.profile.get("motor_positions"):
                return ["MOTOR MAP UNREVIEWED"]
            return []
        reasons = (
            []
            if delegated and kind == "arm"
            else readiness(snap, self.c, self.profile, self.faults)
        )
        if kind == "start":
            mission = self.mission()
            if not mission or not mission.executable:
                reasons.insert(
                    0, mission.errors[0] if mission and mission.errors else "MISSION NOT EXECUTABLE"
                )
            if not self.profile.get("recovery_reviewed"):
                reasons.insert(0, "RECOVERY UNREVIEWED")
            if not snap.cached("home"):
                reasons.insert(0, "HOME UNKNOWN")
        return reasons

    def confirm(self, kind: str, caller: str, now: float, snap: Snapshot) -> None:
        reasons = self.guard(kind, snap)
        if reasons:
            self.alert("; ".join(reasons), now, caller, "CANNOT " + kind.upper())
        else:
            self.move("confirm", now, focus=0, confirm_kind=kind, caller=caller, pending=False)

    def emergency(self, event: EmergencyEvent, now: float) -> list[Effect]:
        s = self.state
        if not event.active or event.episode in s.acknowledged:
            return []
        returning = "run" if s.id == "run" else "home"
        if s.id == "emergency":
            returning = s.return_id
        cancel = s.pending or s.id in ("run", "mtest")
        self.move(
            "emergency",
            now,
            emergency=event,
            return_id=returning,
            pending=False,
            draft=None,
            page=0,
        )
        return [Effect("cancel")] if cancel else []

    def tick(self, snap: Snapshot, now: float) -> list[Effect]:
        s = self.state
        if s.id == "boot":
            status = snap.get("pi_state", max_age=self.c["pi"]["stale_s"])
            if status in ("running", "degraded"):
                self.move("home", now, focus=0)
            elif now - s.started_at >= self.c["display"]["boot_escape_s"]:
                self.alert("PI BOOT STATE UNKNOWN", now, title="BOOT TIMEOUT")
        elif s.id == "count" and now >= s.deadline:
            reasons = self.guard("start", snap)
            if reasons:
                self.alert("; ".join(reasons), now, "mdetail", "CANNOT START")
            else:
                self.move("run", now, pending=True, operation="mission", phase="PREPARING")
                return [Effect("mission", self.mission())]
        elif s.id == "mtest" and now >= s.deadline and not s.pending:
            s.pending, s.phase = True, "STOPPING"
            return [Effect("motor_stop", s.motor + 1)]
        elif s.id == "run" and s.phase == "RUNNING":
            if snap.get("mode") not in (None, "AUTO") or snap.get("mission_state") == 4:
                self.alert("PILOT/FC LEFT AUTO; MISSION SUSPENDED", now, title="SUSPENDED")
                return []
            mission = self.mission()
            reached = snap.get("mission_reached")
            completed = snap.get("mission_state") == 5
            terminal = mission and reached == len(mission.items) - 1
            if completed and (terminal or snap.get("landed") == 1) and snap.get("armed") is False:
                self.alert("MISSION DONE", now, title="COMPLETE")
        elif s.id == "armed" and snap.get("armed") is False:
            self.move("home", now)
        return []

    def operation(self, kind: str, status: str, detail: str, now: float) -> None:
        s = self.state
        if kind == "read_params":
            return
        if status == "progress":
            s.phase = detail
            return
        s.pending = False
        if s.id == "emergency":
            s.message = detail
            return
        if status != "success":
            self.alert(detail, now, "home", "ACTION FAILED")
            return
        if kind == "arm":
            self.move("armed", now)
        elif kind == "disarm":
            self.move("home", now)
        elif kind == "motor_start":
            self.move(
                "mtest",
                now,
                deadline=now + self.c["motors"]["duration_s"],
                started_at=now,
                phase="SPINNING",
                pending=False,
            )
        elif kind == "motor_stop":
            self.alert(detail, now, "motors", "TEST ENDED")
        elif kind == "mission":
            self.move("run", now, phase="RUNNING", pending=False)
        elif kind == "land":
            self.move("emergency", now, message="LAND REQUESTED")
        elif kind == "abort":
            self.alert(detail, now, "home", "ABORT REQUESTED")
        else:
            self.alert(
                detail, now, "cal" if kind.startswith("cal") or kind == "mag_accept" else "home"
            )

    def key(self, event: KeyEvent, snap: Snapshot, now: float) -> list[Effect]:
        s, key = self.state, event.key
        urgent = key == "back" and s.id in ("count", "run", "mtest", "emergency")
        if event.generation != s.generation:
            return []
        if now - s.changed_at < self.c["buttons"]["transition_guard_ms"] / 1000 and not urgent:
            return []
        if event.kind == "repeat" and s.id not in (
            "status",
            "menu",
            "missions",
            "mdetail",
            "cal",
            "failsafe",
        ):
            return []
        if event.kind == "long" and key == "back" and not urgent:
            if not s.pending:
                self.move("home", now, draft=None)
            return []
        if s.id == "boot":
            if key == "ok":
                self.move("home", now, focus=0)
        elif s.id == "home":
            if key in ("left", "right"):
                s.focus = int(key == "right")
            elif key == "ok":
                self.move("status" if s.focus == 0 else "menu", now, focus=0, page=0)
        elif s.id == "status":
            if key in ("up", "left", "down", "right"):
                s.page = (s.page + (-1 if key in ("up", "left") else 1)) % 5
            elif key == "back":
                self.move("home", now)
        elif s.id == "menu":
            if key in ("up", "down"):
                s.focus = (s.focus + (1 if key == "down" else -1)) % 6
            elif key == "back":
                self.move("home", now)
            elif key == "ok":
                if s.focus == 0:
                    self.confirm("disarm" if snap.get("armed") else "arm", "menu", now, snap)
                elif s.focus == 1:
                    self.move("missions", now, mission_index=0)
                elif s.focus == 2:
                    self.move("motors", now, motor=0)
                elif s.focus in (3, 4):
                    self.move("cal" if s.focus == 3 else "failsafe", now, focus=0, draft=None)
                    if s.id == "failsafe":
                        return [Effect("read_params", PARAMS[:1])]
        elif s.id == "missions":
            if key in ("up", "down") and self.missions:
                s.mission_index = (s.mission_index + (1 if key == "down" else -1)) % len(
                    self.missions
                )
            elif key == "ok":
                if self.missions:
                    self.move("mdetail", now, step=0)
                else:
                    self.alert("NO MISSIONS", now, "missions")
            elif key == "back":
                self.move("menu", now)
        elif s.id == "mdetail":
            mission = self.mission()
            if key in ("up", "down") and mission and mission.steps:
                s.step = (s.step + (1 if key == "down" else -1)) % len(mission.steps)
            elif key == "ok":
                self.confirm("start", "mdetail", now, snap)
            elif key == "back":
                self.move("missions", now)
        elif s.id == "confirm":
            if s.pending:
                if key == "back":
                    return [Effect("cancel")]
            elif key in ("up", "down", "left", "right"):
                s.focus ^= 1
            elif key == "back" or (key == "ok" and s.focus == 0):
                self.move(s.caller, now, draft=None)
            elif key == "ok":
                kind = s.confirm_kind
                reasons = self.guard(kind, snap)
                if reasons:
                    self.alert("; ".join(reasons), now, s.caller)
                elif kind == "start":
                    self.move("count", now, deadline=now + self.c["missions"]["countdown_s"])
                else:
                    s.pending, s.operation, s.phase = True, kind, "WAITING FOR FC"
                    action = {
                        "spin": "motor_start",
                        "cal": "calibrate",
                        "write": "write_param",
                    }.get(kind, kind)
                    data: Any = s.motor + 1 if kind == "spin" else None
                    if kind == "cal":
                        data = s.message
                    elif kind == "write":
                        data = s.draft
                    return [Effect(action, data)]
        elif s.id == "count" and key == "back":
            self.move("mdetail", now)
        elif s.id == "run" and key == "back":
            if not s.pending:
                s.pending, s.phase = True, "ABORTING"
                return [Effect("abort")]
            return [Effect("cancel")]
        elif s.id == "armed" and key in ("ok", "back"):
            self.confirm("disarm", "armed", now, snap)
        elif s.id == "motors":
            if key in ("prev", "next"):
                s.motor = (s.motor + (-1 if key == "prev" else 1)) % 4
            elif key in ("up", "down"):
                s.motor ^= 2
            elif key in ("left", "right"):
                s.motor ^= 1
            elif key == "ok":
                self.confirm("spin", "motors", now, snap)
            elif key == "back":
                self.move("menu", now)
        elif s.id == "mtest" and key == "back":
            if not s.pending:
                s.pending, s.phase = True, "STOPPING"
                return [Effect("motor_stop", s.motor + 1)]
        elif s.id == "alert":
            if key in ("up", "left", "down", "right"):
                pages = max(1, (len(wrap_text(s.message)) + 2) // 3)
                s.page = (s.page + (-1 if key in ("up", "left") else 1)) % pages
            elif key in ("ok", "back"):
                self.move(s.caller, now, draft=None)
        elif s.id == "emergency":
            if (
                key in ("up", "left", "down", "right")
                and s.emergency
                and s.emergency.kind.startswith("FC TEXT ")
            ):
                pages = max(1, (len(wrap_text(s.emergency.text)) + 3) // 4)
                s.page = (s.page + (-1 if key in ("up", "left") else 1)) % pages
                return []
            if key == "back":
                if s.emergency:
                    s.acknowledged.add(s.emergency.episode)
                self.move(s.return_id, now)
                return [Effect("acknowledge", s.emergency)]
            if key == "ok":
                self.confirm("land", "emergency", now, snap)
        elif s.id in ("cal", "failsafe"):
            count = len(CAL) if s.id == "cal" else len(PARAMS)
            if s.id == "failsafe" and s.draft:
                if key == "back":
                    s.draft = None
                    return []
                if key in ("up", "down"):
                    key = "left" if key == "up" else "right"
            if key in ("up", "down"):
                s.focus = (s.focus + (1 if key == "down" else -1)) % count
                s.draft = None
                if s.id == "failsafe":
                    return [Effect("read_params", (PARAMS[s.focus],))]
            elif key == "back":
                self.move("menu", now, draft=None)
            elif s.id == "cal" and key == "ok":
                selection = CAL[s.focus]
                if selection in ("ACCEL 6-AXIS", "OPTICAL FLOW"):
                    self.alert("USE REVIEWED GCS CAL", now, "cal")
                else:
                    s.message = selection
                    self.confirm(
                        "mag_accept"
                        if selection == "COMPASS" and snap.cached("mag_cal_ready")
                        else "cal",
                        "cal",
                        now,
                        snap,
                    )
            elif s.id == "failsafe":
                name = PARAMS[s.focus]
                spec = self.profile.get("parameters", {}).get(name)
                current = snap.get("param_" + name)
                if (
                    key in ("left", "right")
                    and spec
                    and current is not None
                    and self.c["safety"]["parameter_write_enabled"]
                ):
                    old = s.draft[1] if s.draft else current[0]
                    if spec.get("values"):
                        choices = spec["values"]
                        candidates = (
                            [v for v in choices if v > old]
                            if key == "right"
                            else [v for v in choices if v < old]
                        )
                        new = (
                            (min(candidates) if key == "right" else max(candidates))
                            if candidates
                            else old
                        )
                    else:
                        new = old + spec["step"] * (1 if key == "right" else -1)
                    s.draft = (name, min(spec["max"], max(spec["min"], new)))
                elif key == "ok":
                    if spec and s.draft:
                        self.confirm("write", "failsafe", now, snap)
                    elif (
                        spec and current is not None and self.c["safety"]["parameter_write_enabled"]
                    ):
                        reasons = self.guard("write", snap)
                        if reasons:
                            self.alert("; ".join(reasons), now, "failsafe")
                        else:
                            s.draft = (name, current[0])
                    else:
                        self.alert("READ ONLY / NO EDIT", now, "failsafe")
        return []
