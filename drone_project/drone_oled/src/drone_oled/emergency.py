"""Evidence-based alarms. Display labels are not fabricated FC quotations."""

from dataclasses import dataclass
from .model import Snapshot


@dataclass(frozen=True)
class EmergencyEvent:
    kind: str
    text: str
    episode: int
    active: bool = True


class EmergencyDetector:
    def __init__(self, config: dict, profile: dict):
        self.c, self.profile = config, profile
        self.since: dict[str, float] = {}
        self.clear_since: dict[str, float] = {}
        self.active: dict[str, EmergencyEvent] = {}
        self.episode = 0
        self.seen_fc = False
        self.last_mode = None

    def raise_event(self, kind: str, text: str) -> EmergencyEvent | None:
        if kind in self.active:
            return None
        self.episode += 1
        event = EmergencyEvent(kind, text, self.episode)
        self.active[kind] = event
        return event

    def text(self, severity: int, text: str) -> EmergencyEvent | None:
        # Exact failsafe string matchers are firmware-profile fixtures, never invented here.
        matchers = self.profile.get("failsafe_texts", {})
        if text in matchers:
            return self.raise_event(matchers[text], text)
        if severity <= 2:
            return self.raise_event("FC TEXT " + text[:32], text)
        return None

    def update(self, s: Snapshot, expected_modes: set[str] | None = None) -> list[EmergencyEvent]:
        now = s.now
        if s.get("heartbeat"):
            self.seen_fc = True
        flags, fix = s.get("ekf_flags"), s.get("fix")
        voltage, remaining = s.get("pack_v"), s.get("remaining")
        minimum = self.profile.get("min_voltage_v")
        airborne = s.get("armed") is True and s.get("landed") != 1
        conditions = {
            "NO FC LINK": (
                self.seen_fc and s.age("heartbeat") >= self.c["mavlink"]["heartbeat_lost_s"],
                0,
                2,
            ),
            "RC HEALTH FAIL": (s.healthy(65536) is False, 0.5, 2),
            "BATT LOW": (
                (minimum is not None and voltage is not None and voltage < minimum)
                or (remaining is not None and remaining < self.c["readiness"]["min_remaining_pct"]),
                2,
                5,
            ),
            "GPS LOSS": (airborne and fix is not None and fix not in (3, 4, 5, 6, 8), 1, 5),
            "GPS GLITCH": (flags is not None and bool(flags & 32768), 1, 5),
            "EKF HEALTH FAIL": (
                airborne and flags is not None and (flags & 23 != 23 or bool(flags & (128 | 1024))),
                0.5,
                5,
            ),
            "FENCE BREACH": (s.get("fence_breach") == 1, 0, 2),
        }
        events = []
        for kind, (bad, debounce, clear_delay) in conditions.items():
            if bad:
                self.clear_since.pop(kind, None)
                started = self.since.setdefault(kind, now)
                if now - started >= debounce:
                    event = self.raise_event(kind, kind)
                    if event:
                        events.append(event)
            else:
                self.since.pop(kind, None)
                # Missing evidence must not clear an already active safety alarm.
                evidence = {
                    "NO FC LINK": s.get("heartbeat") is True,
                    "RC HEALTH FAIL": s.healthy(65536) is True,
                    "BATT LOW": voltage is not None
                    and remaining is not None
                    and minimum is not None
                    and voltage >= minimum + 0.3
                    and remaining >= self.c["readiness"]["min_remaining_pct"] + 5,
                    "GPS LOSS": fix in (3, 4, 5, 6, 8),
                    "GPS GLITCH": flags is not None and not flags & 32768,
                    "EKF HEALTH FAIL": flags is not None
                    and flags & 23 == 23
                    and not flags & (128 | 1024),
                    "FENCE BREACH": s.get("fence_breach") == 0,
                }[kind]
                if kind in self.active and evidence:
                    started = self.clear_since.setdefault(kind, now)
                    if now - started >= clear_delay:
                        old = self.active.pop(kind)
                        events.append(EmergencyEvent(kind, old.text, old.episode, False))
                else:
                    self.clear_since.pop(kind, None)
        mode = s.get("mode")
        if mode and self.last_mode and mode != self.last_mode and mode in ("RTL", "LAND"):
            if mode not in (expected_modes or set()):
                event = self.raise_event("MODE CHANGE", f"MODE {mode} CAUSE UNKNOWN")
                if event:
                    events.append(event)
        if mode:
            self.last_mode = mode
        return events

    def acknowledge(self, episode: int) -> None:
        # Persistent telemetry faults stay latched until evidence clears them.
        # Text-only faults cannot have a fabricated automatic clear condition.
        for kind, event in list(self.active.items()):
            if event.episode == episode and (kind.startswith("FC TEXT ") or kind == "MODE CHANGE"):
                del self.active[kind]
