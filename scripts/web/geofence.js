'use strict';
const $=id=>document.getElementById(id);
let token='',telemetry={},job={},revision=0,dirty=true,polling=false,posting=false,centered=false,uploadDraft=null,appliedResult=null,returnPoint=null,pendingJob=null,pendingRevision=null;
const map=L.map('map',{preferCanvas:true,maxZoom:22}).setView([20,0],2);
map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
const tiles=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxNativeZoom:19,maxZoom:22,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
let tileErrors=0;tiles.on('tileerror',()=>{if(++tileErrors===3)$('map-note').textContent='Map tiles are unavailable. You can still draw, edit and enter coordinates. Try again when internet access returns.'});
L.control.scale({imperial:false}).addTo(map);
const boundaries=L.featureGroup().addTo(map);
const drone=L.circleMarker([0,0],{radius:7,color:'#fff',fillColor:'#167cff',fillOpacity:1,pmIgnore:true}).bindTooltip('Aircraft');
const homeMarker=L.circleMarker([0,0],{radius:6,color:'#fff',fillColor:'#f5b649',fillOpacity:1,pmIgnore:true});
let returnMarker=null;
function message(text){$('message').textContent=text}
function element(tag,text,cls=''){const node=document.createElement(tag);node.textContent=text;node.className=cls;return node}
let settingsSchema={},settingsDraft={},settingsOriginal={},settingsReview=null,shownResult=null;
function busy(){return posting||job.state==='running'}
function snapshot(){const plan={version:1,fences:boundaries.getLayers().map(layer=>layer instanceof L.Circle?{kind:'circle',inclusion:layer.fenceInclusion,center:[layer.getLatLng().lat,layer.getLatLng().lng],radius:layer.getRadius()}:{kind:'polygon',inclusion:layer.fenceInclusion,vertices:layer.getLatLngs()[0].map(p=>[p.lat,p.lng])})};if(returnPoint)plan.return_point=returnPoint;return plan}
function saveDraft(){try{localStorage.setItem('drone-geofence-draft',JSON.stringify(snapshot()))}catch{}}
function markDirty(){revision++;dirty=true;saveDraft();render()}
function finishEditing(){map.pm.disableDraw();for(const layer of boundaries.getLayers())layer.pm.disable()}
function style(layer){layer.setStyle({color:layer.fenceInclusion?'#17a76a':'#d84b4b',fillOpacity:.14,weight:3})}
function addFence(fence,attach=true){let layer;if(fence.kind==='circle')layer=L.circle(fence.center,{radius:fence.radius});else if(fence.kind==='polygon')layer=L.polygon(fence.vertices);else throw Error('Unknown boundary type');layer.fenceInclusion=fence.inclusion;style(layer);if(attach)boundaries.addLayer(layer);layer.on('pm:edit',markDirty);layer.on('pm:dragend',markDirty);return layer}
function loadPlan(plan,fromController=false){if(!plan||!Array.isArray(plan.fences)||plan.fences.length>100)throw Error('Invalid fence plan');const fresh=plan.fences.map(fence=>addFence(fence,false));if(plan.return_point&&(!Array.isArray(plan.return_point)||plan.return_point.length!==2||!plan.return_point.every(Number.isFinite)))throw Error('Invalid return point');finishEditing();boundaries.clearLayers();for(const layer of fresh)boundaries.addLayer(layer);returnPoint=plan.return_point||null;if(returnMarker){map.removeLayer(returnMarker);returnMarker=null}if(returnPoint)returnMarker=L.circleMarker(returnPoint,{radius:6,color:'#ad68ff',pmIgnore:true}).bindTooltip('Fence return point').addTo(map);revision++;dirty=!fromController;saveDraft();render();if(boundaries.getLayers().length)map.fitBounds(boundaries.getBounds(),{padding:[30,30],maxZoom:20})}
function render(){
 const active=busy(),connected=telemetry.connected===true,supported=telemetry.fence_supported===true,disarmed=telemetry.armed===false;
 $('connection').textContent=connected?(telemetry.armed?'Aircraft armed':'Connected to aircraft'):'Telemetry unavailable';
 for(const id of ['polygon','circle','add-circle','clear','import','enable-after'])$(id).disabled=active;
 $('read').disabled=active||!connected||!supported;
 $('upload').disabled=active||!connected||!supported||!disarmed||!boundaries.getLayers().length||telemetry.settings?.FENCE_ENABLE!==0||($('enable-after').checked&&(!telemetry.position||telemetry.gps_fix<3));
 $('enable').disabled=active||!connected||!supported||!disarmed||telemetry.settings?.FENCE_ENABLE!==0||!telemetry.position||telemetry.gps_fix<3;
 $('disable').disabled=active||!connected||!supported||!disarmed||telemetry.settings?.FENCE_ENABLE!==1;
 $('center-drone').disabled=!telemetry.position;$('center-home').disabled=!telemetry.home;
 $('draft-state').textContent=dirty?'Local changes':'Matches verified controller readback';$('draft-state').classList.toggle('draft-warning',dirty);
 const position=telemetry.position;
 $('coordinates').textContent=position?`${position.lat.toFixed(7)}, ${position.lon.toFixed(7)}\nRelative altitude: ${position.relative_alt_m.toFixed(2)} m`:'Waiting for a fresh GPS position.';
 $('gps').textContent=`GPS fix: ${telemetry.gps_fix??'unknown'} · satellites: ${telemetry.satellites??'unknown'}${telemetry.fence_supported===false?' · Fence protocol unsupported':telemetry.fence_supported==null?' · Checking fence support':''}`;
 $('home').textContent=telemetry.home?`Home: ${telemetry.home.lat.toFixed(7)}, ${telemetry.home.lon.toFixed(7)}`:'Home position unavailable';
 const settings=telemetry.settings||{};
 $('fence-settings').textContent=`Saved fence enable: ${settings.FENCE_ENABLE===1?'enabled':settings.FENCE_ENABLE===0?'disabled':'unknown'} · type ${settings.FENCE_TYPE??'?'} · breach action ${settings.FENCE_ACTION??'?'} · margin ${settings.FENCE_MARGIN??'?'} m · max altitude ${settings.FENCE_ALT_MAX??'?'} m. Runtime geofence: ${telemetry.runtime_fence?(telemetry.runtime_fence.enabled?'active':'inactive')+(telemetry.runtime_fence.healthy?' · healthy':' · breach / unhealthy'):'unreported'}.`;
 renderSettings();
 $('empty').hidden=boundaries.getLayers().length>0;
 // Keep inputs stable while polling. Rebuild the list only when geometry/state changes.
 const signature=JSON.stringify({plan:snapshot(),active});
 if($('boundaries').dataset.signature===signature)return;
 $('boundaries').dataset.signature=signature;$('boundaries').replaceChildren();
 for(const [index,layer] of boundaries.getLayers().entries()){
  const card=element('div','',`boundary${layer.fenceInclusion?'':' exclusion'}`);card.append(element('strong',`${index+1}. ${layer instanceof L.Circle?'Circle':'Polygon'}`));
  const label=element('label',''),check=document.createElement('input');check.type='checkbox';check.checked=layer.fenceInclusion;check.disabled=active;check.onchange=()=>{layer.fenceInclusion=check.checked;style(layer);markDirty()};label.append(check,document.createTextNode('Inclusion zone (stay inside)'));card.append(label);
  if(layer instanceof L.Circle){const radius=document.createElement('input');radius.type='number';radius.min='1';radius.max='10000';radius.step='.1';radius.value=layer.getRadius().toFixed(2);radius.disabled=active;radius.onchange=()=>{const value=Number(radius.value);if(!Number.isFinite(value)||value<1||value>10000){message('Radius must be 1–10000 metres.');return}layer.setRadius(value);markDirty()};const row=element('label','Radius (m)');row.append(radius);card.append(row)}else card.append(element('p',`${layer.getLatLngs()[0].length} vertices`,'description'));
  const controls=element('div','','actions');for(const [name,action] of [['Edit',()=>{finishEditing();layer.pm.enable({allowSelfIntersection:false,draggable:true,removeLayerBelowMinVertexCount:false});message('Drag handles to edit. Select Finish / cancel editing when done.')}],['Center',()=>map.fitBounds(layer.getBounds(),{padding:[30,30],maxZoom:21})],['Delete',()=>{layer.pm.disable();boundaries.removeLayer(layer);markDirty()}]]){const button=element('button',name,'secondary');button.disabled=active;button.onclick=action;controls.append(button)}card.append(controls);$('boundaries').append(card)
 }
}
map.on('pm:create',event=>{const layer=event.layer;if(!(layer instanceof L.Circle)&&!(layer instanceof L.Polygon)){map.removeLayer(layer);return}layer.fenceInclusion=true;style(layer);boundaries.addLayer(layer);layer.on('pm:edit',markDirty);layer.on('pm:dragend',markDirty);map.pm.disableDraw();markDirty();message('Boundary added to the local drawing. Review it before uploading.')});
$('polygon').onclick=()=>{finishEditing();map.pm.enableDraw('Polygon',{allowSelfIntersection:false,snappable:true});message('Tap the polygon vertices. Tap the first vertex to finish.')};
$('circle').onclick=()=>{finishEditing();map.pm.enableDraw('Circle',{snappable:false});message('Tap the circle center, then its edge.')};
$('cancel-draw').onclick=finishEditing;
$('center-drone').onclick=()=>{if(telemetry.position)map.setView([telemetry.position.lat,telemetry.position.lon],20)};
$('center-home').onclick=()=>{if(telemetry.home)map.setView([telemetry.home.lat,telemetry.home.lon],20)};
$('add-circle').onclick=()=>{if(busy())return;const lat=Number($('latitude').value),lon=Number($('longitude').value),radius=Number($('radius').value);if(!$('latitude').value||!$('longitude').value||![lat,lon,radius].every(Number.isFinite)||lat < -85||lat>85||lon < -180||lon>180||radius<1||radius>10000){message('Enter latitude −85…85, longitude −180…180, and radius 1–10000 m.');return}const layer=addFence({kind:'circle',inclusion:true,center:[lat,lon],radius});markDirty();map.fitBounds(layer.getBounds(),{padding:[25,25],maxZoom:21})};
$('clear').onclick=()=>{if(!busy()&&confirm('Clear the local drawing? This does not erase the controller fence.'))loadPlan({fences:[]})};
async function api(path,data){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),10000);try{const response=await fetch(path,data===undefined?{cache:'no-store',signal:controller.signal}:{method:'POST',signal:controller.signal,headers:{'Content-Type':'application/json','X-Dashboard-Token':token},body:JSON.stringify(data)});const value=await response.json();if(!response.ok)throw Error(value.error||'Geofence request failed');return value}finally{clearTimeout(timer)}}
async function operation(name,data={}){if(busy())return;finishEditing();posting=true;render();try{const accepted=await api('/api/geofence/'+name,data);pendingJob=accepted.job_id;pendingRevision=revision;job={id:accepted.job_id,state:'running',operation:name};message('Fence operation started. Do not start another fence transfer from a different GCS.')}catch(error){message(error.message)}finally{posting=false;render();poll()}}
$('read').onclick=()=>{if(dirty&&boundaries.getLayers().length&&!confirm('Replace your local drawing with the controller fence?'))return;operation('read')};
$('upload').onclick=()=>{finishEditing();uploadDraft={plan:JSON.parse(JSON.stringify(snapshot())),enable_after:$('enable-after').checked,revision};const items=uploadDraft.plan.fences.reduce((sum,f)=>sum+(f.kind==='circle'?1:f.vertices.length),0)+(returnPoint?1:0);$('upload-summary').textContent=`${uploadDraft.plan.fences.length} boundaries · ${items} MAVLink fence items. Enforcement will ${uploadDraft.enable_after?'be enabled after verification':'remain disabled'}.`;if(telemetry.position)$('upload-summary').textContent+=` Current position: ${telemetry.position.lat.toFixed(7)}, ${telemetry.position.lon.toFixed(7)}.`;$('upload-dialog').showModal()};
$('upload-cancel').onclick=()=>{uploadDraft=null;$('upload-dialog').close()};$('upload-dialog').addEventListener('cancel',()=>{uploadDraft=null});
$('upload-confirm').onclick=()=>{if(!uploadDraft)return;const data=uploadDraft;uploadDraft=null;$('upload-dialog').close();if(data.revision!==revision){message('Drawing changed after review. Review the fence again before uploading.');return}operation('upload',{plan:data.plan,enable_after:data.enable_after,confirmed:true})};
$('enable-after').onchange=render;
$('enable').onclick=()=>{if(confirm('Enable the fence currently stored on the controller? The aircraft must have a fresh 3D GPS fix and be inside the permitted area. Existing breach actions remain in effect.'))operation('enable',{confirmed:true})};
$('disable').onclick=()=>{if(confirm('Disable fence enforcement on the controller? Stored boundaries will remain available.'))operation('disable',{confirmed:true})};
function clockwise(points){const copy=points.map(p=>[...p]);const area=copy.reduce((sum,p,i)=>sum+p[1]*copy[(i+1)%copy.length][0]-copy[(i+1)%copy.length][1]*p[0],0);if(area>0)copy.reverse();return copy}
function qgcPlan(plan){return {fileType:'Plan',version:1,groundStation:'QGroundControl',geoFence:{version:2,circles:plan.fences.filter(f=>f.kind==='circle').map(f=>({version:1,inclusion:f.inclusion,circle:{center:f.center,radius:f.radius}})),polygons:plan.fences.filter(f=>f.kind==='polygon').map(f=>({version:1,inclusion:f.inclusion,polygon:clockwise(f.vertices)}))},mission:{version:2,items:[],firmwareType:3,vehicleType:2,cruiseSpeed:15,hoverSpeed:5,plannedHomePosition:[telemetry.home?.lat??0,telemetry.home?.lon??0,0]},rallyPoints:{version:2,points:[]}}}
$('export').onclick=()=>{const plan=snapshot(),native=!!plan.return_point,blob=new Blob([JSON.stringify(native?plan:qgcPlan(plan),null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=native?'geofence.json':'geofence.plan';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message(native?'Exported JSON preserving the controller return point.':'Exported QGC-format fence plan. Mission and rally lists in this file are empty.')};
$('import').onchange=async()=>{const file=$('import').files[0];if(!file||busy())return;try{if(file.size>262144)throw Error('Fence file must be smaller than 256 KB');const data=JSON.parse(await file.text());let plan=data;if(data.geoFence){const g=data.geoFence;if(g.version!==2)throw Error('Supported QGC fence version is 2');plan={version:1,fences:[...(g.circles||[]).map(f=>({kind:'circle',inclusion:f.inclusion,center:f.circle.center,radius:f.circle.radius})),...(g.polygons||[]).map(f=>({kind:'polygon',inclusion:f.inclusion,vertices:f.polygon}))]};if(g.breachReturnPoint)plan.return_point=g.breachReturnPoint}if(!plan||!Array.isArray(plan.fences)||plan.fences.length>100)throw Error('Invalid fence file');for(const f of plan.fences){if(typeof f.inclusion!=='boolean')throw Error('Fence inclusion must be true or false');const points=f.kind==='circle'?[f.center]:f.vertices;if(!Array.isArray(points)||points.length>100||points.some(p=>!Array.isArray(p)||p.length!==2||!p.every(Number.isFinite)||p[0]<-85||p[0]>85||p[1]<-180||p[1]>180))throw Error('Invalid fence coordinates');if(f.kind==='circle'&&(!Number.isFinite(f.radius)||f.radius<1||f.radius>10000))throw Error('Invalid circle radius')}loadPlan(plan);message('Imported boundaries into the local drawing. Upload is a separate action.')}catch(error){message(error.message)}finally{$('import').value=''}};
async function poll(){if(polling)return;polling=true;try{const data=await api('/api/geofence');settingsSchema=data.settings_schema||settingsSchema;token=data.token;telemetry=data.telemetry;job=data.job;if(job.settings&&!(telemetry.settings_observed_at>=job.finished_at)){telemetry.settings={...telemetry.settings,...job.settings}}if(telemetry.position){drone.setLatLng([telemetry.position.lat,telemetry.position.lon]).addTo(map);drone.setStyle({opacity:1,fillOpacity:1});if(!centered){map.setView(drone.getLatLng(),19);centered=true}if(document.activeElement!==$('latitude')&&document.activeElement!==$('longitude')&&!$('latitude').value){$('latitude').value=telemetry.position.lat.toFixed(7);$('longitude').value=telemetry.position.lon.toFixed(7)}}else if(map.hasLayer(drone))drone.setStyle({opacity:.4,fillOpacity:.3});if(telemetry.home)homeMarker.setLatLng([telemetry.home.lat,telemetry.home.lon]).bindTooltip('Home').addTo(map).bringToFront();if(telemetry.position)drone.bringToFront();const result=JSON.stringify(job);if(job.state==='complete'&&result!==appliedResult){appliedResult=result;if(job.plan&&['read','upload'].includes(job.operation)&&pendingJob===job.id&&pendingRevision===revision)loadPlan(job.plan,true);if(pendingJob===job.id)showResult(job);pendingJob=null;message(job.message+' · Saved fence enable '+(job.settings?.FENCE_ENABLE===1?'enabled':job.settings?.FENCE_ENABLE===0?'disabled':'unknown'))}else if(job.state==='running')message(job.message);else if(job.state==='error'){message(job.error||job.message);if(pendingJob===job.id){showResult(job);pendingJob=null}};render()}catch(error){telemetry={connected:false};message(error.message+' Drawing is still available.');render()}finally{polling=false}}
function changesForSettings(){const changes={},expected={};for(const name of Object.keys(settingsDraft)){const value=settingsDraft[name];if(value!==settingsOriginal[name]){changes[name]=value;expected[name]=settingsOriginal[name]}}return {changes,expected}}
function renderSettings(){
 const host=$('safety-fields'),values=telemetry.settings||{},locked=busy()||!telemetry.connected||telemetry.armed!==false||values.FENCE_ENABLE!==0;
 for(const [name,spec] of Object.entries(settingsSchema)){
  let row=document.getElementById('field-'+name);
  if(!row){row=element('div','','setting-row');row.id='field-'+name;row.append(element('label',spec.label+(spec.unit?' ('+spec.unit+')':'')),element('code',name));
   let input;
   if(spec.choices?.bits){input=element('div','','setting-bits');for(const [bit,label] of Object.entries(spec.choices.bits)){const check=document.createElement('input');check.type='checkbox';check.value=bit;const line=element('label','');line.append(check,document.createTextNode(label));input.append(line)}input.addEventListener('change',()=>{settingsDraft[name]=[...input.querySelectorAll('input:checked')].reduce((n,c)=>n|Number(c.value),0);renderSettings()})}
   else {input=document.createElement(spec.choices?'select':'input');if(spec.choices){for(const [value,label] of Object.entries(spec.choices)){const option=element('option',label);option.value=value;input.append(option)}}else{input.type='number';input.min=spec.min;input.max=spec.max;input.step='any'}input.addEventListener('input',()=>{settingsDraft[name]=input.value===''?NaN:Number(input.value);renderSettings()});input.id='input-'+name;row.querySelector('label').htmlFor=input.id}
   input.classList.add('setting-input');row.append(input,element('p',spec.help||'','description'),element('span','','setting-live'));host.append(row)
  }
  const supported=Number.isFinite(values[name]),input=row.querySelector('.setting-input');
  if(supported&&!(name in settingsDraft)){settingsOriginal[name]=values[name];settingsDraft[name]=values[name]}
  if(supported&&settingsDraft[name]===settingsOriginal[name]&&document.activeElement!==input){settingsOriginal[name]=values[name];settingsDraft[name]=values[name]}
  if(spec.choices?.bits){for(const check of input.querySelectorAll('input')){check.disabled=locked||!supported;check.checked=!!(settingsDraft[name]&Number(check.value))}}
  else {input.disabled=locked||!supported;if(document.activeElement!==input)input.value=supported?settingsDraft[name]:''}
  row.querySelector('.setting-live').textContent=supported?'Controller: '+Number(values[name].toFixed(5))+(spec.unit?' '+spec.unit:''):'Waiting for parameter / unsupported';
 }
 const {changes}=changesForSettings();$('settings-save').disabled=locked||!Object.keys(changes).length||Object.values(changes).some(v=>!Number.isFinite(v));$('settings-reset').disabled=busy();
 $('settings-note').textContent=locked?'Saving requires connection, disarmed aircraft and disabled fence enforcement.':Object.keys(changes).length+' unsaved changes. Saving does not enable the fence.';
}
$('settings-reset').onclick=()=>{settingsDraft={};settingsOriginal={};renderSettings()};
$('settings-save').onclick=()=>{settingsReview=changesForSettings();$('settings-review').textContent=Object.entries(settingsReview.changes).map(([name,value])=>name+': '+settingsReview.expected[name]+' → '+value+' '+(settingsSchema[name].unit||'')).join('\n');$('settings-dialog').showModal()};
$('settings-cancel').onclick=()=>{$('settings-dialog').close();settingsReview=null};
$('settings-confirm').onclick=()=>{const review=settingsReview;settingsReview=null;$('settings-dialog').close();if(review)operation('settings',{...review,confirmed:true})};
function showResult(result){
 if(shownResult===result.id||!['upload','settings','enable','disable'].includes(result.operation))return;shownResult=result.id;
 const success=result.state==='complete';$('result-title').textContent=success?(result.operation==='upload'?'Fence upload complete':result.operation==='settings'?'Settings saved and verified':'Enforcement change verified'):'Controller operation needs attention';
 let text=success?result.message:result.error;
 if(result.operation==='upload'&&success)text='The controller accepted the fence and its downloaded boundaries match your upload.';
 if(result.upload_verified&&!success)text+=' Boundaries are uploaded and verified.';
 const enabled=result.settings?.FENCE_ENABLE; text+=' Saved fence enable: '+(enabled===1?'enabled':enabled===0?'disabled':'unknown; read controller state')+'.';
 if(result.applied&&Object.keys(result.applied).length)text+=' Verified settings: '+Object.keys(result.applied).join(', ')+'.';
 if(result.backup)text+=' Backup: '+result.backup+'.';
 if(result.operation==='settings'&&success){settingsDraft={};settingsOriginal={}};
 $('result-text').textContent=text;$('result-dialog').showModal();
}
$('result-close').onclick=()=>$('result-dialog').close();
try{const saved=localStorage.getItem('drone-geofence-draft');if(saved)loadPlan(JSON.parse(saved))}catch{}
new ResizeObserver(()=>map.invalidateSize()).observe($('map'));render();poll();setInterval(poll,1000);
