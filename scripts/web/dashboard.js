const $=id=>document.getElementById(id);
function element(tag,cls,text){const item=document.createElement(tag);item.className=cls;item.textContent=text;return item}
const videoURL=new URL(window.location.href);videoURL.port='8080';videoURL.pathname='/';videoURL.search='';videoURL.hash='';
for(const link of document.querySelectorAll('[data-video-link]'))link.href=videoURL.href;
async function refresh(){
 $('refresh').disabled=true;
 try{
  const response=await fetch('/api/sites',{cache:'no-store'});if(!response.ok)throw Error('Service status is temporarily unavailable.');
  const data=await response.json();$('sites').replaceChildren();let online=0;
  for(const site of data.sites){
   online+=Number(site.online);const card=element('article','card','');const top=element('div','card-top','');
   top.append(element('span','icon',site.icon||'↗'),element('span','status'+(site.online?' online':''),site.online?'● Online':'○ Offline'));
   const link=element('a','open','Open website ↗');const url=new URL(site.url);url.hostname=location.hostname;link.href=url.href;
   card.append(top,element('h3','',site.name),element('p','',site.description),element('div','boot',`Startup ${site.startup} · ${site.state}`),link);$('sites').append(card);
  }
  $('online-count').textContent=`${online} of ${data.sites.length} online`;$('support').replaceChildren();
  for(const service of data.support_services){const item=element('div','support-item','');const name=element('div','',service.name);name.append(element('small','',`Startup ${service.startup}`));item.append(name,element('span','status'+(service.state==='active'?' online':''),service.state));$('support').append(item)}
  $('message').textContent='';
 }catch(error){$('message').textContent=error.message}finally{$('refresh').disabled=false}
}
$('refresh').onclick=refresh;refresh();setInterval(refresh,10000);
