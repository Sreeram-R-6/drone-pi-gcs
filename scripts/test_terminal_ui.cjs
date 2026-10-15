/* Frontend interaction tests with an in-memory DOM and inert API. No SSH/MAVLink.
   Run: node scripts/test_terminal_ui.cjs
   Real Chromium layout tests are in test_terminal_browser.py. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/web/terminal.js', 'utf8');
class Element {
    constructor() { this.value=''; this.checked=false; this.disabled=false; this.hidden=false; this.isConnected=true; this.textContent=''; this.children=[]; this.listeners={}; this.classes=new Set(); this.open=false;
        this.classList={toggle:name=>{if(this.classes.has(name)){this.classes.delete(name);return false}this.classes.add(name);return true},contains:name=>this.classes.has(name)}; }
    append(node) { this.children.push(node); if (this.id==='command' && this.children.length===1) this.value=String(node.value); }
    replaceChildren() { this.children=[]; }
    setAttribute() {} focus() {} closest() { return null; }
    remove() { this.isConnected=false; }
    addEventListener(name,fn) { this.listeners[name]=fn; }
    showModal() { this.open=true; }
    close() { this.open=false; }
    click() { if(!this.disabled) return this.onclick?.(); }
}
class Term {
    constructor(options) { this.options=options;this.cols=80;this.rows=24;this.output=''; }
    loadAddon() {} open() {} focus() {} reset() {this.output=''} clear() {this.output=''} dispose() {}
    onData(fn) {this.ondata=fn}
    write(bytes,done) {this.output+=Buffer.from(bytes).toString();if(done)queueMicrotask(done)}
    writeln(text) {this.output+=text+'\n'}
}
async function setup(path='/',blockedStorage=false) {
    const elements=new Map();const get=id=>{if(!elements.has(id)){const e=new Element();e.id=id;elements.set(id,e)}return elements.get(id)};
    get('command-kind').value='shell';get('motor').value='1';get('throttle').value='5';get('seconds').value='2';
    const panel=new Element();const keys=['ctrlc','tab','esc','up','down','enter'].map(key=>{const e=new Element();e.dataset={key};return e});
    const terminals=new Map([['t1',{id:'t1',mode:null,connected:false,generation:'g0'}]]),requests=[],storage=new Map();let serial=1;
    const model={terminals,requests,inputFailure:false,outputFailure:false,confirmed:true};
    const document={hidden:false,getElementById:get,createElement:()=>new Element(),querySelector:()=>panel,querySelectorAll:()=>keys,addEventListener(){}};
    const context=vm.createContext({document,window:{},location:{pathname:path,href:'http://localhost:8071'+path},URL,TextEncoder,Uint8Array,AbortController,console,
        setTimeout,clearTimeout,setInterval(){},ResizeObserver:class{observe(){}},Terminal:Term,FitAddon:{FitAddon:class{fit(){}}},
        localStorage:{getItem(key){if(blockedStorage)throw Error('blocked');return storage.get(key)??null},setItem(key,value){if(blockedStorage)throw Error('blocked');storage.set(key,value)}},
        confirm:()=>model.confirmed,atob:s=>Buffer.from(s,'base64').toString('latin1'),
        fetch:async(path,options={})=>{
            const data=options.body?JSON.parse(options.body):null;requests.push({path,data});let result={},status=200;
            const terminal=data?terminals.get(data.id):null;
            if(path==='/api/session')result={token:'token',terminals:[...terminals.values()].map(t=>({id:t.id,mode:t.mode,connected:t.connected}))};
            else if(path.startsWith('/api/output?')){const t=terminals.get(new URL('http://test'+path).searchParams.get('id'));if(model.outputFailure){status=503;result={error:'offline'}}else result={...t,cursor:0,base:0,output:''};}
            else if(path==='/api/new'){const id='t'+(++serial);terminals.set(id,{id,mode:null,connected:false,generation:'g0'});result={id};}
            else if(path==='/api/connect'||path==='/api/restart'){terminal.connected=true;terminal.mode=data.mode||terminal.mode;terminal.generation='g'+(++serial)}
            else if(path==='/api/disconnect')terminal.connected=false;
            else if(path==='/api/remove')terminals.delete(data.id);
            else if(path==='/api/input'&&model.inputFailure){status=503;result={error:'input offline'}}
            return {ok:status===200,status,json:async()=>result};
        }});
    vm.runInContext(source,context);
    const run=code=>vm.runInContext(code,context);
    const settle=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve))};
    await settle();
    const connect=async(mode)=>{await get(mode).click();await run('poll()');await settle()};
    return {get,keys,panel,context,model,run,settle,connect};
}
async function main() {
    let app=await setup();await app.connect('mavproxy');
    app.get('force-arm').click();assert(!app.get('force-arm-confirm').disabled);
    app.get('force-arm-confirm').click();await app.settle();
    assert.deepEqual(app.model.requests.filter(r=>r.path==='/api/input').map(r=>r.data.data),['long COMPONENT_ARM_DISARM 1 21196 0 0 0 0 0']);
    app.get('force-arm').click();app.get('force-arm-cancel').click();assert.equal(app.model.requests.filter(r=>r.path==='/api/input').length,1);
    app.get('throttle').value='100';app.get('motor-test').click();assert.equal(app.model.requests.filter(r=>r.path==='/api/input').length,1);
    app.get('throttle').value='5';app.get('motor-test').click();await app.settle();
    assert.equal(app.model.requests.filter(r=>r.path==='/api/input').at(-1).data.data,'long DO_MOTOR_TEST 1 0 5 2 1 0 0');
    app.get('force-arm').click();
    app.run("selected().generation='changed'");app.get('force-arm-confirm').click();assert(app.get('force-arm-confirm').disabled);
    console.log('PASS force-arm confirmation, cancellation, stale session, motor limits, no automatic Enter');
    app=await setup();await app.connect('shell');const first=app.run('active');
    await app.get('new').click();await app.connect('codex');assert.equal(app.model.terminals.size,2);assert(app.model.terminals.get(first).connected);
    const second=app.run('active'),oldGeneration=app.model.terminals.get(second).generation;
    app.get('restart').click();await app.settle();await app.run('poll()');assert.notEqual(app.model.terminals.get(second).generation,oldGeneration);
    app.get('disconnect').click();await app.settle();await app.run('poll()');assert(!app.model.terminals.get(second).connected);
    await app.get('reconnect').click();await app.run('poll()');assert(app.model.terminals.get(second).connected);
    app.get('close').click();await app.settle();assert.equal(app.model.terminals.size,1);assert(app.model.terminals.get(first).connected);
    console.log('PASS independent tabs, restart, disconnect, reconnect and close');
    const text='α中🚁'.repeat(2000);app.get('paste').click();app.get('paste-text').value=text;app.get('paste-send').click();await app.settle();
    const parts=app.model.requests.filter(r=>r.path==='/api/input').map(r=>r.data.data);assert.equal(parts.join(''),text);assert(parts.every(p=>Buffer.byteLength(p)<=8192));
    const oldFont=app.run('fontSize');app.get('font-up').click();assert.equal(app.run('fontSize'),oldFont+1);app.get('fullscreen').click();assert(app.panel.classList.contains('expanded'));
    console.log('PASS Unicode paste chunks, fonts and expand controls');
    app.model.outputFailure=true;await app.run('poll()');assert(app.run('unreachable'));assert(app.get('paste').disabled);
    app.model.outputFailure=false;await app.run('poll()');assert(!app.run('unreachable'));assert(!app.get('paste').disabled);
    const prior=app.model.requests.filter(r=>r.path==='/api/input').length;app.model.inputFailure=true;await app.run("input('a')");await app.settle();
    assert(app.get('message').textContent.includes('never retried'));assert.equal(app.model.requests.filter(r=>r.path==='/api/input').length,prior+1);
    console.log('PASS connection recovery and failed input is never replayed');
    app=await setup('/codex',true);await app.connect('codex');assert.equal(app.run('selected().mode'),'codex');assert.equal(app.model.terminals.size,1);
    console.log('PASS Codex view with browser storage blocked');
}
main().catch(error=>{console.error(error);process.exitCode=1});
