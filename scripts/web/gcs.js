'use strict';
let token='', running=false, busy=false;
const el=id=>document.getElementById(id);
el('dashboard').href=`${location.protocol}//${location.hostname}:8070/`;
async function key(value){
  if(!running || !token) return;
  const response=await fetch('/api/key',{method:'POST',headers:{'Content-Type':'application/json','X-GCS-Token':token},body:JSON.stringify({key:value})});
  if(!response.ok){const body=await response.text();throw new Error(`Control rejected (${response.status}): ${body}`);}
}
function failure(error){el('notice').textContent=error.message;}
document.querySelectorAll('[data-key]').forEach(button=>button.onclick=()=>key(button.dataset.key).catch(failure));
const keys={ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right',PageUp:'pageup',PageDown:'pagedown',Home:'home',Enter:'enter',Escape:'escape',Backspace:'backspace'};
el('screen').addEventListener('keydown',event=>{if(event.ctrlKey||event.metaKey||event.altKey||event.repeat)return;const value=keys[event.key]||(event.key.length===1?event.key:null);if(value){event.preventDefault();key(value).catch(failure);}});
el('answer-form').onsubmit=async event=>{event.preventDefault();if(busy)return;busy=true;try{for(const char of el('answer').value)await key(char);await key('enter');el('answer').value='';}catch(error){failure(error);}finally{busy=false;}};
el('fullscreen').onclick=()=>document.querySelector('.console').requestFullscreen().catch(failure);
async function refresh(){
  try{
    const response=await fetch('/api/state',{cache:'no-store',signal:AbortSignal.timeout(4000)});
    if(!response.ok)throw new Error('GCS unavailable');
    const data=await response.json();token=data.token;running=data.running;
    el('screen').textContent=data.lines.join('\n');el('state').textContent=data.state;
    el('connection').textContent=running?'● Connected':'● Session ended';
    el('prompt').textContent=data.prompt||'No confirmation requested.';
    el('notice').textContent=data.error||data.notice;
    document.querySelectorAll('[data-key]').forEach(button=>button.disabled=!running);
    el('answer').disabled=!running||!data.prompt;
    el('answer-form').querySelector('button').disabled=!running||!data.prompt;
  }catch(error){running=false;el('connection').textContent='● Connection lost';failure(error);}
  setTimeout(refresh,250);
}
refresh();
