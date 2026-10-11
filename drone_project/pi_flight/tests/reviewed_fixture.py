"""Synthetic reviewed test inputs; never aircraft evidence or launch approval."""
from pathlib import Path
from flightcheck.config import load, bench_profile


def reviewed_config():
    c = bench_profile(load(Path(__file__).parents[1] / 'config.example.toml'))
    c.pop('_bench_only')
    c['link'].update(endpoint='udpin:127.0.0.1:14550', device_confirmed=True)
    c['aircraft'].update(firmware_target='TEST-BOARD', flight_sw_version=67568127,
                         board_version=76218368, uid=0, uid2='01' * 18,
                         pi_serial_group='SERIAL4', flow_serial_group='SERIAL5')
    c['commissioning'].update(thresholds_approved=True)
    c['firmware'].update(climb_parameter='WP_SPD_UP', climb_parameter_type=9,
                        climb_parameter_units_per_m_s=1.0,
                        climb_parameter_min=0.1, climb_parameter_max=10.0)
    c['attitude']['max_ekf_variance'] = 1.0
    c['gcs'].update(routes_reviewed=True, monitor_ekf_variance=2.0)
    return c
