"""Explicit disarmed FC diagnostics and backed-up DataFlash log management."""
import curses
import hashlib
import json
from pathlib import Path
import time
from .checks import Check, evaluate
from .manual import ground_guard
from .reporting import Report
from .telemetry import Transport, Rejected, process_lock


def sensor_details(data):
    names = {1:'gyro',2:'accelerometer',4:'compass',8:'barometer',32:'GPS',
             64:'optical flow',65536:'RC',268435456:'pre-arm check'}
    required = sum(names)
    present = data.get('onboard_control_sensors_present',0)
    enabled = data.get('onboard_control_sensors_enabled',0)
    health = data.get('onboard_control_sensors_health',0)
    decode = lambda mask:[names.get(1<<bit,f'bit {bit}') for bit in range(32) if mask & (1<<bit)]
    return {'missing_required':decode(required & ~present),
            'disabled_required':decode(required & ~enabled),
            'unhealthy_required':decode(required & ~health),
            'unhealthy_enabled':decode(enabled & ~health)}


def list_logs(t, guard):
    for _ in range(3):
        guard()
        t.store.samples['LOG_ENTRY'].clear()
        t.connection.mav.log_request_list_send(*t.target,0,65535)
        deadline = time.monotonic()+10
        entries = {}
        count = None
        while time.monotonic() < deadline:
            t.pump()
            t.heartbeat()
            guard()
            for _,data in t.store.samples['LOG_ENTRY']:
                if count is not None and count != data['num_logs']:
                    raise Rejected('FC log inventory changed during listing')
                count = data['num_logs']
                if count == 0:
                    return []
                entries[data['id']] = dict(data)
            if count is not None and len(entries) == count:
                return sorted(entries.values(),key=lambda d:d['id'])
    raise Rejected('Incomplete FC log list; erase not permitted')


def download_one(t, entry, destination, guard, progress):
    size = entry['size']
    part = destination.with_suffix('.bin.part')
    digest = hashlib.sha256()
    with part.open('xb') as stream:
        offset = 0
        while offset < size:
            stop = min(size,offset+32760)
            chunks = {}
            cursor = offset
            for _ in range(4):
                guard()
                t.store.samples['LOG_DATA'].clear()
                t.connection.mav.log_request_data_send(*t.target,entry['id'],cursor,stop-cursor)
                deadline = time.monotonic()+8
                while time.monotonic() < deadline:
                    t.pump()
                    t.heartbeat()
                    guard()
                    for _,d in t.store.samples['LOG_DATA']:
                        if d['id'] != entry['id'] or not offset <= d['ofs'] < stop:
                            continue
                        raw = bytes.fromhex(d['data']) if isinstance(d['data'],str) else bytes(d['data'])
                        count = d['count']
                        if not 0 < count <= 90 or count > len(raw) or d['ofs']+count > stop:
                            continue
                        chunk = raw[:count]
                        if d['ofs'] in chunks and chunks[d['ofs']] != chunk:
                            raise Rejected('Conflicting log packets; download not verified')
                        chunks[d['ofs']] = chunk
                    while cursor in chunks:
                        cursor += len(chunks[cursor])
                    if cursor == stop:
                        break
                if cursor == stop:
                    break
            if cursor != stop:
                raise Rejected(f'Log {entry["id"]}: missing bytes at {cursor}; partial file retained')
            while offset < stop:
                chunk = chunks[offset]
                stream.write(chunk)
                digest.update(chunk)
                offset += len(chunk)
            progress(offset,size)
    if part.stat().st_size != size:
        raise Rejected('Downloaded size differs from FC log inventory')
    part.rename(destination)
    return digest.hexdigest()


def verify_backup(root, manifest, entries, identity):
    if not identity or manifest.get('uid') != identity:
        raise Rejected('Backup FC identity missing/mismatched; download again before erase')
    backup = manifest.get('logs',[])
    if {(d['id'],d['size'],d['time_utc']) for d in backup} != {(d['id'],d['size'],d['time_utc']) for d in entries}:
        raise Rejected('FC logs changed since download; download all logs again')
    for log in backup:
        path = root/f'log-{log["id"]}.bin'
        if path.is_symlink() or not path.is_file() or path.stat().st_size != log['size']:
            raise Rejected('Backup file missing or size mismatch; erase blocked')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):
                digest.update(chunk)
        if digest.hexdigest() != log['sha256']:
            raise Rejected('Backup checksum mismatch; erase blocked')


