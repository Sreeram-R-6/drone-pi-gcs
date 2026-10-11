#!/usr/bin/env python3
"""Live receive-only dashboard by default; explicit flags for checks or flight."""
import contextlib
import curses
import sys
import time
import preflight_hover
from flightcheck.terminal import Terminal
from flightcheck.monitor import MISSION_SELECTED, MANUAL_SELECTED, SERVICE_SELECTED, RECONNECT_SELECTED


def selected_mission_args(argv):
    """Menu selection requests gated preflight; it never directly requests arm."""
    return [arg for arg in argv if arg != '--monitor'] + ['--check-once','--flight']


def manual_args(argv, action):
    return [arg for arg in argv if arg != '--monitor'] + ['--check-once',
            '--arm-only' if action == 'ARM' else '--disarm-only']


def service_args(argv, action):
    return [arg for arg in argv if arg != '--monitor'] + ['--check-once','--maintenance',action]


def dashboard(ui, argv):
    """Monitor → live mission menu → strict preflight → monitor again."""
    initial_monitor = '--monitor' in argv
    if not initial_monitor and not ui.choose():
        return 0
    current = list(argv)
    while True:
        with contextlib.redirect_stdout(ui),contextlib.redirect_stderr(ui):
            try:
                result = preflight_hover.main(current,ui=ui)
            except (OSError,ValueError,RuntimeError) as exc:
                ui.write(f'FAIL: {exc}')
                ui.notice = str(exc)
                ui.view_state = 'CONNECTION ERROR'
                ui.transport = None
                if initial_monitor:
                    ui.receive_only = True
                result = 3
        if '--monitor' in current and result == RECONNECT_SELECTED:
            ui.reconnect_requested = False
            ui.transport = None
            ui.manual_action = None
            ui.service_request = None
            ui.menu_requested = False
            curses.flushinp()
            current = list(argv)
            continue
        if '--monitor' in current and result in (MISSION_SELECTED, MANUAL_SELECTED, SERVICE_SELECTED):
            if result in (MANUAL_SELECTED,SERVICE_SELECTED):
                ui.mission_selected = False
            ui.receive_only = False
            ui.transport = None # Monitor has closed/released its sole UART ownership.
            ui.request = None
            curses.flushinp()
            current = (service_args(argv, ui.service_request) if result == SERVICE_SELECTED else manual_args(argv, ui.manual_action) if result == MANUAL_SELECTED
                       else selected_mission_args(argv))
            continue
        if initial_monitor and any(flag in current for flag in ('--flight','--arm-only','--disarm-only','--maintenance')):
            outcome = ui.mission.machine.state if ui.mission else ui.view_state
            if ui.view_state not in ('PREFLIGHT BLOCKED','MAINTENANCE BLOCKED') and '--maintenance' not in current:
                label = 'ARM CONTROL' if '--arm-only' in current or '--disarm-only' in current else ui.route
                ui.workflow_status = f'{label}: {outcome} (code {result}). M selects a mission.'
            ui.mission = None
            if '--flight' not in current or ui.view_state != 'PREFLIGHT BLOCKED':
                ui.mission_selected = False
            ui.transport = None
            ui.request = None
            if ui.quit_requested:
                return result
            ui.manual_action = None
            current = list(argv)
            curses.flushinp()
            continue
        ui.mission = None
        if ui.quit_requested:
            return result
        if ui.view_state != 'CONNECTION ERROR':
            ui.notice = f'Run ended (code {result}). Press Q to exit. No automatic restart.'
        while True:
            ui.keys()
            ui.draw(force=True)
            if ui.quit_requested:
                return result
            if initial_monitor and getattr(ui,'reconnect_requested',False):
                ui.reconnect_requested = False
                ui.transport = None
                ui.manual_action = None
                ui.service_request = None
                ui.menu_requested = False
                curses.flushinp()
                current = list(argv)
                break
            if initial_monitor and (ui.menu_requested or getattr(ui,'manual_action',None) or getattr(ui,'service_request',None)):
                ui.menu_requested = False
                if getattr(ui,'service_request',None) or getattr(ui,'manual_action',None) or ui.choose():
                    if ui.manual_action:
                        ui.mission_selected = False
                    ui.receive_only = False
                    ui.transport = None
                    ui.request = None
                    curses.flushinp()
                    current = (service_args(argv,ui.service_request) if getattr(ui,'service_request',None) else manual_args(argv, ui.manual_action) if ui.manual_action
                               else selected_mission_args(argv))
                    break
            time.sleep(.1)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--help' in argv or '-h' in argv:
        p = preflight_hover.parser()
        p.description = 'Live UART dashboard by default. Press M or Enter for the mission menu; full preflight and final Enter are required to start. Use --offline for preview.'
        p.parse_args(['--help'])
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('GCS requires an interactive terminal. Use preflight_hover.py --offline for headless checks.')
        return 2
    if not any(flag in argv for flag in ('--offline','--check-once','--wait-for-fcu','--monitor')):
        argv.append('--check-once' if '--flight' in argv else '--monitor')
    def session(screen):
        ui = Terminal(screen)
        return dashboard(ui,argv)
    return curses.wrapper(session)


if __name__ == '__main__':
    raise SystemExit(main())
