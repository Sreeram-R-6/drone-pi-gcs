"""Service, safe offline renderer, replay and hardware diagnostics entry points."""

from __future__ import annotations

import argparse
from itertools import count
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Empty, Full, Queue, PriorityQueue
import select
import signal
import sys
from threading import Event
import time

from .config import load, load_profile, ConfigError
from .emergency import EmergencyDetector
from .fsm import FSM
from .hardware import Display
from .input import GPIOButtons, InputInterpreter, KeyEvent
from .mav import MavLinkWorker
from .missions import MissionStore
from .model import TelemetryStore
from .monitor import PiMonitor
from .render import render
from .webbridge import Bridge

LOG = logging.getLogger(__name__)
SCREENS = (
    "boot",
    "home",
    "status",
    "menu",
    "missions",
    "mdetail",
    "confirm",
    "count",
    "run",
    "armed",
    "motors",
    "mtest",
    "alert",
    "emergency",
    "cal",
    "failsafe",
)


def logging_setup(c: dict):
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    directory = c["logging"]["directory"]
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            Path(directory) / "oled.log",
            maxBytes=c["logging"]["max_bytes"],
            backupCount=c["logging"]["backup_count"],
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)


def ascii_frame(frame) -> str:
    return "\n".join(
        "".join("#" if frame.getpixel((x, y)) else " " for x in range(128)) for y in range(64)
    )


def replay(
    path: Path, store: TelemetryStore, target: tuple[int, int] = (1, 1), battery_id: int = 0
) -> float:
    """Decode offline only. No MavLinkWorker/connection sender is ever created."""
    if not path.is_file() or path.suffix != ".tlog":
        raise ValueError("Replay requires a regular .tlog file, never a device or network endpoint")
    os.environ["MAVLINK20"] = "1"
    from pymavlink import mavutil

    connection = mavutil.mavlink_connection(str(path), dialect="ardupilotmega")
    latest = 0.0
    first = None
    try:
        while True:
            message = connection.recv_match()
            if message is None:
                break
            if message.get_type() == "BAD_DATA":
                continue
            stamp = getattr(message, "_timestamp", 0.0)
            first = stamp if first is None else first
            latest = max(latest, stamp - first)
            if (message.get_srcSystem(), message.get_srcComponent()) == target:
                store.update(message.to_dict(), latest, battery_id)
    finally:
        connection.close()
    return latest


def offline(args, c: dict, profile: dict, missions: list):
    store = TelemetryStore(c["mavlink"]["stale_s"])
    now = (
        replay(
            args.replay,
            store,
            (c["mavlink"]["target_system"], c["mavlink"]["target_component"]),
            c["battery"]["id"],
        )
        if args.replay
        else 100.0
    )
    fsm = FSM(c, profile, missions)
    fsm.state.started_at = now
    if args.render_all:
        args.output.mkdir(parents=True, exist_ok=True)
        for name in SCREENS:
            for page in range(5) if name == "status" else (0,):
                fsm.state.id, fsm.state.page = name, page
                fsm.state.confirm_kind = "arm"
                fsm.state.deadline = now + 10
                fsm.state.message = "NO FC LINK; CONTROLS DISABLED"
                render(fsm, store.snapshot(now), now).save(
                    args.output / f"{name}{page + 1 if name == 'status' else ''}.png"
                )
        LOG.info("Offline PNG previews: %s", args.output)
        return
    fsm.state.id = args.screen
    fsm.state.page = args.page - 1
    fsm.state.deadline = now + 10
    fsm.state.confirm_kind = "arm"
    fsm.state.message = "OFFLINE NO FC LINK"
    frame = render(fsm, store.snapshot(now), now)
    if args.ascii:
        print(ascii_frame(frame))
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        frame.save(args.output)


