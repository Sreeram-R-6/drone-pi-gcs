"""Accept pilot-controlled startup modes, while retaining takeover detection."""
from .telemetry import Rejected

START_MODES = ('STABILIZE','ALT_HOLD','LOITER','POSHOLD')


def confirmation_mode(transport):
    mode = getattr(transport,'preflight_mode',None) or transport.mode()
    if mode not in START_MODES:
        raise Rejected('Unsupported startup mode '+str(mode)+'; use '+', '.join(START_MODES))
    return mode


def check_confirmation_state(transport, expected_mode):
    if transport.armed() or transport.mode() != expected_mode:
        raise Rejected('Vehicle armed or pilot changed mode during preflight confirmation')
