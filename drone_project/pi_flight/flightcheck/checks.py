"""Pure checks: no transport, commands, or side effects."""
from dataclasses import dataclass
import math
import statistics as stats
from .reporting import utc


@dataclass
class Check:
    name: str
    state: str
    measured: object
    requirement: str
    explanation: str = ""
    evidence_timestamp: str = ""
    required: bool = True

    def __post_init__(self):
        if self.state not in ("PASS", "WARN", "FAIL", "UNKNOWN"):
            raise ValueError("Invalid check state")
        if not self.evidence_timestamp:
            self.evidence_timestamp = utc()


def all_pass(checks):
    required = [c for c in checks if c.required]
    return bool(required) and all(c.state == "PASS" for c in required)


def percentile(values, fraction):
    values = sorted(values)
    offset = (len(values) - 1) * fraction
    lo = int(offset)
    return values[lo] + (values[min(lo + 1, len(values)-1)] - values[lo]) * (offset-lo)


def ne_offset(lat, lon, lat0, lon0):
    """WGS84 tangent-plane approximation for stationary small-distance samples."""
    phi = math.radians(lat0)
    a, e2 = 6378137.0, 0.00669437999014
    w = math.sqrt(1 - e2 * math.sin(phi)**2)
    meridian, prime = a * (1-e2) / w**3, a / w
    longitude_delta = (lon - lon0 + 180) % 360 - 180
    return (math.radians(lat-lat0)*meridian,
            math.radians(longitude_delta)*prime*math.cos(phi))


def gps_statistics(samples, duration):
    if len(samples) < 2 or duration <= 0:
        raise ValueError("Insufficient GPS samples/window")
    if any(not all(math.isfinite(s[k]) for k in ("lat", "lon", "vel")) for s in samples):
        raise ValueError("Nonfinite GPS")
    lat0 = stats.median(s["lat"] / 1e7 for s in samples)
    # Unwrap near the first longitude, including dateline crossings.
    lon_ref = samples[0]["lon"] / 1e7
    lon0 = stats.median(lon_ref + ((s["lon"]/1e7-lon_ref+180) % 360-180)
                        for s in samples)
    points = [ne_offset(s["lat"]/1e7, s["lon"]/1e7, lat0, lon0) for s in samples]
    radii = [math.hypot(*p) for p in points]
    return {"count": len(samples), "rate_hz": len(samples)/duration,
            "center_deg": [lat0, lon0],
            "rms_m": math.sqrt(stats.mean(r*r for r in radii)),
            "p95_m": percentile(radii, .95), "radius_m": max(radii),
            "drift_m": math.dist(points[0], points[-1]),
            "step_m": max(math.dist(a, b) for a, b in zip(points, points[1:])),
            "speed_m_s": max(s["vel"]/100 for s in samples), "offsets_ne_m": points}


def stream_health(samples, start, end, min_hz, max_age, max_gap=None):
    """Arrival timestamps are monotonic; startup gaps and duplicate times count."""
    times = [s[0] for s in samples if start <= s[0] <= end]
    if not times:
        return "UNKNOWN", {"count": 0, "rate_hz": 0}
    duration = end - start
    gap_limit = max_age if max_gap is None else max_gap
    gaps = [times[0]-start, *[b-a for a,b in zip(times,times[1:])], end-times[-1]]
    values = {"count": len(times), "rate_hz": len(times)/duration if duration > 0 else 0,
              "age_s": end-times[-1], "max_gap_s": max(gaps),
              "dropouts": sum(g > gap_limit for g in gaps)}
    ok = (len(times) >= 2 and len(set(times)) == len(times) and
          values["rate_hz"] >= min_hz and values["age_s"] <= max_age and
          values["max_gap_s"] <= gap_limit)
    return ("PASS" if ok else "FAIL"), values


