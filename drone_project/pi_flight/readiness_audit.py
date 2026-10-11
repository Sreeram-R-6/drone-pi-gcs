#!/usr/bin/env python3
"""Non-actuating, disarmed-only audit on the spare local router output.

Only parameter reads and MAVLink message/interval requests are sent. Never
writes parameters, changes modes, requests calibration, arms or starts flight.
"""
import argparse
import json
import os
from pathlib import Path
import time

PARAMETERS = (
    'ARMING_SKIPCHK', 'EK3_SRC1_POSXY', 'EK3_SRC1_VELXY', 'EK3_SRC1_POSZ',
    'EK3_SRC1_VELZ', 'EK3_SRC1_YAW', 'AHRS_EKF_TYPE', 'WP_SPD_UP',
    'WP_RFND_USE', 'GUID_OPTIONS', 'RNGFND1_TYPE', 'RNGFND1_ORIENT',
    'RNGFND1_MIN', 'RNGFND1_MAX', 'FLOW_TYPE', 'FLOW_FXSCALER',
    'FLOW_FYSCALER', 'BATT_MONITOR', 'BATT_CAPACITY', 'BATT_ARM_VOLT',
    'BATT_LOW_VOLT', 'BATT_CRT_VOLT', 'BATT_FS_LOW_ACT', 'BATT_FS_CRT_ACT',
    'FS_THR_ENABLE', 'FS_THR_VALUE', 'FS_GCS_ENABLE', 'FS_GCS_TIMEOUT',
    'MAV_GCS_SYSID', 'MAV_GCS_SYSID_HI', 'FS_EKF_ACTION', 'FS_OPTIONS',
    'SERIAL4_PROTOCOL', 'SERIAL4_BAUD', 'SERIAL5_PROTOCOL', 'SERIAL5_BAUD',
    'FENCE_ENABLE', 'FENCE_RADIUS', 'FENCE_ALT_MAX', 'RTL_ALT_M', 'LAND_SPD_MS',
)


def disarmed(heartbeat):
    return not heartbeat['base_mode'] & 128


def run(seconds=12):
    os.environ['MAVLINK20'] = '1'
    from pymavlink import mavutil
    from flightcheck.telemetry import process_lock
    with process_lock('readiness-audit.lock'):
        c = mavutil.mavlink_connection('udpin:127.0.0.1:14552',
            source_system=251, source_component=191, dialect='ardupilotmega')
        result = {'non_actuating': True, 'parameters': {}, 'messages': {},
                  'flow_samples': [], 'request_acks': []}
        hb_stamp = None

        def receive(timeout):
            nonlocal hb_stamp
            m = c.recv_match(blocking=True, timeout=timeout)
            if not m or (m.get_srcSystem(), m.get_srcComponent()) != (1, 1):
                return
            d, kind = m.to_dict(), m.get_type()
            if kind == 'HEARTBEAT':
                if not disarmed(d):
                    raise RuntimeError('FC armed: stopping audit without further requests')
                hb_stamp = time.monotonic()
            if kind == 'PARAM_VALUE':
                if d['param_id'] in PARAMETERS:
                    result['parameters'][d['param_id']] = {
                        'value': d['param_value'], 'type': d['param_type']}
            elif kind == 'OPTICAL_FLOW':
                result['flow_samples'].append(dict(stamp=time.monotonic(), **d))
                result['messages'][kind] = d
            elif kind == 'COMMAND_ACK' and d['command'] in (511, 512):
                result['request_acks'].append(d)
            elif kind in ('HEARTBEAT', 'AUTOPILOT_VERSION', 'SYS_STATUS',
                    'GPS_RAW_INT', 'BATTERY_STATUS', 'EKF_STATUS_REPORT',
                    'EXTENDED_SYS_STATE', 'HOME_POSITION', 'GPS_GLOBAL_ORIGIN',
                    'DISTANCE_SENSOR', 'OPTICAL_FLOW_RAD'):
                result['messages'][kind] = d

        def guard():
            if hb_stamp is None or time.monotonic() - hb_stamp > 2:
                raise RuntimeError('No fresh disarmed FC heartbeat: audit stopped')

        try:
            deadline = time.monotonic() + 5
            while hb_stamp is None and time.monotonic() < deadline:
                receive(.2)
            guard()
            for message_id in (148, 242, 49):
                receive(.01)
                guard()
                c.mav.command_long_send(1, 1, 512, 0, message_id, 0, 0, 0, 0, 0, 0)
            for message_id in (100, 106, 132):
                receive(.01)
                guard()
                c.mav.command_long_send(1, 1, 511, 0, message_id, 200000, 0, 0, 0, 0, 0)
            for name in PARAMETERS:
                receive(.01)
                guard()
                c.mav.param_request_read_send(1, 1, name.encode(), -1)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                receive(.2)
                guard()
        finally:
            c.close()
        result['missing_parameters'] = [n for n in PARAMETERS if n not in result['parameters']]
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists; choose a new evidence file')
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2)
    print('Read-only evidence saved:', args.output)
    print('Parameters:', len(result['parameters']), 'missing:', result['missing_parameters'])
    print('OPTICAL_FLOW samples:', len(result['flow_samples']))


if __name__ == '__main__':
    main()
