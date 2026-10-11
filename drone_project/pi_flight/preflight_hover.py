#!/usr/bin/env python3
"""Offline by default. Connected modes are checklist-only unless --flight is explicit."""
import argparse
from contextlib import nullcontext
from copy import deepcopy
import csv
import os
import select
from pathlib import Path
import sys
import termios
import time
from flightcheck.checks import Check, all_pass, evaluate
from flightcheck.config import load, bench_profile
from flightcheck.mission import Machine, Mission
from flightcheck.reporting import Report, utc
from flightcheck.telemetry import LinkLost, Rejected, Transport, parameter_audit, process_lock
from flightcheck.startup import START_MODES, confirmation_mode, check_confirmation_state

HUMAN_ITEMS = (
    "Propellers correctly positioned and undamaged",
    "Fasteners and frame secure", "Battery secured and charged",
    "GPS/compass clear of wiring and RF sources",
    "Optical-flow lens and rangefinder clean",
    "Floor textured and well lit", "At least 15 m controlled clear radius",
    "Test volume and overhead clear", "Nobody near, above, or below aircraft",
    "Transmitter on", "STABILIZE / ALT_HOLD / LOITER recovery switch tested props-off",
    "Pilot ready", "Observer ready", "Weather/wind acceptable for commissioned aircraft",
    "Local permission to fly", "Aircraft points in agreed direction; compass agrees",
    "No USB cable, bench lead, or loose tether attached",
)


def input_live(prompt, transport):
    """Keep receiving while humans read; never manufacture freshness from a backlog."""
    if getattr(transport, "ui", None):
        return transport.ui.input_live(prompt, transport)
    expected_mode = confirmation_mode(transport)
    fd = sys.stdin.fileno()
    termios.tcflush(fd, termios.TCIFLUSH)
    print(prompt, end="", flush=True)
    while True:
        transport.pump()
        transport.heartbeat()
        check_confirmation_state(transport, expected_mode)
        if select.select([fd], [], [], 0)[0]:
            data = os.read(fd, 4096)
            if not data:
                raise Rejected("Terminal input closed")
            return data.decode(errors="replace").removesuffix("\n").removesuffix("\r")


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=str(Path(__file__).with_name("config.toml")))
    group = p.add_mutually_exclusive_group()
    group.add_argument("--offline", action="store_true")
    group.add_argument("--check-once", action="store_true")
    group.add_argument("--wait-for-fcu", action="store_true")
    group.add_argument("--monitor", action="store_true", help="receive-only live UDP telemetry; no FC commands")
    p.add_argument("--timeout", type=float)
    p.add_argument("--flight", action="store_true")
    p.add_argument('--bench',action='store_true',help='tolerant read-only bench diagnostics; motor commands and missions disabled')
    manual = p.add_mutually_exclusive_group()
    manual.add_argument('--arm-only', action='store_true')
    manual.add_argument('--disarm-only', action='store_true')
    p.add_argument('--maintenance',choices=('diagnostics','download-logs','erase-logs'))
    return p