def evaluate(store, config, start, end, airborne=False, mission_readiness=True):
    """Evaluate every sample, not just the newest packet; never trim outliers."""
    checks = []
    def add(name, ok, value, requirement, explanation=""):
        checks.append(Check(name, "PASS" if ok else "FAIL", value, requirement,
                            explanation or ('' if ok else 'Outside requirement: '+requirement)))
    def rows(kind):
        return [d for t,d in store.samples.get(kind, []) if start <= t <= end]
    flow_kind = 'OPTICAL_FLOW_RAD' if rows('OPTICAL_FLOW_RAD') else 'OPTICAL_FLOW'
    streams = {
        "HEARTBEAT": config["sampling"]["heartbeat_hz"],
        "GPS_RAW_INT": config["sampling"]["position_hz"],
        "GLOBAL_POSITION_INT": config["sampling"]["position_hz"],
        "LOCAL_POSITION_NED": config["sampling"]["position_hz"],
        "ATTITUDE": config["sampling"]["sensor_hz"],
        "EKF_STATUS_REPORT": config["sampling"]["sensor_hz"],
        "SYS_STATUS": config["sampling"]["sensor_hz"],
        "BATTERY_STATUS": config["sampling"]["sensor_hz"],
        "RC_CHANNELS": config["sampling"]["sensor_hz"],
        flow_kind: config["flow"]["min_hz"],
        "DISTANCE_SENSOR": config["sampling"]["sensor_hz"],
    }
    for kind, hz in streams.items():
        samples = store.samples.get(kind, [])
        selection = {"BATTERY_STATUS": ("id", config["battery"]["sensor_id"]),
                     "DISTANCE_SENSOR": ("id", config["range"]["sensor_id"]),
                     flow_kind: ("sensor_id", config["flow"]["sensor_id"])}
        if kind in selection:
            key, expected = selection[kind]
            if expected < 0:
                observed = sorted({d.get(key) for t,d in samples if start <= t <= end and isinstance(d.get(key),int)})
                checks.append(Check(kind+' stream','UNKNOWN',{'observed_ids':observed},'verified sensor ID',
                                    'Sensor ID unset; observed IDs: '+str(observed)))
                continue
            samples = [(t,d) for t,d in samples if d.get(key) == expected]
        if hz <= 0:
            checks.append(Check(kind+' stream','UNKNOWN',None,'approved positive stream rate',
                                'Stream-rate limit unset'))
            continue
        state, value = stream_health(samples, start, end,
                                     hz, config["link"]["max_age_s"])
        checks.append(Check(kind + " stream", state, value, f">={hz} Hz, fresh, no gaps"))
    limits = {
        'GPS quality/stability': [('gps',k) for k in ('min_satellites','max_hdop','max_rms_m','max_p95_m','max_radius_m','max_drift_m','max_speed_m_s','max_step_m')],
        'EKF': [('attitude','max_ekf_variance')],
        'battery': [('battery',k) for k in ('min_voltage_v','min_remaining_pct','max_current_a')],
        'attitude/heading': [('attitude',k) for k in ('max_stationary_tilt_deg','max_heading_span_deg')],
        'local position/origin continuity': [('attitude','max_altitude_span_m'),('gps','max_step_m')],
        'optical flow': [('flow',k) for k in (('min_quality','min_hz','max_gap_s','max_compensated_m_s')
                          if flow_kind == 'OPTICAL_FLOW' else ('min_quality','min_hz','max_gap_s','max_integration_us','max_compensated_rad_s'))],
        'downward range': [('range','usable_min_m'),('range','usable_max_m')],
    }
    def guarded(name, function):
        try:
            required_limits = limits.get(name,[])
            if airborne and name in ('attitude/heading','local position/origin continuity'):
                required_limits = []
            if airborne and name == 'GPS quality/stability':
                required_limits = [('gps','min_satellites'),('gps','max_hdop')]
            unset = [section+'.'+key for section,key in required_limits if config[section].get(key,0) <= 0]
            if unset:
                checks.append(Check(name,'UNKNOWN',{'unset_limits':unset},'approved acceptance limits',
                                    'Limits unset: '+', '.join(unset)))
                return
            function()
        except (KeyError, IndexError, ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
            checks.append(Check(name, "UNKNOWN", None, "complete finite evidence", str(exc)))
    def heartbeat():
        values = rows("HEARTBEAT")
        add("vehicle identity/state", bool(values) and all(
            d["autopilot"] == 3 and d["type"] == 2 and d["system_status"] in (3,4)
            and bool(d["base_mode"] & 128) == airborne for d in values), values[-1],
            "ArduPilot quadrotor, standby/active, expected arm state")
    guarded("vehicle identity/state", heartbeat)
    def gps():
        values = rows("GPS_RAW_INT")
        g = config["gps"]
        result = gps_statistics(values, end-start)
        reasons = []
        if any(d['fix_type'] < 3 for d in values):
            reasons.append('3D fix lost in sample window')
        if any(d['satellites_visible'] == 255 or d['satellites_visible'] < g['min_satellites'] for d in values):
            reasons.append(f'Satellites below {g["min_satellites"]} or unavailable')
        if any(d['eph'] in (0,65535) or d['eph'] > g['max_hdop']*100 for d in values):
            reasons.append(f'HDOP above {g["max_hdop"]} or unavailable')
        if any(d['vel'] == 65535 for d in values):
            reasons.append('GPS speed unavailable')
        quality = all(d["fix_type"] >= 3 and d["satellites_visible"] != 255 and
                      d["satellites_visible"] >= g["min_satellites"] > 0 and
                      0 < d["eph"] <= g["max_hdop"]*100 and d["eph"] != 65535
                      and d["vel"] != 65535 for d in values)
        if not airborne:
            quality = quality and all(0 < g["max_"+key] and result[key] <= g["max_"+key]
                for key in ("rms_m", "p95_m", "radius_m", "drift_m", "speed_m_s", "step_m"))
            for key in ('rms_m','p95_m','radius_m','drift_m','speed_m_s','step_m'):
                if result[key] > g['max_'+key]:
                    reasons.append(f'{key} {result[key]:.2f} > {g["max_"+key]:.2f}')
        result.update(fix_min=min(d['fix_type'] for d in values),
                      satellites_min=min(d['satellites_visible'] for d in values),
                      hdop_max=max(d['eph']/100 for d in values))
        add("GPS quality/stability", quality, result, "approved fix/HDOP/satellites/jitter limits",
            'Failed: '+'; '.join(reasons) if reasons else 'Stationary jitter does not validate multipath or compass/EKF in flight')
    guarded("GPS quality/stability", gps)
    def ekf():
        values = rows("EKF_STATUS_REPORT")
        required = 1 | 2 | 4 | 16 | 32 # attitude, H/V velocity, absolute H position, V position
        if config.get('_bench_only',False):
            required = 1 | 32 # Bench display health only; NOT horizontal flight readiness.
        elif not mission_readiness:
            required = 1 | 2 | 4 | 32 # Dashboard doesn't require absolute horizontal position.
        forbidden = 128 | 32768 # constant position, uninitialized
        limit = config["attitude"]["max_ekf_variance"]
        if not mission_readiness:
            limit = config.get('gcs',{}).get('monitor_ekf_variance',limit)
        missing = sorted({d['flags'] & forbidden for d in values} - {0})
        absent = sorted({required & ~d['flags'] for d in values} - {0})
        flag_names = {1:'attitude estimate',2:'horizontal velocity estimate',
                      4:'vertical velocity estimate',16:'absolute horizontal position (GPS/global position)',
                      32:'vertical position estimate'}
        unavailable = [name for bit,name in flag_names.items()
                       if any(mask & bit for mask in absent)]
        unsafe = [name for bit,name in ((128,'constant-position mode'),(32768,'EKF uninitialized'))
                  if any(mask & bit for mask in missing)]
        explanation = ('Not ready: '+', '.join(unavailable)+'. ' if unavailable else '')
        if unsafe:
            explanation += 'Unsafe estimator state: '+', '.join(unsafe)+'. '
        add("EKF" if mission_readiness else 'EKF [diagnostic]', bool(values) and math.isfinite(limit) and limit > 0 and all(
            d["flags"] & required == required and not d["flags"] & forbidden and
            all(math.isfinite(d[k]) and 0 <= d[k] <= limit for k in
                ("velocity_variance", "pos_horiz_variance", "pos_vert_variance", "compass_variance"))
            for d in values), {'latest':values[-1],'required_flags':required,'variance_limit':limit},
            'required EKF flags; bounded variances',
            explanation+f'Variance limit: {limit}. '+
            ('Mission horizontal-position checks enforced.' if mission_readiness else 'Diagnostic only, not approval for position hold.'))
    guarded("EKF", ekf)
    def sensors():
        values = rows("SYS_STATUS")
        from .maintenance import sensor_details
        # gyro, accel, compass, pressure, GPS, optical flow, RC, prearm check.
        required = 1 | 2 | 4 | 8 | 32 | 64 | 65536 | 268435456
        details = [sensor_details(d) for d in values]
        problems = sorted({category+': '+', '.join(items) for detail in details for category,items in detail.items() if items})
        add("onboard sensor health", bool(values) and all(
            all(d[k] & required == required for k in
                ("onboard_control_sensors_present", "onboard_control_sensors_enabled",
                 "onboard_control_sensors_health")) and
            d["onboard_control_sensors_health"] & d["onboard_control_sensors_enabled"] ==
            d["onboard_control_sensors_enabled"] for d in values),
            {'latest':values[-1],'sensor_bits':details[-1]},
            "required sensors present/enabled/healthy; all enabled sensors healthy",
            '; '.join(problems))
    guarded("onboard sensor health", sensors)
    def battery():
        b = config["battery"]
        values = [d for d in rows("BATTERY_STATUS") if d["id"] == b["sensor_id"]]
        measured = []
        for d in values:
            volts = [v for v in d["voltages"] if v != 65535]
            v = sum(volts)/1000
            measured.append((v, d["current_battery"]/100, d["battery_remaining"]))
        add("battery", bool(measured) and b["min_voltage_v"] > 0 and b["min_remaining_pct"] > 0
            and b["max_current_a"] > 0 and all(
                b["min_voltage_v"] <= v <= b["max_voltage_v"] and
                0 <= a <= b["max_current_a"] and b["min_remaining_pct"] <= p <= 100
                for v,a,p in measured), measured, "approved calibrated 4S voltage/current/remaining")
    guarded("battery", battery)
    def flow():
        f = config["flow"]
        if flow_kind == 'OPTICAL_FLOW':
            values = [d for d in rows(flow_kind) if d.get('sensor_id') == f['sensor_id']]
            samples = [(stamp,d) for stamp,d in store.samples.get(flow_kind,[]) if d.get('sensor_id') == f['sensor_id']]
            state,health = stream_health(samples,start,end,f['min_hz'],config['link']['max_age_s'],f['max_gap_s'])
            valid = bool(values) and all(f['min_quality'] <= d['quality'] <= 255 and
                all(math.isfinite(d[k]) for k in ('flow_comp_m_x','flow_comp_m_y')) for d in values)
            speed = max((math.hypot(d['flow_comp_m_x'],d['flow_comp_m_y']) for d in values),default=0)
            if not airborne:
                valid = valid and speed <= f['max_compensated_m_s']
            reasons = []
            if not values:
                reasons.append('No samples for configured optical-flow sensor ID')
            elif any(not f['min_quality'] <= d['quality'] <= 255 for d in values):
                reasons.append(f'opt_qua below {f["min_quality"]} or invalid')
            if any(not all(math.isfinite(d[k]) for k in ('flow_comp_m_x','flow_comp_m_y')) for d in values):
                reasons.append('Non-finite opt_m_x/opt_m_y')
            if not airborne and speed > f['max_compensated_m_s']:
                reasons.append(f'Stationary compensated speed {speed:.2f} m/s > {f["max_compensated_m_s"]:.2f} m/s')
            if state != 'PASS':
                reasons.append('Optical-flow stream rate/freshness/gaps outside limits')
            add('optical flow',valid and state == 'PASS',
                {'source':'OPTICAL_FLOW','sensor_id':f['sensor_id'],
                 'opt_m_x':values[-1].get('flow_comp_m_x') if values else None,
                 'opt_m_y':values[-1].get('flow_comp_m_y') if values else None,
                 'opt_qua_min':min((d['quality'] for d in values),default=None),
                 'compensated_m_s_max':speed,'stream':health},
                'identified sensor; finite opt_m_x/opt_m_y; sufficient opt_qua; fresh stream; bounded stationary flow',
                ('Failed: '+'; '.join(reasons)+'. ' if reasons else '')+
                'Legacy OPTICAL_FLOW values evaluated directly. Calibration/fusion approval is separate.')
            return
        values = rows("OPTICAL_FLOW_RAD")
        rates = []
        for d in values:
            dt = d["integration_time_us"] / 1e6
            rates.append(math.hypot(d["integrated_x"]-d["integrated_xgyro"],
                                   d["integrated_y"]-d["integrated_ygyro"]) / dt)
        valid = bool(values) and f["min_quality"] > 0 and f["max_integration_us"] > 0 and all(
            d["sensor_id"] == f["sensor_id"] and f["min_quality"] <= d["quality"] <= 255 and
            0 < d["integration_time_us"] <= f["max_integration_us"] and
            all(math.isfinite(d[k]) for k in
                ("integrated_x", "integrated_y", "integrated_xgyro", "integrated_ygyro", "integrated_zgyro"))
            for d in values)
        state, health = stream_health([(t,d) for t,d in store.samples.get("OPTICAL_FLOW_RAD", [])
                                      if d.get("sensor_id") == f["sensor_id"]], start, end,
            f["min_hz"], config["link"]["max_age_s"], f["max_gap_s"])
        if not airborne:
            valid = valid and f["max_compensated_rad_s"] > 0 and max(rates) <= f["max_compensated_rad_s"]
        add("optical flow", valid and state == "PASS",
            {"quality_min": min(d["quality"] for d in values),
             "quality_median": stats.median(d["quality"] for d in values),
             "compensated_rad_s_max": max(rates), "stream": health},
            "identified sensor, finite flow/gyro, integration, quality, bias and rate limits",
            "Static stream cannot prove axes, scaling, compensation or EKF fusion")
    guarded("optical flow", flow)
    def ranging():
        r = config["range"]
        values = [d for d in rows("DISTANCE_SENSOR") if d["id"] == r["sensor_id"]]
        add("downward range", bool(values) and r["usable_max_m"] > r["usable_min_m"] > 0 and all(
            d["orientation"] == 25 and
            max(r["usable_min_m"]*100, d["min_distance"]) <= d["current_distance"] <=
            min(r["usable_max_m"]*100, d["max_distance"]) and
            d["covariance"] != 255 and 0 <= d["covariance"] <= r["max_covariance_cm2"]
            for d in values), values[-1], "measured usable envelope, downward, known covariance")
    guarded("downward range", ranging)
    def attitude():
        values = rows("ATTITUDE")
        tilt = [math.degrees(math.hypot(d["roll"], d["pitch"])) for d in values]
        yaw = [math.degrees(math.atan2(math.sin(d["yaw"]-values[0]["yaw"]),
                                      math.cos(d["yaw"]-values[0]["yaw"]))) for d in values]
        limit = config["mission"]["max_tilt_deg"] if airborne else config["attitude"]["max_stationary_tilt_deg"]
        add("attitude/heading", limit > 0 and all(math.isfinite(x) for x in tilt+yaw) and
            max(tilt) <= limit and (airborne or
            0 < config["attitude"]["max_heading_span_deg"] >= max(yaw)-min(yaw)),
            {"tilt_mean_deg": stats.mean(tilt), "tilt_rms_deg": math.sqrt(stats.mean(v*v for v in tilt)),
             "tilt_max_deg": max(tilt), "heading_span_deg": max(yaw)-min(yaw)},
            "level/stable within reviewed limits; heading separately checked against real direction")
    guarded("attitude/heading", attitude)
    def positions():
        values = rows("LOCAL_POSITION_NED")
        keys = ("x", "y", "z", "vx", "vy", "vz")
        valid = all(all(math.isfinite(d[k]) for k in keys) for d in values)
        span = max(d["z"] for d in values)-min(d["z"] for d in values)
        steps = [math.dist([a[k] for k in ("x","y","z")], [b[k] for k in ("x","y","z")])
                 for a,b in zip(values, values[1:])]
        valid = valid and not store.origin_reset and not store.boot_reset
        if not airborne:
            valid = valid and 0 < config["attitude"]["max_altitude_span_m"] >= span
            valid = valid and bool(steps) and 0 < config["gps"]["max_step_m"] >= max(steps)
            global_values = rows("GLOBAL_POSITION_INT")
            relative_span = (max(d["relative_alt"] for d in global_values)-
                             min(d["relative_alt"] for d in global_values))/1000
            valid = valid and relative_span <= config["attitude"]["max_altitude_span_m"]
            local_delta = values[0]["z"]-values[-1]["z"]
            global_delta = (global_values[-1]["relative_alt"]-global_values[0]["relative_alt"])/1000
            valid = valid and abs(local_delta-global_delta) <= config["mission"]["max_altitude_disagreement_m"]
        add("local position/origin continuity", valid, {"altitude_span_m": span, "steps_m": steps},
            "finite positions, stable ground baseline, no origin/reset discontinuity")
    guarded("local position/origin continuity", positions)
    rc = rows("RC_CHANNELS")
    add("RC receiver", bool(rc) and all(d["chancount"] >= 4 and
        all(800 <= d.get(f"chan{i}_raw", 0) <= 2200 for i in range(1,5)) for d in rc),
        rc[-1] if rc else None, "fresh RC with at least four valid PWM channels",
        '' if rc else 'No RC_CHANNELS telemetry received')
    add("STATUSTEXT faults", not store.faults, store.faults, "no unresolved fault/prearm/failsafe text",
        'Retained this session; may be historical, not current: '+'; '.join(store.faults) if store.faults else '')
    return checks
