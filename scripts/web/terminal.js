'use strict';
const $ = id => document.getElementById(id);
const embeddedCodex = location.pathname === '/codex';
const storageKey = embeddedCodex ? 'pi-codex-tab' : 'pi-terminal-tab';
const views = new Map();
let token = '', active = null, busy = false, polling = false, multi = false, epoch = 0, unreachable = false;
let forceTarget = null, pasteTarget = null, resizeTimer;
const modeLabel = mode => mode === 'codex' ? 'Codex' : mode === 'mavproxy' ? 'MAVProxy' : 'Pi terminal';
function stored(key, fallback = null) { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } }
function save(key, value) { try { localStorage.setItem(key, value); } catch {} }
let fontSize = Math.min(24, Math.max(10, Number(stored('pi-terminal-font', '13')) || 13));
const back = new URL(location.href);
back.port = '8070'; back.pathname = '/'; back.search = ''; back.hash = '';
$('back').href = back.href; $('dashboard').href = back.href;
function message(text) { $('message').textContent = text; }
function selected() { return views.get(active); }
async function api(path, data) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
        const response = await fetch(path, data === undefined ? {cache: 'no-store', signal: controller.signal} : {
            method: 'POST', signal: controller.signal,
            headers: {'Content-Type': 'application/json', 'X-Terminal-Token': token}, body: JSON.stringify(data)
        });
        const value = await response.json();
        if (!response.ok) { const error = Error(value.error || 'Terminal request failed'); error.status = response.status; throw error; }
        return value;
    } catch (error) {
        if (error.name === 'AbortError') throw Error('Connection timed out. Check terminal output before repeating a command.');
        throw error;
    } finally { clearTimeout(timeout); }
}
function state() {
    const view = selected(), connected = !!view?.connected && !unreachable;
    for (const id of ['disconnect', 'insert', 'paste']) $(id).disabled = !connected || busy;
    for (const id of ['motor-test', 'force-arm']) $(id).disabled = !connected || busy || view.mode !== 'mavproxy';
    for (const id of ['mavproxy', 'shell', 'codex', 'reconnect']) $(id).disabled = !!view?.connected || busy || unreachable;
    $('restart').disabled = !multi || busy || !view?.mode;
    $('new').disabled = !multi || busy || views.size >= 8;
    $('close').disabled = !multi || busy || !view;
    for (const button of document.querySelectorAll('[data-key]')) button.disabled = !connected || busy;
    $('connection').textContent = unreachable ? 'Website connection lost · retrying…' : connected ? '● SSH connected · ' + modeLabel(view.mode) : 'Disconnected';
    $('font-down').disabled = fontSize <= 10; $('font-up').disabled = fontSize >= 24;
    for (const [id, v] of views) {
        v.tab.setAttribute('aria-selected', String(id === active));
        v.tab.tabIndex = id === active ? 0 : -1; v.tab.disabled = busy;
        v.tab.textContent = modeLabel(v.mode) + ' ' + v.number + (v.connected ? ' ●' : '');
    }
}
async function resize(view = selected()) {
    if (!view || view.id !== active || !view.node.isConnected) return;
    try {
        view.fit.fit();
        const size = `${view.term.cols}x${view.term.rows}`;
        if (view.connected && size !== view.size) {
            await api('/api/resize', {id: view.id, columns: Math.min(300, Math.max(20, view.term.cols)), rows: Math.min(100, Math.max(5, view.term.rows))});
            view.size = size;
        }
    } catch (error) { message(error.message); }
}
function scheduleResize() { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => resize(), 80); }
function input(data, view = selected()) {
    if (!view?.connected || busy || unreachable) return Promise.resolve(false);
    const generation = view.generation;
    // Keep Unicode code points intact and stay below the server's 8192-byte limit.
    const chunks = []; let chunk = '', bytes = 0;
    for (const character of data) {
        const count = new TextEncoder().encode(character).length;
        if (bytes + count > 6000) { chunks.push(chunk); chunk = ''; bytes = 0; }
        chunk += character; bytes += count;
    }
    if (chunk) chunks.push(chunk);
    const pending = view.queue.then(async () => {
        if (view !== views.get(view.id) || generation !== view.generation || !view.connected) throw Error('Terminal session changed; input was not sent.');
        for (const part of chunks) await api('/api/input', {id: view.id, data: part});
        return true;
    });
    view.queue = pending.catch(error => { message(error.message + ' Input is never retried automatically.'); return false; });
    return view.queue;
}
function addView(info) {
    const node = document.createElement('div'); node.className = 'terminal-view'; node.hidden = true;
    node.id = 'view-' + info.id; node.setAttribute('role', 'tabpanel'); $('terminal').append(node);
    const tab = document.createElement('button'); tab.className = 'secondary'; tab.setAttribute('role', 'tab');
    tab.id = 'tab-' + info.id; tab.setAttribute('aria-controls', node.id); node.setAttribute('aria-labelledby', tab.id); $('tabs').append(tab);
    const term = new Terminal({cursorBlink: true, fontSize, fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace', scrollback: 5000,
        theme: {background: '#090f15', foreground: '#d6e7e1', cursor: '#8ae4bf'}});
    const fit = new FitAddon.FitAddon(); term.loadAddon(fit); term.open(node);
    const view = {...info, number: Math.max(0, ...Array.from(views.values(), v => v.number)) + 1,
        node, tab, term, fit, cursor: 0, generation: '', queue: Promise.resolve(), size: null};
    views.set(info.id, view); term.onData(data => input(data, view)); tab.onclick = () => select(info.id);
    tab.onkeydown = event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || busy) return;
        event.preventDefault(); const ids = [...views.keys()], index = ids.indexOf(info.id);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? ids.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + ids.length) % ids.length;
        select(ids[next]); views.get(ids[next]).tab.focus();
    };
    return view;
}
function select(id) {
    active = id; for (const [key, view] of views) view.node.hidden = key !== id;
    save(storageKey, id); $('command-kind').value = selected()?.mode === 'mavproxy' ? 'mavproxy' : 'shell';
    commands(); state(); scheduleResize(); poll();
}
async function refresh() {
    const data = await api('/api/session'); unreachable = false; token = data.token; multi = Array.isArray(data.terminals);
    if (!multi) { data.terminals = [{id: 'legacy', mode: data.mode, connected: data.connected}]; message('Terminal update awaits website restart. Your current session is still available.'); }
    const ids = new Set(data.terminals.map(view => view.id));
    for (const [id, view] of views) if (!ids.has(id)) { view.term.dispose(); view.node.remove(); view.tab.remove(); views.delete(id); }
    for (const info of data.terminals) { const view = views.get(info.id); if (view) Object.assign(view, info); else addView(info); }
    if (embeddedCodex && !views.has(active)) {
        const candidate = data.terminals.find(v => v.mode === 'codex') || data.terminals.find(v => !v.mode && !v.connected);
        if (candidate) active = candidate.id;
        else { const created = await api('/api/new', {}); active = created.id; return refresh(); }
    }
    if (!views.has(active)) active = views.has(stored(storageKey)) ? stored(storageKey) : data.terminals[0]?.id;
    select(active);
}
async function poll() {
    const view = selected(), requestEpoch = epoch;
    if (polling || busy || !token || !view || document.hidden) return;
    polling = true;
    try {
        const data = await api('/api/output?id=' + encodeURIComponent(view.id) + '&cursor=' + view.cursor + '&generation=' + encodeURIComponent(view.generation));
        if (requestEpoch !== epoch || views.get(view.id) !== view) return;
        if (view.generation !== data.generation || view.cursor < data.base) {
            view.term.reset(); view.size = null;
            if (data.base) view.term.writeln('[Older terminal output is no longer available]');
        }
        view.generation = data.generation;
        if (data.output) await new Promise(resolve => view.term.write(Uint8Array.from(atob(data.output), character => character.charCodeAt(0)), resolve));
        unreachable = false; view.cursor = data.cursor; view.connected = data.connected; view.mode = data.mode; state();
        if (view.id === active && !view.size) scheduleResize();
    } catch (error) {
        unreachable = true; state(); message(error.message);
        if (error.status === 403 || error.status === 404) {
            // Reload metadata only; never reconnect SSH or replay operator input.
            try { await refresh(); message('Terminal sessions changed. Sign in again if disconnected.'); } catch (failure) { message(failure.message); }
        }
    } finally { polling = false; }
}
async function action(path, data = {}) {
    if (busy || !selected()) return;
    const view = selected(); busy = true; epoch++; state();
    try {
        await view.queue; await api(path, {id: view.id, ...data}); view.size = null; await refresh();
        message(path === '/api/connect' || path === '/api/restart' ? 'Enter your Pi password when prompted.' : 'Terminal updated.');
    } catch (error) { message(error.message); }
    finally { busy = false; state(); scheduleResize(); if (path === '/api/connect' || path === '/api/restart') selected()?.term.focus(); poll(); }
}
$('new').onclick = async () => {
    if (busy) return; busy = true; epoch++; state();
    try { const data = await api('/api/new', {}); active = data.id; await refresh(); message('Choose Open Pi terminal, Open MAVProxy or Open Codex for this tab.'); }
    catch (error) { message(error.message); } finally { busy = false; state(); }
};
$('shell').onclick = () => action('/api/connect', {mode: 'shell'});
$('mavproxy').onclick = () => action('/api/connect', {mode: 'mavproxy'});
$('codex').onclick = () => action('/api/connect', {mode: 'codex'});
$('reconnect').onclick = () => action('/api/connect', {mode: selected()?.mode || (embeddedCodex ? 'codex' : 'shell')});
$('restart').onclick = () => { if (confirm('Restart this terminal? Its foreground command will be interrupted. This does not land or disarm the drone.')) action('/api/restart'); };
$('disconnect').onclick = () => { if (confirm('Disconnect this terminal? Its foreground command will be interrupted. This does not land or disarm the drone.')) action('/api/disconnect'); };
$('close').onclick = () => { if (confirm('Close this tab and end its SSH session? This does not land or disarm the drone.')) action('/api/remove'); };
const shortcuts = {
    shell: [['Current folder', 'pwd'], ['List files', 'ls -lah'], ['Pi information', 'hostnamectl'], ['Uptime', 'uptime'], ['Disk space', 'df -h'], ['Memory', 'free -h'], ['IP addresses', 'hostname -I'], ['Tailscale status', 'tailscale status'], ['Drone services', 'systemctl --no-pager status mavproxy drone-mavproxy-web drone-web-dashboard drone-video-gallery'], ['Router logs', 'journalctl -u mavproxy -n 50 --no-pager'], ['Open MAVProxy', 'cd /home/user/all_drone && drone_project/pi_flight/.venv/bin/python scripts/web/mavproxy_session.py']],
    mavproxy: [['Vehicle status', 'status'], ['Telemetry links', 'link list'], ['Flight modes', 'mode'], ['Arming check status', 'arm list'], ['Optical flow parameters', 'param show FLOW*'], ['Rangefinder parameters', 'param show RNGFND*'], ['Arm (normal checks)', 'arm throttle', true], ['Disarm', 'disarm', true], ['GUIDED mode', 'mode GUIDED', true], ['Land', 'mode LAND', true], ['Return to launch', 'mode RTL', true]]
};
function commands() {
    $('command').replaceChildren();
    for (const [index, item] of shortcuts[$('command-kind').value].entries()) { const option = document.createElement('option'); option.value = index; option.textContent = item[0]; $('command').append(option); }
}
$('command-kind').onchange = commands;
async function insert(command, view = selected()) {
    if (await input(command, view)) { view.term.focus(); message('Inserted: ' + command + ' — review the prompt, then press Enter to run.'); }
}
$('insert').onclick = () => {
    const item = shortcuts[$('command-kind').value][Number($('command').value)];
    if (item[2] && !confirm('Prepare ' + item[0] + ' in the selected MAVProxy console? Pressing Enter afterwards will send it to the aircraft.')) return;
    insert(item[1]);
};
$('motor-test').onclick = () => {
    const view = selected(); if (!view?.connected || view.mode !== 'mavproxy' || busy) return;
    const motor = Number($('motor').value), throttle = Number($('throttle').value), seconds = Number($('seconds').value);
    if (!Number.isInteger(motor) || motor < 1 || motor > 12 || !Number.isInteger(throttle) || throttle < 1 || throttle > 15 || !Number.isInteger(seconds) || seconds < 1 || seconds > 3) { message('Choose motor 1–12, throttle 1–15%, duration 1–3 seconds.'); return; }
    if (confirm(`Propellers removed, aircraft on the ground, and terminal at a MAVProxy prompt? Prepare motor test ${motor} at ${throttle}% for ${seconds}s. Pressing Enter afterwards spins the motor.`)) insert(`long DO_MOTOR_TEST ${motor} 0 ${throttle} ${seconds} 1 0 0`, view);
};
function validForceTarget() { return forceTarget && selected() === forceTarget.view && forceTarget.view.connected && forceTarget.view.mode === 'mavproxy' && forceTarget.generation === forceTarget.view.generation && !busy && !unreachable; }
function forceReady() { $('force-arm-confirm').disabled = !validForceTarget(); }
$('force-arm').onclick = () => {
    const view = selected(); if (!view?.connected || view.mode !== 'mavproxy' || busy) return;
    forceTarget = {view, generation: view.generation}; forceReady(); $('force-arm-dialog').showModal();
};
$('force-arm-cancel').onclick = () => { forceTarget = null; $('force-arm-dialog').close(); };
$('force-arm-dialog').addEventListener('cancel', () => { forceTarget = null; });
$('force-arm-confirm').onclick = () => {
    forceReady(); if ($('force-arm-confirm').disabled) return;
    const view = forceTarget.view; forceTarget = null; $('force-arm-dialog').close();
    insert('long COMPONENT_ARM_DISARM 1 21196 0 0 0 0 0', view);
};
$('focus-terminal').onclick = () => selected()?.term.focus();
$('clear-screen').onclick = () => { selected()?.term.clear(); selected()?.term.focus(); };
function setFont(change) { fontSize = Math.min(24, Math.max(10, fontSize + change)); save('pi-terminal-font', fontSize); for (const view of views.values()) { view.term.options.fontSize = fontSize; view.size = null; } state(); scheduleResize(); }
$('font-down').onclick = () => setFont(-1); $('font-up').onclick = () => setFont(1);
$('fullscreen').onclick = () => { const expanded = document.querySelector('.terminal-panel').classList.toggle('expanded'); $('fullscreen').textContent = expanded ? 'Collapse' : 'Expand'; scheduleResize(); };
document.addEventListener('keydown', event => { if (event.key === 'Escape' && !event.target.closest('.xterm') && document.querySelector('.terminal-panel').classList.contains('expanded') && !$('paste-dialog').open && !$('force-arm-dialog').open) $('fullscreen').click(); });
$('paste').onclick = () => { pasteTarget = {view: selected(), generation: selected()?.generation}; $('paste-text').value = ''; $('paste-dialog').showModal(); $('paste-text').focus(); };
$('paste-cancel').onclick = () => { pasteTarget = null; $('paste-dialog').close(); };
$('paste-dialog').addEventListener('cancel', () => { pasteTarget = null; });
$('paste-send').onclick = () => {
    if (!pasteTarget || pasteTarget.view !== selected() || !pasteTarget.view.connected || pasteTarget.generation !== pasteTarget.view.generation) { message('Select the original connected terminal before pasting.'); return; }
    const view = pasteTarget.view, text = $('paste-text').value; pasteTarget = null; $('paste-dialog').close(); input(text, view); view.term.focus();
};
const keys = {ctrlc: '\x03', tab: '\t', esc: '\x1b', up: '\x1b[A', down: '\x1b[B', enter: '\r'};
for (const button of document.querySelectorAll('[data-key]')) button.onclick = () => { input(keys[button.dataset.key]); selected()?.term.focus(); };
new ResizeObserver(scheduleResize).observe($('terminal'));
window.visualViewport?.addEventListener('resize', scheduleResize);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { scheduleResize(); poll(); } });
commands(); refresh().catch(error => message(error.message)); setInterval(poll, 250);
