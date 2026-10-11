"""Explicit normal ground disarm; never force-disarm or take off."""
import time
import curses
from pathlib import Path
from .reporting import Report
from .telemetry import Transport, Rejected, process_lock


def ground_guard(t, mode):
    t.require_live()
    if t.mode() != mode:
        raise Rejected('Mode changed during ground disarm')
    landed = t.store.latest('EXTENDED_SYS_STATE', time.monotonic(), t.c['link']['max_age_s'])
    if landed.get('landed_state') != 1:
        raise Rejected('Disarm requires fresh FC confirmation that aircraft is on ground')


def toggle_ground_arm(t):
    """Normal FC-checked command, chosen from current heartbeat, never force."""
    mode = t.mode()
    wanted = not t.armed()
    guard = lambda: ground_guard(t, mode)
    guard()
    t.flight_authorized = True
    try:
        t.command(400, [1 if wanted else 0, 0], flight=True, guard=guard)
        deadline = time.monotonic()+t.c['mission']['command_timeout_s']
        while time.monotonic() < deadline:
            t.pump()
            guard()
            if t.armed() == wanted:
                return wanted
        raise Rejected('Command ACK received but requested arm state not confirmed by heartbeat')
    finally:
        t.flight_authorized = False


def arm_control(config, ui):
    """First A enters; each fresh A requests an observed-state ground toggle."""
    report = Report(Path(config['_base'])/'logs')
    t = None
    ui.manual_action = None
    ui.arm_control_active = True
    ui.receive_only = True
    ui.view_state = 'ARM MODE / CONNECTING'
    ui.workflow_status = 'No mission. Connecting; A requests arm/disarm, Esc returns.'
    ui.draw(force=True)
    try:
        if not config['link']['device_confirmed'] or not config['commissioning']['wiring_props_off_verified']:
            raise Rejected('Confirmed UART and props-off wiring evidence required')
        with process_lock('mission.lock'):
            t = Transport(config, report)
            ui.transport = t
            t.observer = ui.observe
            t.connect(config['link']['connect_timeout_s'])
            t.request_monitor_telemetry()
            ui.workflow_status = ''
            curses.flushinp()
            ready_after = time.monotonic()+.5
            while True:
                t.pump()
                t.request_monitor_telemetry()
                if ui.quit_requested:
                    return 0
                if ui.arm_control_exit:
                    ui.arm_control_exit = False
                    return 0
                armed = t.armed()
                ui.view_state = 'ARM MODE / '+('ARMED' if armed else 'DISARMED')
                if not ui.workflow_status:
                    ui.workflow_status = 'A: '+('ground DISARM' if armed else 'ARM (motors may spin)')+' · Esc: main menu · no mission'
                if ui.manual_action:
                    action = ui.manual_action
                    ui.manual_action = None
                    if time.monotonic() < ready_after:
                        continue
                    try:
                        if action == 'DISARM' and not armed:
                            ui.workflow_status = 'Already disarmed. A requests arm; Esc returns.'
                        else:
                            ui.workflow_status = 'Waiting for FC command acknowledgement...'
                            ui.draw(force=True)
                            actual = toggle_ground_arm(t)
                            ui.workflow_status = ('ARM CONFIRMED' if actual else 'DISARM CONFIRMED')+' · A toggles · Esc returns'
                    except (Rejected, RuntimeError) as exc:
                        ui.workflow_status = 'COMMAND BLOCKED: '+str(exc)
                        report.event('manual_toggle_blocked', error=str(exc))
                    finally:
                        ui.manual_action = None
                        curses.flushinp()
                        ready_after = time.monotonic()+.5
    except Exception as exc:
        ui.view_state = 'PREFLIGHT BLOCKED'
        ui.workflow_status = 'Arm control unavailable: '+str(exc)
        report.event('arm_control_blocked', error=str(exc))
        return 3
    finally:
        ui.arm_control_active = False
        ui.manual_action = None
        if t is not None:
            t.close()


def disarm(config, ui):
    report = Report(Path(config['_base'])/'logs')
    t = None
    try:
        if not config['link']['device_confirmed'] or not config['commissioning']['wiring_props_off_verified']:
            raise Rejected('Confirmed UART and props-off wiring evidence required')
        with process_lock('mission.lock'):
            t = Transport(config, report)
            ui.transport = t
            ui.receive_only = False
            t.observer = ui.observe
            t.connect(config['link']['connect_timeout_s'])
            t.initialize_streams()
            t.sample(config['sampling']['fast_s'])
            mode = t.mode()
            guard = lambda: ground_guard(t, mode)
            guard()
            ui.view_state = 'CONFIRM GROUND DISARM'
            if ui.input_live('Type DISARM to stop motors on the ground; Escape cancels: ', t, guard=guard) != 'DISARM':
                return 3
            t.flight_authorized = True
            t.command(400, [0, 0], flight=True, guard=guard)
            deadline = time.monotonic()+config['mission']['command_timeout_s']
            while time.monotonic() < deadline:
                t.pump()
                guard()
                if not t.armed():
                    ui.view_state = 'DISARM CONFIRMED'
                    return 0
            raise Rejected('Disarm ACK received but disarmed heartbeat not observed')
    except Exception as exc:
        report.event('manual_disarm_blocked', error=str(exc))
        ui.view_state = 'PREFLIGHT BLOCKED'
        ui.workflow_status = 'Disarm blocked: '+str(exc)
        return 3
    finally:
        if t is not None:
            t.close()
