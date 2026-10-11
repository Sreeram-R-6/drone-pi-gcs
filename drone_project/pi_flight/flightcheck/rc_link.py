"""CRSF LQ exposed through ArduPilot's configurable RC_CHANNELS field."""
import math


def link_quality(store, rc, now):
    params = {}
    for stamp,d in store.samples.get('PARAM_VALUE',[]):
        if 0 <= now-stamp <= 15:
            name = str(d.get('param_id','')).rstrip('\x00')
            if name in ('RC_OPTIONS','RSSI_TYPE'):
                params[name] = d.get('param_value')
    options = params.get('RC_OPTIONS')
    if params.get('RSSI_TYPE') != 3 or not isinstance(options,(int,float)) or not math.isfinite(options) or not int(options) & (1<<11):
        return None,'LQ mapping unverified: requires RSSI_TYPE=3 and RC_OPTIONS bit 11; preserve other bits'
    value = rc.get('rssi')
    # ArduPilot CRSF derive_scaled_lq_value uses round(LQ * 2.5), not 2.55.
    # 255 remains ambiguous/unavailable; 100% LQ normally maps to 250.
    if not isinstance(value,(int,float)) or not math.isfinite(value) or not 0 <= value <= 250:
        return None,'CRSF link quality unavailable in RC_CHANNELS'
    return value/2.5,'CRSF LQ mapping verified from live FC parameters'
