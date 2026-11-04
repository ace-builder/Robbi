import {$,api,esc,heading,empty,raw,notify,action,issue} from '../core.js';

const terminal=new Set(['done','failed','cancelled']);
const pendingKey='robbi.app-management.job';
const appLink=id=>`#apps/${encodeURIComponent(id)}`;
const safeUrl=value=>{try{const url=new URL(value);return ['https:','http:'].includes(url.protocol)?url.href:''}catch{return ''}};

// Load store artwork one request at a time. A grid of author images must not
// fill Robbi's reserved HTTP slots or leave thumbnails broken after throttling.
export function storeImages(root,life){
  let queue=[],active=false,disposed=false;
  const seen=new WeakSet(),timers=new Set();
  function pump(){
    if(active||disposed)return;
    queue=queue.filter(item=>item.image.isConnected);
    const item=queue.shift();if(!item)return;active=true;
    const image=item.image;let done=false;
    const timeout=setTimeout(()=>finish(false),20000);timers.add(timeout);
    function finish(ok){
      if(done)return;done=true;clearTimeout(timeout);timers.delete(timeout);
      image.removeEventListener('load',loaded);image.removeEventListener('error',failed);active=false;
      if(!ok&&item.attempt<2&&!disposed&&image.isConnected){
        image.removeAttribute('src');
        const retry=setTimeout(()=>{timers.delete(retry);queue.push({image,attempt:item.attempt+1});pump()},500*(item.attempt+1));timers.add(retry);
      }
      pump();
    }
    const loaded=()=>finish(true),failed=()=>finish(false);
    image.addEventListener('load',loaded);image.addEventListener('error',failed);image.src=image.dataset.storeSrc;
  }
  const observer=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting){observer.unobserve(entry.target);queue.push({image:entry.target,attempt:0})}pump()},{rootMargin:'200px'});
  life.add(()=>{disposed=true;queue=[];observer.disconnect();timers.forEach(clearTimeout);timers.clear()});
  return {observe(){queue=queue.filter(item=>item.image.isConnected);for(const image of root.querySelectorAll('img[data-store-src]'))if(!seen.has(image)){seen.add(image);observer.observe(image)}}};
}

async function awaitJob(id,signal){
  for(let i=0;i<200;i++){
    const {job}=await api(`/api/app-management/${encodeURIComponent(id)}`,{signal});
    if(terminal.has(job.state)){
      sessionStorage.removeItem(pendingKey);
      if(job.state!=='done')throw new Error(job.error||'App change was cancelled. Check the library before trying again.');
      if(!job.result?.ok)throw new Error(job.result?.text||'The app change could not be completed.');
      return job.result;
    }
    await new Promise(resolve=>setTimeout(resolve,500));
    if(signal?.aborted)throw new DOMException('Page closed','AbortError');
  }
  throw new Error('The app change is still running. Its status will be checked when you reopen the store.');
}

export async function manageApp(spec,signal){
  const {job}=await api('/api/app-management',{method:'POST',body:spec,signal});
  sessionStorage.setItem(pendingKey,job.id);
  return awaitJob(job.id,signal);
}

