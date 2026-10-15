const $=id=>document.getElementById(id);let token='',ticket=null,busy=false,active=false,polling=false;
const videoURL=new URL(location.href);videoURL.port='8080';videoURL.pathname='/';videoURL.search='';videoURL.hash='';$('video-link').href=videoURL.href;
async function api(path,data){const response=await fetch(path,data===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Token':token},body:JSON.stringify(data)});const value=await response.json();if(!response.ok)throw Error(value.error||'Request failed');return value}
async function refresh(){if(polling)return;polling=true;try{
 const data=await api('/api/scripts');token=data.token;const run=data.run;active=['starting','running','stopping'].includes(run.state);
 $('run-state').textContent=run.state;$('review').disabled=busy||active||run.state==='interrupted';$('stop').disabled=busy||!active||run.state==='stopping';$('record-video').disabled=busy||active;
 const output=$('output'),nearBottom=output.scrollTop+output.clientHeight>=output.scrollHeight-50;const text=run.lines.length?run.lines.join('\n'):'No output yet.';
 if(output.textContent!==text){output.textContent=text;if(nearBottom)output.scrollTop=output.scrollHeight}
 $('run-meta').textContent=run.run_id?`${run.message||''}${run.exit_code!==null&&run.exit_code!==undefined?' · Exit '+run.exit_code:''}`:'Choose Review & run script to prepare a flight.';
}catch(error){$('message').textContent=error.message;$('review').disabled=true;$('stop').disabled=true}finally{polling=false}}
$('review').onclick=async()=>{if(busy||active)return;busy=true;$('review').disabled=true;$('message').textContent='';try{
 const data=await api('/api/scripts/prepare',{script:'flight_2m',record_video:$('record-video').checked});ticket=data.ticket;
 $('flight-summary').textContent=data.sequence+' · Video '+(data.record_video?'enabled':'disabled');$('ready').checked=false;$('launch').disabled=true;$('confirm-dialog').showModal();
}catch(error){$('message').textContent=error.message}finally{busy=false;refresh()}};
$('ready').onchange=()=>{$('launch').disabled=!$('ready').checked||busy};
$('cancel').onclick=()=>{ticket=null;$('confirm-dialog').close()};$('confirm-dialog').addEventListener('cancel',()=>{ticket=null});
$('launch').onclick=async()=>{if(!ticket||!$('ready').checked||busy)return;busy=true;$('launch').disabled=true;const approved=ticket;ticket=null;try{
 await api('/api/scripts/run',{ticket:approved,confirmed:true});$('message').textContent='Launch accepted. Follow the script output.';
}catch(error){$('message').textContent=error.message+' Refresh status before preparing another run.'}finally{$('confirm-dialog').close();busy=false;refresh()}};
$('stop').onclick=async()=>{if(busy||!active||!confirm('Interrupt this flight script and request LAND if it still owns GUIDED?'))return;busy=true;$('stop').disabled=true;try{await api('/api/scripts/stop',{});$('message').textContent='Stop requested. Watch output until the aircraft is safe.'}catch(error){$('message').textContent=error.message}finally{busy=false;refresh()}};
$('refresh').onclick=refresh;refresh();setInterval(refresh,1500);

const codexURL=new URL(location.href);codexURL.port='8071';codexURL.pathname='/codex';codexURL.search='';codexURL.hash='';$('codex-terminal').src=codexURL.href;
const terminalURL=new URL(codexURL);terminalURL.pathname='/';$('codex-full').href=terminalURL.href;