def service(args, c: dict, profile: dict, missions: list):
    quit_event = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: quit_event.set())
    edges: Queue = Queue(maxsize=256)
    events: PriorityQueue = PriorityQueue(maxsize=128)
    sequence = count()
    overflow = Event()
    store = TelemetryStore(c["mavlink"]["stale_s"])
    fsm = FSM(c, profile, missions)
    fsm.state.started_at = time.monotonic()
    fsm.state.changed_at = fsm.state.started_at
    detector = EmergencyDetector(c, profile)
    interpreter = InputInterpreter(c["buttons"])
    display = Display(c["display"])
    bridge = Bridge(c["web"]["socket"]) if c["web"]["enabled"] else None
    if not args.no_display:
        first_frame_at = time.monotonic()
        display.show(render(fsm, store.snapshot(first_frame_at), first_frame_at), first_frame_at)

    def emit(kind, data):
        if kind == "operation":
            LOG.info("OPERATION kind=%s status=%s detail=%s", *data)
        try:
            priority = (
                0
                if kind in ("reboot", "worker_error") or (kind == "text" and data["severity"] <= 2)
                else 1
            )
            events.put_nowait((priority, next(sequence), kind, data))
        except Full:
            overflow.set()

    def edge(key, pressed, stamp):
        try:
            edges.put_nowait((key, pressed, stamp))
        except Full:
            overflow.set()

    worker = MavLinkWorker(c, profile, store, emit)
    monitor = PiMonitor(c["pi"], store)
    buttons = None
    if not args.keyboard:
        try:
            buttons = GPIOButtons(c["buttons"], edge)
        except Exception:
            LOG.exception("GPIO unavailable; input actions disabled")
            # A broken keypad must not leave remote/software action routes enabled.
            c["safety"]["commands_enabled"] = False
    worker.start()
    monitor.start()
    next_frame = 0.0
    last_input = time.monotonic()
    saved_frame = None
    blanked = False

    def effects(items):
        for effect in items:
            LOG.info("UI effect=%s", effect.kind)
            if effect.kind == "cancel":
                worker.cancel()
                if (
                    not worker.operation
                    and fsm.state.id == "emergency"
                    and store.snapshot(time.monotonic()).cached("motor_deadline", 0)
                    > time.monotonic()
                ):
                    worker.submit("motor_stop", fsm.state.motor + 1)
            elif effect.kind == "acknowledge":
                if effect.data:
                    detector.acknowledge(effect.data.episode)
            elif not worker.submit(effect.kind, effect.data):
                if effect.kind != "read_params":
                    fsm.alert("OPERATION ALREADY PENDING", time.monotonic())

    try:
        while not quit_event.is_set():
            now = time.monotonic()
            if overflow.is_set():
                overflow.clear()
                worker.cancel()
                c["safety"]["commands_enabled"] = False
                event = detector.raise_event("INPUT/EVENT OVERFLOW", "CONTROLS DISABLED")
                if event:
                    effects(fsm.emergency(event, now))
            # All emergency evidence is processed before buttons or countdown deadlines.
            for _ in range(128):
                try:
                    _, _, kind, data = events.get_nowait()
                except Empty:
                    break
                if kind == "operation":
                    op_kind, status, detail = data
                    fsm.operation(op_kind, status, detail, now)
                    if op_kind == "motor_start" and status == "success" and fsm.state.id == "mtest":
                        fsm.state.deadline = store.snapshot(now).cached("motor_deadline", now)
                        fsm.state.started_at = fsm.state.deadline - c["motors"]["duration_s"]
                elif kind == "text":
                    event = detector.text(data["severity"], data["text"])
                    if event:
                        effects(fsm.emergency(event, now))
                elif kind in ("reboot", "worker_error"):
                    event = detector.raise_event(
                        "FC SESSION CHANGED" if kind == "reboot" else "FC LINK ERROR", str(data)
                    )
                    if event:
                        effects(fsm.emergency(event, now))
            snap = store.snapshot(now)
            expected = worker.expected_modes.copy() if now < worker.expected_until else set()
            for event in detector.update(snap, expected):
                effects(fsm.emergency(event, now))
            fsm.faults = list(detector.active)
            for _ in range(256):
                try:
                    key, pressed, stamp = edges.get_nowait()
                except Empty:
                    break
                interpreter.edge(key, pressed, stamp)
            keys = interpreter.tick(
                now, fsm.state.generation, fsm.state.id in ("count", "run", "mtest")
            )
            if bridge:
                for _ in range(16):
                    try:
                        virtual, generation, stamp = bridge.keys.get_nowait()
                    except Empty:
                        break
                    if now - stamp > 1:
                        continue
                    LOG.info(
                        "WEB KEY key=%s screen=%s generation=%s", virtual, fsm.state.id, generation
                    )
                    key = virtual
                    if virtual in ("prev", "next") and fsm.state.id != "motors":
                        key = (
                            ("left" if virtual == "prev" else "right")
                            if fsm.state.id == "home"
                            else ("up" if virtual == "prev" else "down")
                        )
                    keys.append(KeyEvent(key, generation=generation))
            if args.keyboard and sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]:
                # Line-oriented SSH emulator input; these never pass the live command gate.
                for word in sys.stdin.readline().strip().lower().split():
                    if word in ("up", "down", "left", "right", "ok", "back"):
                        keys.append(KeyEvent(word, generation=fsm.state.generation))
                    elif word == "quit":
                        quit_event.set()
            for key in keys:
                last_input = now
                if blanked:
                    blanked = False
                    continue  # wake gesture is consumed, including OK
                effects(fsm.key(key, snap, now))
            effects(fsm.tick(snap, now))
            if now >= next_frame:
                frame = render(fsm, snap, now)
                d = c["display"]
                idle = now - last_input
                blanked = False
                if not d["always_on"] and fsm.state.id not in (
                    "emergency",
                    "armed",
                    "run",
                    "mtest",
                    "count",
                ):
                    if d["blank_after_s"] and idle >= d["blank_after_s"]:
                        from PIL import Image

                        frame = Image.new("1", (128, 64))
                        blanked = True
                    display.set_contrast(
                        max(1, d["contrast"] // 4)
                        if d["dim_after_s"] and idle >= d["dim_after_s"]
                        else d["contrast"]
                    )
                else:
                    display.set_contrast(d["contrast"])
                if not args.no_display:
                    display.show(frame, now)
                if bridge:
                    bridge.publish(
                        frame,
                        fsm.state.generation,
                        fsm.state.id,
                        display.device is not None and display.last_frame == frame.tobytes(),
                    )
                if args.frame_output and frame.tobytes() != saved_frame:
                    args.frame_output.parent.mkdir(parents=True, exist_ok=True)
                    frame.save(args.frame_output)
                    saved_frame = frame.tobytes()
                next_frame = now + 1 / d["max_fps"]
            quit_event.wait(0.02)
    finally:
        if bridge:
            bridge.close()
        worker.cancel()
        # Attempt bounded stop before stopping the transport. FC timeout remains fallback.
        if (
            store.snapshot(time.monotonic()).cached("motor_deadline", 0) > time.monotonic()
            and not worker.operation
        ):
            worker.submit("motor_stop", fsm.state.motor + 1)
            until = time.monotonic() + 0.3
            while worker.operation or time.monotonic() < until:
                if time.monotonic() > until + 0.3:
                    break
                Event().wait(0.02)
        worker.close()
        monitor.close()
        if buttons:
            buttons.close()
        display.close(clear=True)


def button_test(c: dict):
    edges: Queue = Queue()
    interpreter = InputInterpreter(c["buttons"])
    buttons = GPIOButtons(c["buttons"], lambda *args: edges.put(args))
    print("BCM GPIOs; pull_up=True; active LOW. Ctrl-C to exit.", flush=True)
    try:
        while True:
            now = time.monotonic()
            while not edges.empty():
                key, pressed, stamp = edges.get_nowait()
                interpreter.edge(key, pressed, stamp)
                print(f"{key.upper()}: {'LOW/PRESSED' if pressed else 'HIGH/RELEASED'}", flush=True)
            for event in interpreter.tick(now, 0):
                print(event, flush=True)
            Event().wait(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        buttons.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--render-all", action="store_true")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--screen", choices=SCREENS, default="home")
    parser.add_argument("--page", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--output", type=Path, default=Path("preview.png"))
    parser.add_argument("--ascii", action="store_true")
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--keyboard", action="store_true")
    parser.add_argument("--no-display", action="store_true")
    parser.add_argument("--frame-output", type=Path)
    parser.add_argument("--button-test", action="store_true")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args(argv)
    try:
        c = load(args.config)
        profile = load_profile(c)
        logging_setup(c)
        missions = MissionStore(
            Path(c["missions"]["directory"]), c["missions"]["max_display_steps"]
        )
        missions.load_all()
        for error in missions.errors:
            LOG.error("MISSION LOAD: %s", error)
        if args.validate:
            print(
                json.dumps(
                    {
                        "config": "VALID",
                        "profile_loaded": bool(profile),
                        "missions": len(missions.missions),
                        "mission_errors": missions.errors,
                    },
                    indent=2,
                )
            )
            return 1 if missions.errors else 0
        if args.render or args.render_all or args.ascii or args.replay:
            offline(args, c, profile, missions.missions)
        elif args.button_test:
            button_test(c)
        else:
            if args.keyboard:
                c["safety"]["commands_enabled"] = False
            service(args, c, profile, missions.missions)
        return 0
    except (ConfigError, OSError, ValueError) as exc:
        LOG.error("Startup failed: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