export async function appHub(root,life){
  root.innerHTML=`<a class="app-back" href="#apps">← Installed apps</a>`+heading('MORE POSSIBILITIES','Robbi App Hub.','Choose an app. Robbi checks the signed package and keeps its data separate.',
    '<button id="store-refresh">Refresh Hub</button>')+`
    <section id="store-connection" class="panel"><div id="store-status"></div>
      <details id="store-setup"><summary>Connect to Robbi App Hub</summary><div class="details-body">
        <p>Robbi connects to a separate App Hub. Review its signing key once, then choose which apps to install.</p>
        <form id="store-connect-form"><div class="form-grid"><label>Hub URL<input name="url" type="url" maxlength="1000" placeholder="https://ai-tier.com/apphub" required></label>
        <label class="app-schema-check"><input name="local" type="checkbox"> Allow HTTP for a local development server</label></div>
        <div class="actions"><button type="submit">Check Hub</button><button id="store-disconnect" type="button" class="small">Disconnect</button></div><div class="form-output section-gap" aria-live="polite"></div></form>
        <div id="store-trust" hidden></div></div></details></section>
    <div id="store-feedback" class="section-gap" role="status" aria-live="polite"></div>
    <div class="app-library-toolbar section-gap"><label class="app-search"><span class="sr-only">Find an app in the store</span><input id="store-query" type="search" placeholder="Find your next app…" maxlength="200"></label>
      <label>Category<select id="store-category"><option value="">All categories</option></select></label><label>Show<select id="store-filter"><option value="all">All apps</option><option value="new">Not installed</option><option value="installed">Installed</option></select></label></div>
    <div id="store-cards" class="grid three">${empty('Connect to Robbi App Hub to browse its apps.')}</div>
    <section class="panel section-gap"><h2>Removed apps</h2><p class="muted">Saved data stays on Robbi. Restore an app when you need it again.</p><div id="store-removed"></div></section>
    <dialog id="store-detail" class="store-dialog"><div class="actions"><button id="store-close" type="button">Close</button></div><div id="store-detail-content"></div></dialog>`;
  let catalog=[],status=null,candidate=null,loading=false;
  const feedback=$('#store-feedback',root),dialog=$('#store-detail',root);
  const artwork=storeImages(root,life);
  function render(){
    const query=$('#store-query',root).value.trim().toLocaleLowerCase(),category=$('#store-category',root).value,filter=$('#store-filter',root).value;
    const selected=catalog.filter(app=>(!category||app.category===category)&&(filter==='all'||(filter==='installed'?app.installed:!app.installed))&&`${app.name} ${app.id} ${app.description} ${(app.tags||[]).join(' ')}`.toLocaleLowerCase().includes(query));
    $('#store-cards',root).innerHTML=selected.map(app=>`<article class="app-card app-library-card store-card">
      <div class="store-app-mark">${app.images?.icon?`<img data-store-src="/api/app-store/apps/${encodeURIComponent(app.id)}/images/icon" width="64" height="64" alt="">`:`<span aria-hidden="true">${esc(app.name.slice(0,2).toUpperCase())}</span>`}<div><span class="hint">${esc(app.category||'App')}</span><h2>${esc(app.name)}</h2></div></div>
      <p>${esc(app.description)}</p><span class="hint">${esc(app.author?.name||'Independent author')} · v${esc(app.version)}</span>
      ${app.installed?`<span class="badge ready">Installed · ${esc(app.installed_version)}</span>`:''}
      <div class="app-card-foot"><button class="small" data-details="${esc(app.id)}">Details</button>${app.installed&&app.installed_version===app.version?`<a class="button-link small" href="${appLink(app.id)}">Open app</a>`:`<button class="primary small" data-manage="${app.installed?'update':'install'}" data-app="${esc(app.id)}">${app.installed?'Update':'Install'}</button>`}</div></article>`).join('')||empty(status?.configured?'No matching apps.':'Connect your store to browse apps.');
    artwork.observe();
  }
  function renderStatus(){
    const store=status?.store;
    $('#store-status',root).innerHTML=store?`<div class="panel-head"><div><span class="badge ready">Trusted store</span><h2>${esc(store.name)}</h2><p class="hint">${esc(store.url)}</p></div><span class="hint">Signatures checked on Robbi</span></div>`:`<h2>Connect Robbi App Hub</h2><p class="muted">Choose the Hub address below, or change its default in Settings.</p>`;
    if(status?.error)issue(feedback,status.error);
    $('#store-setup',root).open=!status?.configured;
    const form=$('#store-connect-form',root);form.elements.url.value=store?.url||status?.hub_url||'https://ai-tier.com/apphub';form.elements.local.checked=store?.allow_http??false;
    $('#store-disconnect',root).hidden=!status?.configured;
    $('#store-removed',root).innerHTML=(status?.removed||[]).map(app=>`<div class="store-removed-row"><div><strong>${esc(app.name)}</strong><span class="hint"> · ${esc(app.version)}</span></div><button class="small" data-manage="restore" data-app="${esc(app.id)}" ${app.restore_possible?'':'disabled'}>Restore</button></div>`).join('')||'<p class="hint">No removed apps.</p>';
  }
  async function refresh(force=false){
    if(loading)return;loading=true;
    try{
      status=await api('/api/app-store',{signal:life.signal});if(life.signal.aborted)return;renderStatus();
      catalog=status.configured?(await api('/api/app-store/catalog'+(force?'?refresh=1':''),{signal:life.signal,timeout:20000})).apps:[];
      if(life.signal.aborted)return;
      const selected=$('#store-category',root).value;
      $('#store-category',root).innerHTML='<option value="">All categories</option>'+[...new Set(catalog.map(app=>app.category).filter(Boolean))].sort().map(value=>`<option value="${esc(value)}">${esc(value)}</option>`).join('');
      $('#store-category',root).value=selected;render();
    }catch(error){if(!life.signal.aborted){issue(feedback,error);$('#store-cards',root).innerHTML=empty('Store unavailable. Installed apps still work.')}}finally{loading=false}
  }
  life.on($('#store-refresh',root),'click',()=>refresh(true));
  for(const id of ['store-query','store-category','store-filter'])life.on($('#'+id,root),id==='store-query'?'input':'change',render);
  life.on($('#store-close',root),'click',()=>dialog.close());life.add(()=>dialog.close());
  life.on($('#store-connect-form',root),'submit',event=>{
    event.preventDefault();const form=event.currentTarget;candidate=null;$('#store-trust',root).hidden=true;
    action($('button[type=submit]',form),async()=>{
      candidate=await api('/api/app-store/probe',{method:'POST',body:{url:form.elements.url.value,allow_http:form.elements.local.checked},signal:life.signal,timeout:20000});
      if(life.signal.aborted)return;
      const trust=$('#store-trust',root);trust.hidden=false;trust.innerHTML=`<div class="notice section-gap"><strong>${esc(candidate.name)}</strong><p>Trust packages signed by this store. Compare this fingerprint with the server's setup page.</p><code class="store-fingerprint">${esc(candidate.fingerprint)}</code><div class="actions"><button id="store-trust-button" type="button" class="primary">Trust and connect</button></div></div>`;
      life.on($('#store-trust-button',root),'click',event=>action(event.currentTarget,async()=>{await api('/api/app-store/connect',{method:'POST',body:{url:candidate.url,public_key:candidate.public_key,allow_http:candidate.allow_http},signal:life.signal,timeout:20000});trust.hidden=true;candidate=null;await refresh(true)},{output:feedback,success:'Store connected.'}));
    },{output:$('.form-output',form),success:''});
  });
  life.on($('#store-disconnect',root),'click',event=>action(event.currentTarget,async()=>{await api('/api/app-store',{method:'DELETE',signal:life.signal});await refresh()},{output:feedback,success:'Store disconnected. Installed apps remain available.'}));
  life.on(root,'click',async event=>{
    const button=event.target.closest('button[data-manage],button[data-details]');if(!button)return;
    if(button.dataset.manage){
      await action(button,async()=>{const reply=await manageApp({operation:button.dataset.manage,app_id:button.dataset.app},life.signal);feedback.textContent=reply.text;notify(reply.text);await refresh(true)},{success:'',output:null});
      return;
    }
    await action(button,async()=>{
      const {app}=await api('/api/app-store/apps/'+encodeURIComponent(button.dataset.details),{signal:life.signal,timeout:20000});if(life.signal.aborted)return;
      const website=safeUrl(app.homepage),authorUrl=safeUrl(app.author?.url);
      $('#store-detail-content',root).innerHTML=`<span class="hint">${esc(app.category||'App')} · ${esc(app.version)}</span><h2>${esc(app.name)}</h2><p>${esc(app.description)}</p><p>By ${authorUrl?`<a href="${esc(authorUrl)}" target="_blank" rel="noopener noreferrer">${esc(app.author.name)}</a>`:esc(app.author?.name||'Independent author')}</p>${app.images?.screen?`<img class="store-screen" data-store-src="/api/app-store/apps/${encodeURIComponent(app.id)}/images/screen" width="280" height="240" alt="${esc(app.name)} screen preview">`:''}
        <h3>Capabilities</h3><p>${esc(app.permissions.join(', ')||'No special capabilities')}</p><p class="hint">Installation enables the app. Capabilities are approved separately when needed. Imported Python apps run in an isolated Linux process. Robot data, devices and provider access go through approved SDK capabilities; direct network and host-file access are blocked. A signature identifies the store, not app quality.</p>
        <h3>Tools</h3>${app.tools.map(tool=>`<p><strong>${esc(tool.title||tool.name)}</strong><br>${esc(tool.description)}</p>`).join('')}
        ${raw('Settings available to this app',app.config_schema)}${raw('Compatibility requirements',app.requires||{})}<p class="hint">${esc(app.license||'License not specified')}${website?` · <a href="${esc(website)}" target="_blank" rel="noopener noreferrer">App website</a>`:''}</p><code class="store-fingerprint">SHA-256 ${esc(app.sha256)}</code>`;
      dialog.showModal();
      artwork.observe();
    },{success:''});
  });
  await refresh();
  const pending=sessionStorage.getItem(pendingKey);
  if(pending&&!life.signal.aborted){feedback.textContent='Checking the previous app change…';try{const reply=await awaitJob(pending,life.signal);feedback.textContent=reply.text;await refresh(true)}catch(error){if(!life.signal.aborted)issue(feedback,error)}}
}
