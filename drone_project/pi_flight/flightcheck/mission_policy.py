"""Reviewed dbe79216 test-mission policy, enforced through fresh FC readbacks.

No parameter writes and no flight commands occur here. Configuration of a policy
does not claim that the actual FC has been configured or flight-tested.
"""
import math
from .checks import Check
from .telemetry import Rejected


def policy_checks(t, config):
    policy = config['firmware'].get('mission_policy')
    if not policy:
        return [] # Other deployments retain their existing explicit review gate.
    onboard = policy == 'onboard_auto_v1'
    if (policy not in ('home_relative_land_v1', 'onboard_auto_v1') or
            config['aircraft']['flight_sw_version'] != 67568127 or
            config['firmware']['takeoff_altitude_reference'] != 'relative_home'):
        return [Check('mission takeoff/recovery policy', 'FAIL', policy,
                      'reviewed dbe79216 home-relative/LAND policy')]
    expected = (
        ('WP_RFND_USE', 0, 'takeoff reference: home-relative'),
        ('FS_GCS_ENABLE', 0 if onboard else 5,
         'Onboard AUTO independent of GCS heartbeat' if onboard else 'GCS-loss recovery: LAND'),
        ('FS_GCS_TIMEOUT', 5.0, 'GCS-loss timeout'),
        ('MAV_GCS_SYSID', config['link']['source_system'], 'GCS failsafe source ID'),
        ('MAV_GCS_SYSID_HI', 0, 'single-GCS failsafe identity'),
        ('FS_OPTIONS', 16, 'reviewed failsafe options'),
    )
    result = []
    if onboard:
        approved = config.get('commissioning', {}).get('onboard_auto_reviewed') is True
        result.append(Check('onboard AUTO commissioning', 'PASS' if approved else 'UNKNOWN',
                            approved, 'SITL link-loss/start/landing and physical RC recovery verified',
                            'Onboard AUTO backend is not flight-released until commissioning is complete.'))
        expected += (('ARMING_SKIPCHK', 0, 'FC arming checks enabled'),
                     ('FS_THR_ENABLE', 1, 'RC-loss recovery: RTL'),
                     ('FS_EKF_ACTION', 1, 'EKF-loss recovery: LAND'),
                     ('WP_SPD_UP', config['mission']['climb_m_s'], 'Onboard climb speed'),
                     ('MIS_RESTART', 1, 'Onboard mission restart policy'))
    for name, wanted, title in expected:
        try:
            value, kind = t.read_param(name)
            passed = isinstance(value, (int, float)) and math.isfinite(value) and math.isclose(value, wanted, rel_tol=0, abs_tol=1e-6)
            result.append(Check(title, 'PASS' if passed else 'FAIL',
                {'parameter': name, 'value': value, 'type': kind}, f'{name}={wanted}',
                '' if passed else f'{name} currently {value}; reviewed mission requires {wanted}. No FC write performed.'))
        except Rejected as exc:
            result.append(Check(title, 'UNKNOWN', None, f'{name}={wanted}', str(exc)))
    return result
