"""Receive-only UART/UDP dashboard, independent of flight release."""
from pathlib import Path
import time
from .checks import evaluate
from .reporting import Report
from .telemetry import LinkLost, Transport, process_lock


FORWARDING_HELP = "Check mavproxy.service (shared UART router); OLED uses 14553, GCS uses 14550."
MISSION_SELECTED = 10 # Internal handoff to gated preflight, not a process exit code.
MANUAL_SELECTED = 11
SERVICE_SELECTED = 12
RECONNECT_SELECTED = 13


def dashboard_checks(checks):
    """Hide redundant monitor diagnostics, without weakening mission gates."""
    return [check for check in checks if check.name not in
            ('optical flow','OPTICAL_FLOW stream','OPTICAL_FLOW_RAD stream',
             'vehicle identity/state')]


def run(config, ui=None, timeout=None):
    """Record startup failures and leave a visible, retryable disconnected UI."""
    report = Report(Path(config['_base'])/'logs')
    try:
        return _run(config,ui,timeout,report)
    except (OSError,RuntimeError,ValueError) as exc:
        report.event('connection_error',error=str(exc),error_type=type(exc).__name__)
        print('CONNECTION ERROR: '+str(exc))
        if ui:
            ui.transport = None
            ui.receive_only = True
            ui.view_state = 'CONNECTION ERROR'
            ui.workflow_status = str(exc)+' · Fix cause, then press R to reconnect.'
            ui.notice = ui.workflow_status
        return 3


def _run(config, ui, timeout, report):
    """Receive only. Never starts another serial owner or changes FC state."""
    direct = config['link']['endpoint'].startswith('/dev/')
    link_name = f"UART {config['link']['endpoint']} @ {config['link']['baud']}" if direct else 'SHARED UART router · GCS 14550 / OLED 14553'
    help_text = 'Check FC power, TX/RX, common ground and FC UART baud/protocol.' if direct else FORWARDING_HELP
    if ui:
        ui.config,ui.report,ui.blockers = config,report,[]
        ui.receive_only = True
        ui.view_state = 'CONNECTING / RECEIVE ONLY'
        ui.notice = link_name
    report.event('monitor_start', endpoint=config['link']['endpoint'], receive_only=True)
    checks = []
    with process_lock('mission.lock'):
        transport = Transport(config,report,receive_only=True)
        try:
            if ui:
                ui.transport = transport
                transport.observer = ui.observe
            try:
                transport.connect(timeout or config['link']['connect_timeout_s'])
            except LinkLost:
                if ui:
                    ui.view_state = 'NO UART TELEMETRY' if direct else 'NO UDP TELEMETRY'
                    ui.notice = help_text
                print(f'WAITING: no matching FC heartbeat on {link_name}. '+help_text)
                report.event('not_connected', help=help_text)
                if ui is None:
                    return 4
            started = last_evaluation = time.monotonic()
            connected = False
            while True:
                transport.pump()
                now = time.monotonic()
                try:
                    hb = transport.require_live()
                    if hb.get('autopilot') != 3 or hb.get('type') != 2:
                        raise LinkLost('Waiting for configured ArduPilot quadrotor')
                    if not connected:
                        print(f'CONNECTED: {link_name}; receive-only telemetry. Flight commands remain locked.')
                        report.event('monitor_connected')
                    connected = True
                    if direct:
                        transport.request_monitor_telemetry()
                    if ui:
                        streams = sum(bool(transport.store.samples[k]) for k in ('ATTITUDE','GPS_RAW_INT','BATTERY_STATUS','LOCAL_POSITION_NED'))
                        ui.view_state = 'BENCH ONLY / FLIGHT DISABLED' if config.get('_bench_only',False) else 'FC CONNECTED / FLIGHT LOCKED' if direct else 'LIVE / RECEIVE ONLY'
                        ui.notice = f'{link_name} • FC {transport.target[0]}:{transport.target[1]} heartbeat OK • data streams {streams}/4'
                except LinkLost:
                    if ui:
                        ui.view_state = 'LINK STALE / RECEIVE ONLY' if connected else 'WAITING FOR FC' if direct else 'WAITING FOR MAVPROXY'
                        ui.notice = ('Telemetry stopped. ' if connected else 'No telemetry. ')+help_text
                        foreign = getattr(transport,'foreign_heartbeat',None)
                        if isinstance(foreign,tuple):
                            ui.notice = f'FC heartbeat from {foreign[0]}:{foreign[1]}; configured target {config["link"]["system_id"]}:{config["link"]["component_id"]}. Verify link IDs in config.toml.'
                if now-last_evaluation >= config['sampling']['fast_s']:
                    last_evaluation = now
                    checks = []
                    hb = transport.store.samples['HEARTBEAT']
                    airborne = bool(hb and hb[-1][1].get('base_mode',0) & 128)
                    checks += evaluate(transport.store,config,max(started,now-config['sampling']['full_s']),now,airborne=airborne,mission_readiness=False)
                    checks = dashboard_checks(checks)
                    if ui and not getattr(ui,'mission_selected',False):
                        ui.checks = checks
                if ui and ui.quit_requested:
                    return 0
                if ui and getattr(ui,'reconnect_requested',False):
                    return RECONNECT_SELECTED
                if ui and getattr(ui,'service_request',None):
                    return SERVICE_SELECTED
                if ui and getattr(ui,'manual_action',None):
                    return MANUAL_SELECTED # Close receive-only UART before command workflow.
                if ui and getattr(ui,'menu_requested',False):
                    ui.menu_requested = False
                    try:
                        if transport.armed():
                            ui.workflow_status = 'Mission selection blocked: vehicle already armed'
                            continue
                    except LinkLost:
                        # Browsing/selecting a route is safe without a live link.
                        # The gated preflight still requires fresh FC telemetry.
                        ui.workflow_status = 'Fresh FC telemetry required before mission start'
                    if ui.choose(transport=transport):
                        ui.workflow_status = 'Selected '+ui.route+' • checking flight readiness'
                        # Run inside this UART owner's lifetime: preserve telemetry,
                        # history and the serial/process locks throughout preflight.
                        from preflight_hover import main
                        mission_args = ['--check-once', '--flight']
                        if config.get('_bench_only', False):
                            mission_args.append('--bench')
                        try:
                            main(mission_args, ui=ui, existing_transport=transport)
                        finally:
                            ui.receive_only = True
                            ui.transport = transport
                            ui.config, ui.report = config, report
                            ui.mission = None
                            ui.request = None
                            ui.mission_selected = ui.view_state == 'PREFLIGHT BLOCKED'
                        # Keep the completed mission checklist until the next
                        # ordinary monitoring evaluation; never empty the panel.
                        last_evaluation = time.monotonic()
                        if ui.quit_requested:
                            return 0
                if ui is None and now-started >= config['sampling']['full_s']:
                    report.checklist(checks,'monitor')
                    return 0
        except KeyboardInterrupt:
            return 0
        finally:
            transport.close()
            report.event('monitor_closed')