def run(config, ui, action):
    report = Report(Path(config['_base'])/'logs')
    t = None
    ui.service_active = True
    ui.service_exit = False
    ui.receive_only = False
    ui.mission_selected = False
    ui.view_state = action.upper()+' / CONNECTING'
    ui.workflow_status = 'No mission. Maintenance requires a disarmed FC.'
    ui.draw(force=True)
    try:
        with process_lock('mission.lock'):
            t = Transport(config,report)
            ui.transport = t
            t.observer = ui.observe
            t.connect(config['link']['connect_timeout_s'])
            mode = t.mode()
            def guard():
                t.require_live()
                if t.armed() or t.mode() != mode or ui.service_exit or ui.quit_requested:
                    raise Rejected('Maintenance cancelled or FC arm/mode state changed')
            guard()
            t.request_monitor_telemetry()
            try:
                t.command(512,[148],guard=guard)
            except Rejected as exc:
                report.event('version_request_unavailable',error=str(exc))
            if action == 'diagnostics':
                ui.workflow_status = 'Reading complete FC parameter list and requesting pre-arm checks...'
                t.export_parameters()
                guard()
                try:
                    ack = t.command(401,guard=guard)
                    report.event('prearm_request',ack=ack)
                    prearm = Check('FC pre-arm request','UNKNOWN',ack,
                                   'review FC-reported results; ACK is not an all-clear',required=False)
                except Rejected as exc:
                    report.event('prearm_request_rejected',error=str(exc))
                    ui.write('Pre-arm request unavailable: '+str(exc))
                    prearm = Check('FC pre-arm request','UNKNOWN',str(exc),
                                   'supported FC pre-arm diagnostic request',required=False)
                start,end = t.sample(config['sampling']['fast_s'])
                guard()
                ui.checks = evaluate(t.store,config,start,end)
                ui.checks.append(prearm)
                settings = {k:v[:2] for k,v in t.params.items() if
                    k.startswith(('FS_','ARMING_','BRD_SAFETY')) or 'FS_' in k or 'FAILSAFE' in k}
                report.save('failsafe-prearm-settings.json',settings)
                report.save('reported-statustext.json',list(t.store.samples['STATUSTEXT']))
                ui.checks += [Check('FC text: '+str(d.get('text','')),
                              'FAIL' if d.get('severity',7) <= 3 or 'prearm' in str(d.get('text','')).lower() else 'UNKNOWN',
                              {'received_monotonic':stamp,**d},'reported FC text; not necessarily a current condition',
                              'Captured during this diagnostic session; consult message and timestamp',required=False)
                              for stamp,d in t.store.samples['STATUSTEXT']]
                ui.checks += [Check(f'{k} = {v[0]}','PASS',v,'value read from FC; not safety approval',required=False)
                              for k,v in sorted(settings.items())]
                report.checklist(ui.checks,'diagnostics')
                ui.workflow_status = 'FC diagnostics saved: '+str(report.path)+'. C details; PgUp/PgDn; Esc returns. No message is not proof all checks passed.'
            elif action in ('download-logs','erase-logs'):
                guard()
                entries = list_logs(t,guard)
                root = Path(config['_base'])/'fc_logs'
                root.mkdir(exist_ok=True)
                identity = (t.version or {}).get('uid')
                if action == 'download-logs':
                    destination = root/report.path.name
                    destination.mkdir()
                    logs = []
                    for i,entry in enumerate(entries,1):
                        def progress(done,size):
                            ui.workflow_status = f'Log {i}/{len(entries)} (ID {entry["id"]}): {done}/{size} bytes'
                        progress(0,entry['size'])
                        sha = download_one(t,entry,destination/f'log-{entry["id"]}.bin',guard,progress)
                        logs.append({**entry,'sha256':sha})
                    guard()
                    current = list_logs(t,guard)
                    if [(d['id'],d['size'],d['time_utc']) for d in current] != [(d['id'],d['size'],d['time_utc']) for d in entries]:
                        raise Rejected('Log inventory changed; backup not approved for erase')
                    manifest = {'uid':identity,'target':t.target,'logs':logs}
                    (destination/'manifest.json').write_text(json.dumps(manifest,indent=2))
                    ui.workflow_status = f'Downloaded {len(logs)} complete logs to {destination}. FC logs not erased. Esc returns.'
                else:
                    if not entries:
                        raise Rejected('FC reports no logs to erase')
                    candidates = sorted(root.glob('*/manifest.json'),reverse=True)
                    if not candidates:
                        raise Rejected('Download all logs with L before clearing FC logs')
                    manifest_path = candidates[0]
                    manifest = json.loads(manifest_path.read_text())
                    verify_backup(manifest_path.parent,manifest,entries,identity)
                    erase_guard = lambda:(guard(),ground_guard(t,mode))
                    erase_guard()
                    if ui.input_live('Erase ALL FC logs? Verified local backup exists. Type ERASE LOGS; Escape cancels: ',t,guard=erase_guard) != 'ERASE LOGS':
                        return 0
                    verify_backup(manifest_path.parent,manifest,list_logs(t,erase_guard),identity)
                    erase_guard()
                    t.connection.mav.log_erase_send(*t.target)
                    report.event('fc_logs_erase_requested',backup=str(manifest_path))
                    t.sample(3)
                    if list_logs(t,erase_guard):
                        raise Rejected('Erase sent but FC still reports logs; erasure not confirmed')
                    ui.workflow_status = 'FC log erase confirmed. Local backup retained at '+str(manifest_path.parent)+'. Esc returns.'
                t.connection.mav.log_request_end_send(*t.target)
            ui.view_state = action.upper()+' / COMPLETE'
            ui.show_checks = action == 'diagnostics'
            getattr(ui, 'flush_input', curses.flushinp)()
            while not ui.service_exit and not ui.quit_requested:
                t.pump()
                t.heartbeat()
            return 0
    except Exception as exc:
        ui.view_state = 'MAINTENANCE BLOCKED'
        ui.workflow_status = str(exc)
        report.event('maintenance_blocked',error=str(exc))
        return 3
    finally:
        ui.service_active = False
        ui.service_request = None
        if t is not None:
            try:
                t.connection.mav.log_request_end_send(*t.target)
            finally:
                t.close()