def identity_checks(t, c):
    result = []
    version = t.version or {}
    expected = c["aircraft"]
    for key in ("flight_sw_version", "board_version"):
        actual = version.get(key)
        state = 'UNKNOWN' if not expected[key] or not actual else 'PASS' if actual == expected[key] else 'FAIL'
        result.append(Check(key,state,actual,f"verified hardware value {expected[key]}"))
    # MAVLink specifies uid2 supersedes uid. Some real FCs publish uid=0
    # while providing a valid 18-byte uid2.
    raw_uid2 = version.get('uid2')
    try:
        uid2 = bytes.fromhex(raw_uid2) if isinstance(raw_uid2,str) else bytes(raw_uid2 or [])
        actual_uid2 = uid2.hex() if len(uid2) == 18 and any(uid2) else ''
    except (TypeError,ValueError):
        actual_uid2 = ''
    expected_uid2 = expected.get('uid2','').lower()
    if actual_uid2 or expected_uid2:
        state = 'UNKNOWN' if not actual_uid2 or not expected_uid2 else 'PASS' if actual_uid2 == expected_uid2 else 'FAIL'
        result.append(Check('uid',state,{'uid2':actual_uid2,'uid':version.get('uid')},
                            'verified hardware uid2 '+expected_uid2,'MAVLink 2 uid2 supersedes legacy uid'))
    else:
        actual = version.get('uid')
        state = 'UNKNOWN' if not expected['uid'] or not actual else 'PASS' if actual == expected['uid'] else 'FAIL'
        result.append(Check('uid',state,actual,f"verified hardware value {expected['uid']}"))
    identity_states = [check.state for check in result]
    target_state = ('FAIL' if 'FAIL' in identity_states else
                    'PASS' if expected['firmware_target'] and all(state == 'PASS' for state in identity_states)
                    else 'UNKNOWN')
    result.append(Check('firmware target / real hardware',target_state,
                        expected['firmware_target'],
                        'saved target evidence associated with matching live firmware, board and hardware UID',
                        'Recorded target: '+(expected['firmware_target'] or 'unset')+
                        '; live identity '+('matches' if target_state == 'PASS' else 'mismatched or incomplete')))
    result.append(Check('firmware behavior review','PASS' if
                        c['commissioning']['firmware_behavior_reviewed'] else 'UNKNOWN',
                        c['commissioning']['firmware_behavior_reviewed'],
                        'reviewed GUIDED takeoff, climb parameter units and altitude reference for this firmware',
                        'Knowing the board target does not verify takeoff and recovery behavior'))
    actual_mode = t.mode()
    captured_mode = getattr(t,'preflight_mode',None)
    mode_ok = actual_mode in START_MODES and (captured_mode is None or actual_mode == captured_mode)
    result.append(Check("initial mode", "PASS" if mode_ok else "FAIL",
        actual_mode, 'supported pilot mode: '+', '.join(START_MODES)+'; unchanged during preflight',
        '' if mode_ok else f'Current mode {actual_mode}; captured startup mode {captured_mode}. Unsupported mode or pilot mode change.'))
    for kind in ("HOME_POSITION", "GPS_GLOBAL_ORIGIN"):
        records = t.store.samples[kind]
        d = records[-1][1] if records else {}
        result.append(Check(kind, "PASS" if d and all(k in d for k in
            ("latitude", "longitude", "altitude")) and
            -900000000 <= d["latitude"] <= 900000000 and
            -1800000000 <= d["longitude"] <= 1800000000 else "UNKNOWN",
            d, "origin/home received during this connection and plausible"))
    return result


