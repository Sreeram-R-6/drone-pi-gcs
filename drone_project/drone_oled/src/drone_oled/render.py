"""Pure render functions: state + telemetry + time -> 128x64 one-bit image."""

from __future__ import annotations

import math
from pathlib import Path
from functools import lru_cache
from PIL import Image

from .gfx import Canvas, CHECK, CROSS, WIFI, ETH
from .font import compact, width
from .fsm import FSM, MENU, CAL, PARAMS
from .model import Snapshot, readiness, home_geometry

FIXES = {
    0: "NONE",
    1: "NONE",
    2: "2D",
    3: "3D",
    4: "DGPS",
    5: "FLOAT",
    6: "FIXED",
    7: "STATIC",
    8: "PPP",
}


def fmt(value, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "--"
    if isinstance(value, str):
        return value.upper()
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if not math.isfinite(value):
        return "--"
    return f"{value:.{digits}f}" + suffix


@lru_cache(maxsize=4)
def splash(path: str) -> Image.Image:
    with Image.open(path) as image:
        if image.size != (128, 64) or image.mode != "1":
            raise ValueError("Splash must be a prepared 128x64 monochrome PNG")
        return image.copy()


def cells(canvas: Canvas, entries: list[tuple[str, str, bool]]) -> None:
    for index, (key, value, failed) in enumerate(entries[:6]):
        x, y = (2, 65)[index % 2], (13, 29, 45)[index // 2]
        color = canvas.block(x, y, 61, 14, failed)
        key = compact(key, 23)
        canvas.text(x + 3, y + 4, key, color=color)
        available = 52 - width(key) - 3
        value = compact(value, available)
        canvas.text(x + 57 - width(value), y + 4, value, color=color)


def row_list(
    canvas: Canvas,
    entries: list[str],
    selected: int,
    y: int = 12,
    row_h: int = 15,
    spacing: int = 17,
    w: int = 119,
):
    start = max(0, min(selected - 1, len(entries) - 3))
    for slot, label in enumerate(entries[start : start + 3]):
        index = start + slot
        color = canvas.block(2, y + slot * spacing, w, row_h, index == selected)
        canvas.text(7, y + slot * spacing + 4, label, color=color, max_width=w - 10)
    canvas.scroll(selected, len(entries), 3, y, 49)


def render(fsm: FSM, snap: Snapshot, now: float) -> Image.Image:
    s, c = fsm.state, fsm.c
    cv = Canvas()
    get = snap.get
    mission = fsm.mission()
    if s.id == "boot":
        path = c["display"]["splash"] or str(Path(__file__).parent / "assets" / "splash.png")
        try:
            return splash(path).copy()
        except (OSError, ValueError):
            cv.center(12, "DVG", 3)
            cv.center(39, "FPV", 2)
            cv.center(56, "PI STARTING")
    elif s.id == "home":
        pi = get("pi_state", max_age=c["pi"]["stale_s"])
        dot = pi in ("running", "degraded") or int(now * 2) % 2 == 0
        if dot:
            cv.draw.ellipse((2, 2, 6, 6), fill=1)
        cv.text(10, 2, "PI OK" if pi in ("running", "degraded") else "PI..")
        if pi == "degraded":
            cv.text(42, 2, "!")
        wifi = get("wifi", max_age=c["pi"]["stale_s"])
        eth = get("ethernet", max_age=c["pi"]["stale_s"])
        edge = 126
        if wifi is not None:
            label = fmt(wifi, 0, "DBM")
            cv.text(edge - width(label), 2, label)
            edge -= width(label) + 2
            cv.icon(edge - 11, 2, WIFI)
            edge -= 13
        if eth:
            cv.icon(edge - 9, 1, ETH)
        if wifi is None and not eth:
            cv.text(126 - width("NO LINK"), 2, "NO LINK")
        cv.draw.line((0, 11, 127, 11), fill=1)
        problems = readiness(snap, c, fsm.profile, fsm.faults)
        armed = get("armed") is True
        label = (
            "NO FC LINK"
            if not get("heartbeat")
            else "ARMED"
            if armed
            else "NOT READY"
            if problems
            else "READY"
        )
        failed = armed or bool(problems)
        color = cv.block(2, 14, 70, 11, failed)
        cv.text(4, 16, label, color=color)
        cv.icon(65, 17, CROSS if problems else CHECK, color)
        cv.block(74, 14, 52, 11)
        cv.text(79, 16, "SAT " + fmt(get("sats"), 0), max_width=44)
        for index, (x, label) in enumerate(((2, "STATUS"), (65, "MENU"))):
            color = cv.block(x, 28, 61, 34, s.focus == index)
            if index == 0:
                for offset, h in ((0, 4), (6, 8), (12, 12)):
                    cv.draw.rectangle((x + 22 + offset, 46 - h, x + 25 + offset, 45), fill=color)
                cv.draw.line((x + 21, 47, x + 38, 47), fill=color)
            else:
                for yy in (34, 39, 44):
                    cv.draw.rectangle((x + 22, yy, x + 37, yy + 2), fill=color)
            cv.text(x + (61 - width(label)) // 2, 51, label, color=color)
        if get("heartbeat") is None:
            # Keep the block geometry; link absence is also explicit on STATUS/SYSTEM.
            cv.draw.point((125, 24), fill=1)
    elif s.id == "status":
        titles = ("SENSORS", "GPS", "FLIGHT", "POWER", "SYSTEM")
        cv.title(titles[s.page], f"{s.page + 1}/5")
        if s.page == 0:
            for i, (name, mask) in enumerate(
                (
                    ("IMU", 3),
                    ("BARO", 8),
                    ("MAG", 4),
                    ("GPS", 32),
                    ("RC", 65536),
                    ("BATT", 33554432),
                )
            ):
                x, y = (2, 44, 86)[i % 3], (13, 37)[i // 3]
                health = snap.healthy(mask)
                color = cv.block(x, y, 40, 22, health is not True)
                cv.text(x + (40 - width(name)) // 2, y + 4, name, color=color)
                if health is None:
                    cv.text(x + 14, y + 13, "--", color=color)
                else:
                    cv.icon(x + 16, y + 13, CHECK if health else CROSS, color)
        elif s.page == 1:
            gps_fields = [
                ("SAT", "sats", 0),
                ("FIX", "fix", 0),
                ("HDOP", "hdop", 1),
                ("VDOP", "vdop", 1),
                ("MSL", "gps_alt", 0),
                ("SPD", "gps_speed", 1),
            ]
            cells(
                cv,
                [
                    (k, FIXES.get(get(v), "--") if v == "fix" else fmt(get(v), n), get(v) is None)
                    for k, v, n in gps_fields
                ],
            )
        elif s.page == 2:
            roll, pitch = get("roll"), get("pitch")
            cv.block(2, 12, 43, 48, roll is None or pitch is None)
            if roll is not None and pitch is not None:
                tile = Image.new("1", (24, 24))
                from PIL import ImageDraw

                draw = ImageDraw.Draw(tile)
                angle = math.radians(roll)
                for degree in (-20, -10, 0, 10, 20):
                    off = max(-36, min(36, (pitch - degree) * 0.4))
                    half = 20 if degree == 0 else 7
                    points = []
                    for sign in (-1, 1):
                        xx = sign * half
                        points.append(
                            (
                                round(12 + xx * math.cos(angle) - off * math.sin(angle)),
                                round(12 + xx * math.sin(angle) + off * math.cos(angle)),
                            )
                        )
                    draw.line((*points[0], *points[1]), fill=1)
                draw.line((8, 12, 10, 12), fill=1)
                draw.line((14, 12, 16, 12), fill=1)
                draw.point((12, 12), fill=1)
                cv.image.paste(tile, (14, 13))
            cv.text(5, 43, "P" + fmt(pitch, 0), max_width=36)
            cv.text(5, 52, "R" + fmt(roll, 0), max_width=36)
            for row, (key, value) in enumerate(
                (
                    ("ALT", fmt(get("alt"), 1)),
                    ("SPD", fmt(get("speed"), 1)),
                    ("MODE", get("mode", "--")),
                )
            ):
                color = cv.block(48, 13 + row * 16, 78, 15, value == "--")
                cv.text(52, 17 + row * 16, key, color=color)
                val = compact(value, 44)
                cv.text(122 - width(val), 17 + row * 16, val, color=color)
        elif s.page == 3:
            pack, cell = get("pack_v"), None
            measured = get("cells")
            cellkey = "AVG"
            if measured:
                index = int(now / 2) % len(measured)
                cell, cellkey = measured[index], f"C{index + 1}"
            elif pack is not None and c["battery"]["verified_cell_count"]:
                cell = pack / c["battery"]["verified_cell_count"]
            temp = get("battery_temp")
            entries: list[tuple[str, str, bool]] = [
                ("V", fmt(pack, 1), pack is None),
                (cellkey, fmt(cell, 2), cell is None),
                ("A", fmt(get("current_a"), 1), get("current_a") is None),
                ("MAH", fmt(get("consumed"), 0), get("consumed") is None),
                ("%", fmt(get("remaining"), 0), get("remaining") is None),
                ("TEMP", fmt(temp, 0), temp is None),
            ]
            if get("battery_time") is not None and int(now / 4) % 2:
                entries[-1] = ("EST", fmt(get("battery_time"), 0), False)
            cells(cv, entries)
        else:
            items = [
                ("MODE", get("mode")),
                ("ARM", None if get("armed") is None else "YES" if get("armed") else "NO"),
                ("LINK", "OK" if get("heartbeat") else "LOST"),
                ("CPU", fmt(get("cpu", max_age=3), 0)),
                ("TEMP", fmt(get("pi_temp", max_age=3), 0)),
                ("WIFI", fmt(get("wifi", max_age=3), 0)),
            ]
            for i, (key, value) in enumerate(items):
                x, y = (2, 65)[i % 2], (12, 26, 40)[i // 2]
                color = cv.block(x, y, 61, 13, value in (None, "--", "LOST"))
                cv.text(x + 3, y + 3, key, color=color, max_width=23)
                val = compact(value or "--", 28)
                cv.text(x + 57 - width(val), y + 3, val, color=color)
            cv.text(2, 56, "IP " + get("ip", "--", max_age=3), max_width=124)
    elif s.id == "menu":
        cv.title("MENU", f"{s.focus + 1}/6")
        labels = list(MENU)
        if get("armed"):
            labels[0] = "DISARM"
        start = max(0, min(s.focus - 1, 3))
        for slot, index in enumerate(range(start, min(6, start + 3))):
            y = 12 + slot * 17
            color = cv.block(2, y, 119, 15, index == s.focus)
            cv.text(7, y + 4, str(index + 1), color=color)
            cv.draw.line((18, y + 1, 18, y + 13), fill=color)
            cv.text(24, y + 4, labels[index], color=color, max_width=88)
            if index == s.focus:
                cv.text(114, y + 4, ">", color=color)
        cv.scroll(s.focus, 6, 3)
    elif s.id == "missions":
        cv.title("MISSIONS", f"{s.mission_index + 1}/{len(fsm.missions)}" if fsm.missions else "0")
        start = max(0, min(s.mission_index - 1, len(fsm.missions) - 3))
        for slot, listed in enumerate(fsm.missions[start : start + 3]):
            y = 12 + slot * 17
            color = cv.block(2, y, 119, 15, start + slot == s.mission_index)
            cv.text(7, y + 4, compact(listed.name, 71), color=color)
            count = f"{len(listed.steps)} STP"
            cv.text(117 - width(count), y + 4, count, color=color)
        cv.scroll(s.mission_index, len(fsm.missions), 3)
        if not fsm.missions:
            cv.center(29, "NO MISSIONS")
    elif s.id == "mdetail":
        mission = fsm.mission()
        cv.title(mission.name if mission else "MISSION")
        if mission:
            start = max(0, min(s.step - 1, len(mission.steps) - 3))
            for slot, index in enumerate(range(start, min(len(mission.steps), start + 3))):
                y = 11 + slot * 12
                color = cv.block(2, y, 119, 11, index == s.step)
                cv.text(4, y + 2, f"{index + 1:2}", color=color)
                cv.draw.line((16, y + 1, 16, y + 9), fill=color)
                cv.text(20, y + 2, mission.steps[index][0], color=color, max_width=97)
            cv.scroll(s.step, len(mission.steps), 3, 11, 36)
            cv.block(2, 49, 124, 14)
            cv.center(52, mission.steps[s.step][1], pixels=118)
    elif s.id == "confirm":
        kind = s.confirm_kind
        prompts = {
            "arm": ("ARM?", "PROPS CLEAR?", "AREA CLEAR?", "NO", "ARM"),
            "disarm": ("DISARM?", "ON GROUND?", "STOP MOTORS?", "NO", "DISARM"),
            "start": (
                "START?",
                mission.name if mission else "MISSION",
                "10S DELAY",
                "CANCEL",
                "START",
            ),
            "spin": (f"TEST {chr(65 + s.motor)}?", "REMOVE PROPS!", "MOTOR SPINS 3S", "NO", "SPIN"),
            "land": ("LAND?", "REQUEST FC LAND", "CHECK AREA BELOW", "CANCEL", "LAND"),
            "cal": ("CALIBRATE?", s.message, "PROPS REMOVED?", "NO", "CAL"),
            "mag_accept": ("SAVE COMPASS?", "ACCEPT CAL RESULTS", "NO AUTO REBOOT", "NO", "SAVE"),
            "write": (
                "WRITE PARAM?",
                s.draft[0].replace("_", " ") if s.draft else "-",
                fmt(s.draft[1]) if s.draft else "-",
                "NO",
                "WRITE",
            ),
        }
        title, line1, line2, no, yes = prompts.get(kind, ("CONFIRM?", "-", "-", "NO", "YES"))
        cv.center(4, title, 2 if width(title, 2) <= 120 else 1)
        cv.center(25, "PLEASE WAIT" if s.pending else line1)
        cv.center(35, s.phase if s.pending else line2)
        for i, (x, label) in enumerate(((4, no), (66, yes))):
            color = cv.block(x, 46, 58, 15, s.focus == i)
            cv.text(x + (58 - width(label)) // 2, 50, label, color=color)
    elif s.id == "count":
        cv.center(1, "STARTING IN")
        remaining = max(1, math.ceil(s.deadline - now))
        cv.center(11, str(remaining), 5)
        cv.draw.rectangle((2, 49, 125, 53), outline=1)
        w = round(122 * max(0, min(1, (s.deadline - now) / c["missions"]["countdown_s"])))
        if w:
            cv.draw.rectangle((3, 50, 2 + w, 52), fill=1)
        cv.center(57, "BACK = ABORT")
    elif s.id == "run":
        cv.title("RUNNING", "BACK:ABORT", False)
        mission = fsm.mission()
        cv.center(13, mission.name if mission else "MISSION")
        cv.block(2, 22, 124, 32)
        seq = get("mission_seq")
        steps = mission.steps if mission else ()
        index = max(0, min(len(steps) - 1, (seq or 1) - 1))
        cv.center(25, f"ITEM {seq or '--'}/{len(steps)}")
        if s.phase == "RUNNING" and steps:
            cv.center(35, steps[index][0] if seq is not None else "--")
            cv.center(44, steps[index][1] if seq is not None else "--")
        else:
            cv.center(35, s.phase or "PREPARING")
        cv.bar(2, 56, 124, 7, (index / len(steps)) if steps and seq is not None else 0)
    elif s.id == "armed":
        if int(now * 2) % 2 == 0:
            cv.draw.rectangle((2, 2, 125, 61), outline=1)
        cv.center(12, "ARMED", 3)
        cv.center(36, "MOTORS LIVE")
        color = cv.block(14, 46, 100, 13, True)
        cv.center(49, "DISARM", color=color)
    elif s.id == "motors":
        cv.title("TEST MOTORS")
        positions = fsm.profile.get("motor_positions", [])
        for i in range(4):
            x, y = (2, 65)[i % 2], (13, 37)[i // 2]
            color = cv.block(x, y, 61, 22, s.motor == i)
            cv.text(x + 4, y + 4, chr(65 + i), 2, color)
            cv.draw.line((x + 31, y + 1, x + 31, y + 20), fill=color)
            cv.text(x + 38, y + 8, positions[i] if positions else "--", color=color)
    elif s.id == "mtest":
        cv.title("MOTOR TEST")
        cv.center(14, chr(65 + s.motor), 3)
        cv.center(39, s.phase or "SPINNING")
        deadline = snap.cached("motor_deadline", s.deadline)
        cv.bar(2, 50, 124, 9, 1 - max(0, deadline - now) / c["motors"]["duration_s"])
    elif s.id == "alert":
        cv.title(compact(s.alert_title, 110), height=12)
        clean = compact(s.message.upper(), 6000)
        pages = max(1, math.ceil(len(clean) / 60))
        chunk = clean[(int(now / 4) % pages) * 60 :][:60]
        for row in range(3):
            cv.center(16 + 10 * row, chunk[row * 20 : (row + 1) * 20])
        color = cv.block(34, 46, 60, 15, True)
        cv.center(50, "OK", color=color)
    elif s.id == "emergency":
        event = s.emergency
        cv.draw.rectangle((0, 0, 127, 11), fill=int(now * 4) % 2)
        cv.center(
            2,
            "! " + (event.kind if event else "EMERGENCY") + " !",
            color=0 if int(now * 4) % 2 else 1,
        )
        cv.center(14, get("mode", "ACTION UNKNOWN"))
        dist, _ = home_geometry(snap)
        cv.text(2, 24, "ALT " + fmt(get("alt"), 0) + " DIST " + fmt(dist, 0), max_width=124)
        eta = (
            dist / get("speed")
            if dist is not None and get("speed", 0) > 1 and get("mode") == "RTL"
            else None
        )
        line = (
            "EST " + fmt(eta, 0) + "S"
            if eta is not None and int(now / 2) % 2
            else "SPD " + fmt(get("speed"), 1)
        )
        cv.text(2, 34, line + " HDG " + fmt(get("heading"), 0), max_width=124)
        cv.text(
            2,
            44,
            "GPS "
            + FIXES.get(get("fix"), "--")
            + " S"
            + fmt(get("sats"), 0)
            + " H:"
            + ("Y" if snap.cached("home") else "N"),
            max_width=124,
        )
        cv.center(56, "BACK:ACK OK:LAND")
    elif s.id in ("cal", "failsafe"):
        cv.title("SENSOR CAL" if s.id == "cal" else "FAILSAFE")
        if s.id == "cal":
            row_list(cv, list(CAL), s.focus)
        else:
            start = max(0, min(s.focus - 1, len(PARAMS) - 3))
            for slot, name in enumerate(PARAMS[start : start + 3]):
                y = 12 + slot * 17
                selected = start + slot == s.focus
                color = cv.block(2, y, 119, 15, selected)
                value = get("param_" + name)
                number = s.draft[1] if selected and s.draft else value[0] if value else None
                cv.text(6, y + 4, compact(name.replace("_", " "), 71), color=color)
                label = compact(fmt(number), 35)
                cv.text(117 - width(label), y + 4, label, color=color)
            cv.scroll(s.focus, len(PARAMS), 3)
    else:
        cv.center(28, "UNKNOWN SCREEN")
    return cv.image