def main(argv=None, ui=None, *, existing_transport=None):
    args = parser().parse_args(argv)
    if args.maintenance and (ui is None or args.flight or args.monitor or args.offline or args.arm_only or args.disarm_only):
        parser().error('Maintenance requires terminal UI and cannot be combined with flight/manual/monitor/offline')
    if (args.arm_only or args.disarm_only) and (ui is None or args.flight or args.monitor or args.offline):
        parser().error('Manual controls require the terminal UI and cannot be combined with mission/monitor/offline modes')
    if os.geteuid() == 0:
        print("FAIL: do not run mission as root", file=sys.stderr)
        return 2
    if args.wait_for_fcu and args.timeout is None:
        parser().error("--wait-for-fcu requires --timeout SECONDS")
    if args.timeout is not None and not 0 < args.timeout <= 300:
        parser().error("timeout must be in (0, 300] seconds")
    if args.monitor and args.flight:
        parser().error("--monitor cannot be combined with --flight; use --check-once --flight")
    c = deepcopy(existing_transport.c) if existing_transport is not None else load(args.config)
    if args.bench:
        c = bench_profile(c)
        if args.flight or args.arm_only or args.disarm_only or args.maintenance == 'erase-logs':
            message = 'BENCH ONLY: liberal diagnostic limits cannot authorize arming, disarming, missions or log erase. Restart without --bench for normal controls.'
            print(message)
            if ui is not None:
                ui.view_state = 'PREFLIGHT BLOCKED'
                ui.workflow_status = message
                ui.notice = message
            return 3
    if args.monitor:
        from flightcheck.monitor import run
        return run(c, ui=ui, timeout=args.timeout)
    if args.maintenance:
        from flightcheck.maintenance import run
        return run(c,ui,args.maintenance)
    manual_action = 'ARM' if args.arm_only else 'DISARM' if args.disarm_only else None
    actuating = args.flight or args.arm_only
    if args.arm_only or args.disarm_only:
        from flightcheck.manual import arm_control
        return arm_control(c, ui)
    if args.flight and ui is not None:
        ui.receive_only = False
        ui.view_state = 'MISSION PREFLIGHT / LIVE LINK' if existing_transport is not None else 'MISSION PREFLIGHT / CONNECTING'
        ui.workflow_status = 'Selected '+ui.route.upper()+'. Checking live readiness; start locked.'
        ui.notice = ui.workflow_status
        ui.draw(force=True) if hasattr(ui, 'draw') else None
    route_check = None
    if ui is not None:
        from flightcheck.routes import validate_route
        if args.flight:
            try:
                validate_route(c, ui.route)
            except Rejected as exc:
                route_check = Check('mission review','FAIL',str(exc),'reviewed mission limits and route')
        # Override only the runtime copy for the reviewed GCS mission.
        if manual_action is None:
            c["mission"]["hold_s"] = 5.0
    report = Report(Path(c["_base"])/"logs")
    report.save("configuration.json", c)
    report.event("start", arguments=vars(args))
    if ui is not None:
        ui.config, ui.report, ui.blockers = c, report, []
    checks = []
    if args.flight:
        checks.append(Check('flight limit review','PASS' if c['commissioning']['thresholds_approved'] else 'UNKNOWN',
                            c['commissioning']['thresholds_approved'],'limits validated for this aircraft',
                            'Provisional diagnostic limits must be reviewed before autonomous launch'))
    if route_check:
        checks.append(route_check)
    connected = args.check_once or args.wait_for_fcu
    if not connected:
        checks += [Check(name, "UNKNOWN", None, "fresh live evidence", "OFFLINE: no socket or serial opened")
                   for name in ("link", "identity", "GPS stability", "EKF", "optical flow", "rangefinder",
                                "battery", "RC", "vehicle/mode", "live parameters", "human checks")]
        report.checklist(checks, "offline")
        if ui is not None:
            ui.checks = checks
        print(f"OFFLINE verification only. FLIGHT LOCKED. Report: {report.path}")
        return 3 if actuating else 0
    # Mission route review remains separate from the removed aggregate gate.
    if actuating and route_check:
        report.checklist(checks, "blocked")
        print("FLIGHT LOCKED; mission route review required")
        if ui is not None:
            ui.checks = checks
            ui.show_checks = True
            ui.workflow_status = 'PREFLIGHT BLOCKED: mission route review required. C shows details.'
            ui.notice = ui.workflow_status
            ui.view_state = 'PREFLIGHT BLOCKED'
        return 3
    if not c["link"]["device_confirmed"] or not c["commissioning"]["wiring_props_off_verified"]:
        print("FAIL: hardware connection requires confirmed UART and props-off wiring evidence")
        return 3
    t = None
    saved_transport = None
    saved_preflight_mode = getattr(existing_transport,'preflight_mode',None) if existing_transport is not None else None
    with (nullcontext() if existing_transport is not None else process_lock("mission.lock")):
        try:
            t = existing_transport if existing_transport is not None else Transport(c, report)
            if existing_transport is not None:
                saved_transport = (t.c, t.report, t.receive_only, t.observer, getattr(t, 'ui', None))
                t.c, t.report, t.receive_only = c, report, False
            if ui is not None:
                t.ui = ui
                t.observer = ui.observe
                ui.transport = t
            try:
                if existing_transport is None:
                    t.connect(args.timeout or c["link"]["connect_timeout_s"])
                else:
                    t.pump()
                    t.require_live()
            except LinkLost:
                print("NOT CONNECTED")
                report.event("not_connected")
                return 4
            if t.armed():
                raise Rejected("Already armed: no startup commands permitted")
            t.preflight_mode = confirmation_mode(t)
            if ui is not None:
                ui.view_state = 'PREFLIGHT / REQUESTING TELEMETRY'
                ui.workflow_status = 'FC connected. Requesting version and sensor streams; please wait.'
                ui.notice = ui.workflow_status
            if existing_transport is None:
                t.initialize_streams()
            elif not t.version:
                # The monitor already requested sensor streams. Only fetch the
                # identity packet that is still missing, not every interval ACK.
                t.command(512, [148])
            report.save("autopilot-version.json", t.version)
            if ui is not None:
                ui.view_state = 'PREFLIGHT / REQUIRED PARAMETERS'
                ui.workflow_status = 'Reading only reviewed safety parameters; no full parameter download.'
                ui.notice = ui.workflow_status
            # Mission readiness needs fresh readbacks of the reviewed safety
            # profile, not a bulk snapshot of every unrelated FC parameter.
            # Full exports remain available explicitly through F diagnostics.
            machine = Machine(report)
            machine.transition("FULL_PREFLIGHT")
            if ui is not None:
                ui.receive_only = False
                ui.view_state = 'FULL PREFLIGHT'
                ui.notice = 'Checking sensors, identity, firmware and parameters • flight locked'
                ui.workflow_status = 'Checking live sensors, identity, firmware and parameters. Please wait.'
            audit = parameter_audit(t, c)
            identity = identity_checks(t, c)
            if ui is not None:
                # Publish completed gates before the stationary sample finishes.
                ui.checks = checks + audit + identity
                ui.preflight_pending = 'FC prearm response and stationary sensor sample'
            if c["firmware"]["prearm_command_supported"]:
                ack = t.command(401)
                report.event("prearm_requested", ack=ack)
                # Accepted means checks requested, not checks passed. SYS_STATUS gate follows.
            else:
                audit.append(Check("prearm command support", "UNKNOWN", None,
                                   "confirm support on detected firmware"))
            print("Keep aircraft motionless for the stationary sample.", flush=True)
            start, end = t.sample(c["sampling"]["full_s"])
            checks += audit + identity + evaluate(t.store, c, start, end)
            gps = [d for ts,d in t.store.samples["GPS_RAW_INT"] if start <= ts <= end]
            report.save("gps-samples.json", gps)
            with (report.path/"gps-samples.csv").open("w", newline="") as stream:
                fields = ["lat", "lon", "alt", "eph", "vel", "satellites_visible", "fix_type"]
                writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(gps)
            report.checklist(checks, "full-preflight")
            if ui is not None:
                ui.checks = checks
            if not actuating:
                print(f"CHECKLIST ONLY; no arming. Report: {report.path}")
                return 0 if all_pass(checks) else 3
            if not all_pass(checks):
                machine.transition("ABORT_DISARMED")
                if ui is not None:
                    ui.view_state = 'PREFLIGHT BLOCKED'
                    ui.workflow_status = 'Live preflight failed. C shows checks. Enter cannot start.'
                    ui.show_checks = True
                return 3
            if not sys.stdin.isatty():
                raise Rejected("Flight requires a foreground interactive terminal")
            for item in HUMAN_ITEMS:
                if ui is not None:
                    ui.view_state = 'OPERATOR CONFIRMATIONS'
                accepted = input_live(f"Confirm: {item} [type YES]: ", t) == "YES"
                report.event("human_confirmation", item=item, accepted=accepted)
                if not accepted:
                    machine.transition("ABORT_DISARMED")
                    return 3
            machine.transition("WAIT_FOR_ENTER")
            if ui is not None:
                ui.view_state = 'READY / PRESS ENTER'
                ui.workflow_status = 'All required confirmations passed. Fresh Enter starts final recheck.'
                ui.notice = ('READY: Press Enter for fresh recheck and ARM ONLY / NO TAKEOFF'
                             if manual_action == 'ARM' else 'READY: Press Enter to run fresh recheck and start '+ui.route)
            print('ARM ONLY: no mission loaded, no takeoff.' if args.arm_only else
                  f"Aircraft: {c['aircraft']}; altitude 2.0 m; climb {c['mission']['climb_m_s']} m/s; hold {c['mission']['hold_s']} s")
            print("Sources: GPS XY position, MTF-01 XY velocity, barometer height, compass yaw.")
            print(f"Recovery modes: {c['commissioning']['recovery_modes']}; MOTORS WILL ARM.")
            print("Final Enter also reconfirms the area remains clear and pilot/observer remain ready.")
            if ui is None:
                termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
            print('ALL REQUIRED CHECKS PASSED. Press Enter for fast recheck and '+
                  ('ARM ONLY; NO TAKEOFF.' if manual_action == 'ARM' else 'begin the 2.0 m flight.'))
            final_prompt = ('ALL CHECKS CONFIRMED. Press Enter to ARM ONLY (no takeoff); any text cancels: '
                            if manual_action == 'ARM' else
                            'ALL CHECKS CONFIRMED. Press Enter to start the mission; any text then Enter cancels: ')
            if input_live(final_prompt, t) != "":
                machine.transition("ABORT_DISARMED")
                return 3
            machine.transition("FAST_RECHECK")
            if ui is not None:
                ui.view_state = 'FINAL RECHECK'
                ui.notice = 'Rechecking fresh telemetry and parameters before arming'
            # Final Enter explicitly renews all area/pilot/observer confirmations.
            report.event("area_ready_renewed", confirmed_at=utc())
            fast_audit = parameter_audit(t, c)
            start, end = t.sample(c["sampling"]["fast_s"])
            fast = identity_checks(t, c) + evaluate(t.store, c, start, end)
            # Readbacks are fresh after Enter; no cached parameter assumptions.
            fast += fast_audit
            report.checklist(fast, "fast-recheck")
            if ui is not None:
                ui.checks = fast
            if not all_pass(fast):
                machine.transition("ABORT_DISARMED")
                if ui is not None:
                    ui.view_state = 'PREFLIGHT BLOCKED'
                    ui.workflow_status = 'Final recheck failed. Enter cannot start. C shows checks.'
                    ui.show_checks = True
                return 3
            t.flight_authorized = True
            if manual_action == 'ARM':
                def arm_guard():
                    t.require_live()
                    if t.mode() != t.preflight_mode:
                        raise Rejected('Mode changed before manual arm')
                    landed = t.store.latest('EXTENDED_SYS_STATE', time.monotonic(), c['link']['max_age_s'])
                    if landed.get('landed_state') != 1:
                        raise Rejected('Manual arm requires fresh on-ground evidence')
                t.command(400, [1, 0], flight=True, guard=arm_guard)
                deadline = time.monotonic()+c['mission']['command_timeout_s']
                while time.monotonic() < deadline:
                    t.pump()
                    arm_guard()
                    if t.armed():
                        ui.view_state = 'ARM CONFIRMED / NO TAKEOFF'
                        return 0
                raise Rejected('Arm ACK received but armed heartbeat not observed')
            if ui is None:
                mission = Mission(t, c, report, machine)
            else:
                from flightcheck.routes import RouteMission
                mission = RouteMission(t, c, report, machine, route=ui.route, ui=ui)
                mission.yaw = t.store.latest("ATTITUDE", time.monotonic(), c["link"]["max_age_s"])["yaw"]
                ui.mission = mission
                ui.selected = 1
                ui.workflow_status = ''
            success = mission.run()
            print("PASS" if success else "FAIL", f"Report: {report.path}")
            return 0 if success else 3
        except (Rejected, LinkLost, ValueError, OSError, KeyboardInterrupt, Exception) as exc:
            report.event("abort", error=str(exc))
            print(f"FAIL: {exc}; report: {report.path}")
            if ui is not None and getattr(ui,'mission',None) is None:
                ui.view_state = 'PREFLIGHT BLOCKED'
                ui.workflow_status = 'Preflight stopped: '+str(exc)
            return 3
        finally:
            if ui is not None:
                ui.preflight_pending = ''
            if t is not None:
                if saved_transport is not None:
                    t.flight_authorized = False
                    t.preflight_mode = saved_preflight_mode
                    t.c, t.report, t.receive_only, t.observer, t.ui = saved_transport
                elif existing_transport is None:
                    t.close()


if __name__ == "__main__":
    raise SystemExit(main())
