import {$,$$,api,esc,title,badge,heading,empty,raw,notify,action,issue,pretty,download} from '../core.js';
import {mountToolHistory} from '../app-ui/history.js';
import {schemaFields,schemaValues} from '../app-ui/schema.js';
import {mountAppBridge} from '../app-ui/bridge.js';
import {manageApp} from './app-hub.js';
import {renderExperience,teardownExperience,beginExperienceInteraction,cancelExperienceInteraction} from '../app-ui/experience.js';
import {applyFormGuidance,formGuidanceDefaults} from '../app-ui/form-guidance.js';

const terminal=new Set(['done','succeeded','failed','cancelled','canceled','expired','timed_out','permission_required','permission_denied']);
// Retain only a few job snapshots while navigating within this dashboard.
const recentJobs=new Map();
const resultSignatures=new WeakMap();
const healthState=app=>!app.enabled?'disabled':typeof app.health==='string'?app.health:app.health?.state||'ready';
const appBadge=app=>healthState(app)==='ready'?'<span class="badge ready">Enabled</span>':badge(healthState(app));
const summaryError=app=>typeof app.health==='object'?(app.health.error||app.health.message||''):'';
const appUrl=id=>`/api/apps/${encodeURIComponent(id)}`;
const appLink=id=>`#apps/${encodeURIComponent(id)}`;
const phrases=items=>(items||[]).filter(value=>typeof value==='string').slice(0,8);
const pendingPermissions=access=>Array.isArray(access?.pending)?access.pending.length:(access?.permissions||[]).filter(permission=>permission.status==='pending').length;
const approvalBadge=access=>pendingPermissions(access)?`<span class="badge queued">${pendingPermissions(access)} ${pendingPermissions(access)===1?'approval':'approvals'} needed</span>`:'';
const bypassNotice='<div class="notice"><strong>App permission checks are disabled.</strong> Enabled apps can request all capabilities, including undeclared ones.<p class="hint">Development mode: enable <code>apps.permissions_enabled</code> and restart Robbi to restore checks.</p></div>';
const collections=[
  {id:'all',name:'All apps',symbol:'✦'},
  {id:'everyday',name:'Everyday',symbol:'☀',apps:['weather','world-clock','calculator','unit-converter','percentage-helper','tip-splitter','date-calculator','random-number','password-generator','text-toolbox']},
  {id:'home',name:'Home & life',symbol:'⌂',apps:['home-care','fridge-rescue','kitchen-timers','recipe-scaler','where-did-i-put-it','ready-to-leave','packing-list']},
  {id:'organize',name:'Get organized',symbol:'✓',apps:['checklists','quick-notes','bookmarks','habit-tracker','expense-tracker','decision-helper','decision-notebook','meeting-prep','focus-timer','reminder']},
  {id:'create',name:'Create & learn',symbol:'✺',apps:['image-studio','teach-me','expressions']},
  {id:'play',name:'Play together',symbol:'◈',apps:['kids','tiny-detective','robot-charades','rhythm-relay','memory-colours','higher-lower','tic-tac-toe']},
  {id:'connected',name:'Connected',symbol:'↗',apps:['gmail','google-calendar','google-maps','web-search']},
  {id:'robot',name:'Robot & more',symbol:'⚙',apps:['robot-status','sensor-check','hello-demo']}
];
const categoryCollections={games:'play',game:'play',utilities:'everyday',utility:'everyday',productivity:'organize',organization:'organize',home:'home',lifestyle:'home',kitchen:'home',food:'home',creative:'create',creativity:'create',education:'create',learning:'create',art:'create',communication:'connected',integrations:'connected',connected:'connected',robot:'robot',hardware:'robot',system:'robot'};
const collectionFor=app=>collections.find(group=>group.apps?.includes(app.id))||collections.find(group=>group.id===(categoryCollections[String(app.category||'').toLowerCase()]||app.category))||collections.at(-1);
const artTone=id=>[...String(id)].reduce((n,char)=>n+char.charCodeAt(0),0)%6;
// Use the packaged small illustration on the Pi's Wi-Fi; retain full icons for exports.
const appArt=(app,className='',kind=app.images?.screen?'screen':'icon')=>`<div class="app-art ${className} tone-${artTone(app.id)}"><span class="app-art-fallback" aria-hidden="true">${esc(collectionFor(app).symbol)}</span><img src="${appUrl(app.id)}/images/${kind}" alt="" width="280" height="240" loading="lazy" decoding="async"></div>`;
function imageFallbacks(root,life){
  // A missing imported illustration retains its collection's visual identity.
  $$('img',root).forEach(img=>{const update=()=>{img.classList.toggle('image-unavailable',!img.naturalWidth);img.parentElement.classList.toggle('art-loaded',!!img.naturalWidth);img.onload=null;img.onerror=null};img.onload=update;img.onerror=update;if(img.complete)update()});
}

export async function apps(root,life){
  const encoded=location.hash.slice(1).split('/')[1];
  if(encoded){let id;try{id=decodeURIComponent(encoded)}catch{throw new Error('The app address is invalid.')};return appPage(root,life,id)}
  root.innerHTML=`<section class="app-library-hero"><div class="app-hero-copy"><div class="eyebrow">LITTLE APPS. BIG POSSIBILITIES.</div><h1>What shall we<br><span>do today?</span></h1><p>Make something. Solve a little mystery. Get life in order.<br>Your next good idea starts with an app.</p><div class="app-hero-links"><a href="#app-hub">Discover more apps <span aria-hidden="true">↗</span></a><button id="apps-refresh" class="small">Refresh library</button></div></div><div class="app-hero-art" aria-hidden="true"><span class="app-hero-spark spark-one">✦</span><div class="app-hero-tile tile-one">${appArt({id:'image-studio'},'','screen')}<span>Make a little magic</span></div><div class="app-hero-tile tile-two">${appArt({id:'tiny-detective'},'','screen')}<span>Follow your curiosity</span></div><span class="app-hero-spark spark-two">✺</span></div></section>
    <div class="app-library-toolbar"><label class="app-search"><span aria-hidden="true">⌕</span><span class="sr-only">Find an app</span><input id="app-search" type="search" placeholder="Find your next favorite app…" autocomplete="off"></label><label class="app-status-filter"><span class="sr-only">Filter app status</span><select id="app-status-filter"><option value="all">All statuses</option><option value="enabled">Enabled</option><option value="approval">Needs approval</option><option value="disabled">Disabled</option></select></label><a href="#daily">Daily tools →</a></div>
    <div id="app-collections" class="app-collections" role="group" aria-label="App categories"></div>
    <div class="app-library-caption"><h2>Your app collection</h2><span id="app-match-count" class="hint" role="status" aria-live="polite"></span></div>
    <div id="app-permission-mode" class="app-notice" hidden></div>
    <div id="app-library" class="app-library-grid" aria-label="Apps">${empty('Finding your apps…')}</div>
    <div id="app-catalog-errors" class="section-gap"></div>
    <details class="section-gap"><summary>Add an app</summary><div class="details-body"><p class="muted">Import a project ZIP containing an <code>app.yaml</code> manifest. Imported projects stay disabled until you enable them, then run in Linux isolation through approved robot APIs; direct host-file and network access is blocked.</p><form id="project-install"><label>Project ZIP<input name="file" type="file" accept=".zip,application/zip" required></label><div class="actions"><button type="submit">Import project</button><button type="button" id="apps-scan">Scan app folders</button></div><div class="form-output section-gap" aria-live="polite"></div></form></div></details>
    <details class="section-gap"><summary>How apps work</summary><div class="details-body"><p>Each app supplies its own tools, settings, descriptions and example requests. Robbi uses those descriptions to choose the right tool when you talk to it.</p><p class="muted">Simple apps use these generated controls. Projects can also include a custom page. Provider keys stay in environment variables on the robot; app settings contain the variable names.</p><div id="app-limits"></div></div></details>`;
  let catalog=[],catalogLoading=false,catalogChecked=0,category='all';
  function renderCollections(){
    const focused=document.activeElement?.dataset.appCategory;
    $('#app-collections',root).innerHTML=collections.filter(group=>group.id==='all'||catalog.some(app=>collectionFor(app).id===group.id)).map(group=>`<button type="button" data-app-category="${group.id}" aria-pressed="${category===group.id}"><span aria-hidden="true">${group.symbol}</span>${group.name}<small>${group.id==='all'?catalog.length:catalog.filter(app=>collectionFor(app).id===group.id).length}</small></button>`).join('');
    if(focused)$$('[data-app-category]',root).find(button=>button.dataset.appCategory===focused)?.focus({preventScroll:true});
  }
  function render(){
    const focused=document.activeElement?.closest('.app-library-card')?.getAttribute('href');
    const query=$('#app-search',root).value.trim().toLowerCase();
    const status=$('#app-status-filter',root).value;
    const selected=catalog.filter(app=>`${app.name} ${app.id} ${app.description} ${collectionFor(app).name}`.toLowerCase().includes(query)&&(category==='all'||collectionFor(app).id===category)&&(status==='all'||status==='enabled'&&app.enabled||status==='disabled'&&!app.enabled||status==='approval'&&app.access?.enforced!==false&&pendingPermissions(app.access)));
    $('#app-match-count',root).textContent=`${selected.length} of ${catalog.length} apps`;
    $('#app-library',root).innerHTML=selected.map((app,index)=>`<a href="${appLink(app.id)}" class="app-library-card" aria-labelledby="app-card-title-${index}" style="--app-tone:${artTone(app.id)}">${appArt(app)}<div class="app-library-card-body"><div class="app-card-kicker"><span>${esc(collectionFor(app).name)}</span>${appBadge(app)}</div><h2 id="app-card-title-${index}">${esc(app.name||app.id)}<span aria-hidden="true">↗</span></h2><p>${esc(app.description||'A little more possibility for your day.')}</p>${app.access?.enforced!==false&&pendingPermissions(app.access)?`<div class="chips">${approvalBadge(app.access)}</div>`:''}${summaryError(app)?`<p class="app-card-error">${esc(summaryError(app))}</p>`:''}<div class="app-card-foot"><span>${(app.tools||[]).length} ${(app.tools||[]).length===1?'tool':'tools'}</span><span>${app.bundled?'Made for Robbi':'Your collection'}</span></div></div></a>`).join('')||`<div class="app-no-matches"><span aria-hidden="true">⌕</span><h3>No apps here yet</h3><p>${esc(query||category!=='all'||status!=='all'?'Try another search or choose a different category or status.':'Visit the app store or import a project below to start your collection.')}</p></div>`;
    imageFallbacks($('#app-library',root),life);
    if(focused)$$('.app-library-card',root).find(link=>link.getAttribute('href')===focused)?.focus({preventScroll:true});
    $$('[data-app-category]',root).forEach(button=>button.setAttribute('aria-pressed',String(category===button.dataset.appCategory)));
    const mode=$('#app-permission-mode',root);mode.hidden=!catalog.some(app=>app.access?.enforced===false);mode.innerHTML=mode.hidden?'':bypassNotice;
  }
  async function refresh(scan=false){
    catalogLoading=true;catalogChecked=Date.now();
    try{
      if(scan)await api('/api/apps/scan',{method:'POST',body:{},signal:life.signal});
      const result=await api('/api/apps',{signal:life.signal});if(life.signal.aborted)return;
      catalog=result.apps||[];if(category!=='all'&&!catalog.some(app=>collectionFor(app).id===category))category='all';renderCollections();render();
      const errors=result.errors||[];
      $('#app-catalog-errors',root).innerHTML=errors.length?`<details><summary>${errors.length} ${errors.length===1?'project needs':'projects need'} attention</summary><div class="details-body">${errors.map(error=>`<div class="notice error section-gap">${esc(typeof error==='string'?error: error.message||error.error||pretty(error))}</div>`).join('')}</div></details>`:'';
      $('#app-limits',root).innerHTML=result.limits?raw('Runtime limits',result.limits):'';
    }catch(error){if(!life.signal.aborted){issue($('#app-library',root),error);throw error}}finally{catalogLoading=false}
  }
  life.on($('#app-search',root),'input',render);
  life.on($('#app-status-filter',root),'change',render);
  life.on($('#app-collections',root),'click',event=>{const button=event.target.closest('[data-app-category]');if(button){category=button.dataset.appCategory;render()}});
  life.on(document,'robbi:status',()=>{if(!document.hidden&&!catalogLoading&&Date.now()-catalogChecked>=15000)refresh().catch(()=>{})});
  life.on($('#apps-refresh',root),'click',event=>action(event.currentTarget,()=>refresh(),{success:'App library refreshed.'}));
  life.on($('#apps-scan',root),'click',event=>action(event.currentTarget,()=>refresh(true),{success:'App folders scanned.'}));
  life.on($('#project-install',root),'submit',async event=>{
    event.preventDefault();const form=event.currentTarget;
    const result=await action($('button[type=submit]',form),()=>api('/api/apps/install',{method:'POST',body:new FormData(form),timeout:30000,signal:life.signal}),{output:$('.form-output',form),success:'Project imported. Open it to review and enable it.'});
    if(result&&!life.signal.aborted){form.reset();await refresh()}
  });
  imageFallbacks($('.app-hero-art',root),life);await refresh();
}

async function appPage(root,life,id){
  root.innerHTML=heading('APP LIBRARY','Opening app…','Loading tools and configuration.');
  const detail=await api(appUrl(id),{signal:life.signal});if(life.signal.aborted)return;
  const app=detail.app||detail,tools=app.tools||[];
  let config=detail.config||{},job=recentJobs.get(id)||null,timer=null,pollCount=0,pollFailures=0,polling=false,submitting=false,lastArguments=null,lastTool=null;
  const interactionTokens=new Map();let pendingInteraction=null;
  function cancelInteractions(){if(pendingInteraction)cancelExperienceInteraction(pendingInteraction);pendingInteraction=null;interactionTokens.forEach(cancelExperienceInteraction);interactionTokens.clear()}
  let permissionState={access:app.access||null,owner_can_decide:false,owner_hint:'Checking owner access…'},permissionLoading=false,permissionSaving=false,permissionsChecked=0,permissionRevision=0,permissionsJobId=null,permissionFeedback='',permissionFeedbackError=false,permissionRendered='',permissionRefreshAgain=false;
  const configSchema=detail.config_schema||app.config_schema||{};
  root.innerHTML=`<a class="app-back" href="#apps">← All your apps</a><section class="app-detail-hero">${appArt(app,'app-detail-art')}`+heading(collectionFor(app).name,app.name||id,app.description||'',`<div class="actions compact">${appBadge(app)}${app.dashboard?.page?'<button id="app-dashboard-shortcut" class="primary">Jump in ↓</button>':''}<button id="app-enabled" class="small">${app.enabled?'Disable':'Enable app'}</button></div>`)+`</section>
    <div id="app-state-notice"></div>
    <div id="app-access-notice"></div>
    <details id="app-permissions" class="app-permissions-panel section-gap"><summary><span id="app-permissions-title" tabindex="-1">Access & permissions</span><span id="app-permissions-summary"></span></summary><div class="details-body"><div id="app-permissions-content"></div><p id="app-permission-feedback" class="hint" role="status" aria-live="polite"></p></div></details>
    <div class="grid full-left app-tool-workspace"><section class="panel app-input-panel"><div class="app-section-label"><span aria-hidden="true">01</span> MAKE IT HAPPEN</div><h2>${tools.length===1?esc(tools[0].title||title(tools[0].name)):'What would you like to do?'}</h2>${tools.length?`<form id="app-tool-form"><label ${tools.length===1?'hidden':''}>Choose an action<select id="app-tool" aria-label="Choose an action">${tools.map(tool=>`<option value="${esc(tool.name)}">${esc(tool.title||title(tool.name))}</option>`).join('')}</select></label><div id="app-tool-description" class="section-gap"></div><div id="app-tool-fields" class="form-grid section-gap"></div><div class="actions"><button type="submit" class="primary" id="app-run">Let's go</button><span id="app-tool-timeout" class="hint"></span></div><div id="app-tool-error" class="section-gap" role="status"></div></form>`:empty('This app has no callable tools.')}</section>
    <section class="panel app-result-panel"><div class="app-section-label"><span aria-hidden="true">02</span> THE GOOD STUFF</div><div class="panel-head"><h2>Your result</h2><span id="app-job-state"></span></div><div id="app-job-output" aria-live="polite"><div class="app-result-welcome"><span aria-hidden="true">✦</span><h3>A little possibility awaits.</h3><p>Choose an action and make it yours. You can also ask Robbi in a conversation.</p></div></div><div id="app-job-actions" class="actions" hidden><button id="app-cancel-job" class="small">Cancel request</button><button id="app-check-job" class="small">Check status</button></div></section></div>
    ${app.dashboard?.page?`<section class="panel section-gap app-custom-panel"><div class="panel-head"><div><div class="app-section-label">YOUR INTERACTIVE APP</div><h2>${esc(app.dashboard.title||app.name||'App dashboard')}</h2></div><button id="app-page-open" class="small">Open interactive app</button></div><div id="app-page-mount" hidden></div></section>`:''}
    ${(configSchema.properties&&Object.keys(configSchema.properties).length)?`<details id="app-settings" class="section-gap"><summary>Settings & provider</summary><div class="details-body"><form id="app-config-form"><div class="form-grid">${schemaFields(configSchema,config)}</div><p class="hint section-gap">API key fields name environment variables configured on Robbi. Do not paste API keys into these settings.</p><div class="actions"><button type="submit">Save settings</button><span id="app-config-saved" role="status"></span></div><div class="form-output section-gap" aria-live="polite"></div></form></div></details>`:''}
    <details class="section-gap"><summary>About this app</summary><div class="details-body"><dl class="key-values"><dt>Project</dt><dd>${esc(id)}</dd><dt>Version</dt><dd>${esc(app.version||'—')}</dd><dt>Author</dt><dd>${esc(app.author?.name||'Not specified')}</dd><dt>Category</dt><dd>${esc(app.category||'App')}</dd><dt>Source</dt><dd>${app.bundled?'Built in':'Installed project'}</dd><dt>Robot access</dt><dd>${esc((app.permissions||[]).join(', ')||'None')}</dd></dl>${phrases(app.triggers).length?`<h3 class="section-gap">Try saying</h3><ul class="app-examples">${phrases(app.triggers).map(text=>`<li>“${esc(text)}”</li>`).join('')}</ul>`:''}<p class="hint">Removing an app keeps saved data. Restore it from the App store page.</p><div class="actions"><button id="app-remove" class="danger small">Remove app</button></div></div></details>`;

  imageFallbacks($('.app-detail-hero',root),life);
  const workspace=$('.app-tool-workspace',root),customPanel=$('.app-custom-panel',root);
  if(customPanel){
    workspace.before(customPanel);
    const advanced=document.createElement('details');advanced.className='app-direct-actions section-gap';advanced.innerHTML='<summary>More actions & results</summary>';workspace.before(advanced);advanced.append(workspace);
  }
  const historyPanel=document.createElement('section');historyPanel.className='panel section-gap app-history-panel';(workspace.closest('details')||workspace).after(historyPanel);
  historyPanel.after($('#app-permissions',root));
  const refreshHistory=mountToolHistory(historyPanel,life,{appId:id,onJob:watchJob,onReuse:inputs=>{
    const tool=tools.find(item=>item.name===inputs.tool);if(!tool)return;
    $('#app-tool',root).value=inputs.tool;toolFields();
    $('#app-tool-fields',root).innerHTML=schemaFields(tool.input_schema||{},inputs.arguments||{});
    guideForm();
    if(workspace.closest('details'))workspace.closest('details').open=true;
    $('#app-tool-form',root).scrollIntoView({block:'center'});
  }});

  function renderPermissions(){
    const signature=JSON.stringify([permissionState,permissionSaving,permissionFeedback,permissionFeedbackError]);
    if(signature===permissionRendered)return;permissionRendered=signature;
    const access=permissionState.access,content=$('#app-permissions-content',root),summary=$('#app-permissions-summary',root);
    const pending=access?.enforced!==false&&pendingPermissions(access);
    $('#app-access-notice',root).innerHTML=pending?`<div class="notice app-access-callout"><div><strong>A quick approval, then you're ready.</strong><p>${pending} ${pending===1?'capability needs':'capabilities need'} the owner's permission.</p></div><button type="button" class="small" data-review-permissions>Review access ↓</button></div>`:'';
    $('#app-permissions',root).classList.toggle('has-pending',!!pending);
    if(pending)$('#app-permissions',root).open=true;
    if(!access){summary.innerHTML='';content.innerHTML=`<p class="muted">${esc(permissionState.owner_hint||'Loading permission status…')}</p>`}
    else{
      const permissions=Array.isArray(access.permissions)?access.permissions:[],enforced=access.enforced!==false;
      const canDecide=enforced&&permissionState.owner_can_decide;
      const labels={unrequested:'Not requested',pending:'Needs approval',granted:'Allowed',denied:'Denied',bypassed:'Checks disabled'};
      summary.innerHTML=enforced?approvalBadge(access)||(permissions.length?`<span class="badge">${permissions.length} ${permissions.length===1?'capability':'capabilities'}</span>`:''):'<span class="badge degraded">Development mode</span>';
      const rows=permissions.map(permission=>{
        const status=permission.status,label=permission.label||title(permission.id),style={pending:'queued',granted:'ready',denied:'failed',bypassed:'degraded'}[status]||'';
        const button=(decision,text,danger=false)=>`<button type="button" class="small${danger?' danger':''}" data-permission-decision="${decision}" data-capability="${esc(permission.id)}" aria-label="${esc(text+' '+label)}" ${permissionSaving?'disabled':''}>${text}</button>`;
        const controls=!canDecide?'':status==='granted'?button('unrequested','Revoke',true):button('granted','Allow')+(status==='denied'?'':button('denied','Deny'));
        return `<div class="app-permission-row"><div class="app-permission-description"><strong>${esc(label)}</strong><p>${esc(permission.description||'')}</p></div><div class="app-permission-controls"><span class="badge ${style}">${esc(labels[status]||title(status||'unknown'))}</span>${controls}</div></div>`;
      }).join('');
      content.innerHTML=(enforced?`<p class="muted">${permissions.length?'Allow access once, and revoke it whenever you need to. Decisions are saved.':'This app does not request any capabilities.'}</p>`:bypassNotice)+(access.error?`<div class="notice error">${esc(access.error)}</div>`:'')+(!enforced&&rows?`<details class="section-gap"><summary>Available capabilities (${permissions.length})</summary><div class="details-body">${rows}</div></details>`:rows)+(enforced&&!permissionState.owner_can_decide?`<p class="hint app-owner-hint">${esc(permissionState.owner_hint||'Permission decisions require owner access.')}</p>`:'');
    }
    const feedback=$('#app-permission-feedback',root);feedback.textContent=permissionSaving?'Saving decision…':permissionFeedback;feedback.className=`hint${permissionFeedbackError?' app-permission-error':''}`;feedback.hidden=!feedback.textContent;
  }
  async function refreshPermissions(force=false){
    if(life.signal.aborted||permissionSaving)return;
    if(permissionLoading){permissionRefreshAgain=permissionRefreshAgain||force;return}
    if(!force&&(document.hidden||Date.now()-permissionsChecked<15000))return;
    permissionLoading=true;permissionsChecked=Date.now();const revision=permissionRevision;
    try{
      const result=await api(`${appUrl(id)}/permissions`,{signal:life.signal,timeout:6000});
      if(!life.signal.aborted&&revision===permissionRevision){permissionState=result;app.access=result.access;renderPermissions()}
    }catch(error){
      if(!life.signal.aborted&&revision===permissionRevision){permissionState={...permissionState,owner_can_decide:false,owner_hint:`Permission status could not be refreshed. ${error.message}`};renderPermissions()}
    }finally{permissionLoading=false;if(permissionRefreshAgain&&!life.signal.aborted){permissionRefreshAgain=false;refreshPermissions(true)}}
  }
  async function decidePermission(capability,decision){
    if(permissionSaving||!permissionState.owner_can_decide||permissionState.access?.enforced===false)return;
    permissionSaving=true;permissionRevision++;permissionFeedback='';permissionFeedbackError=false;renderPermissions();
    try{
      const result=await api(`${appUrl(id)}/permissions`,{method:'PATCH',body:{decisions:{[capability]:decision}},signal:life.signal});
      if(life.signal.aborted)return;
      permissionState=result;app.access=result.access;permissionsChecked=Date.now();
      permissionFeedback=decision==='granted'?'Access allowed. Run the tool again when you are ready.':decision==='denied'?'Access denied. This decision is saved.':'Access revoked. A future request will need approval.';
      if(job&&!terminal.has(job.state))schedule(100);
    }catch(error){if(!life.signal.aborted){permissionFeedback=error.message;permissionFeedbackError=true}}
    finally{permissionSaving=false;if(!life.signal.aborted){renderPermissions();if(permissionFeedbackError)refreshPermissions(true)}}
  }
  function gates(){
    const active=job&&!terminal.has(job.state);
    if($('#app-run',root))$('#app-run',root).disabled=!app.enabled||!!active||submitting;
    if($('#app-tool',root))$('#app-tool',root).disabled=!!active||submitting;
    $('#app-enabled',root).textContent=app.enabled?'Disable':'Enable app';
    $('#app-state-notice',root).innerHTML=!app.enabled?`<div class="notice section-gap app-notice">${app.bundled?'This app is disabled. Enable it to use its tools.':'This imported project is disabled. Enable it only if you trust its code.'}</div>`:summaryError(app)?`<div class="notice error section-gap app-notice">${esc(summaryError(app))}</div>`:'';
  }
  function toolFields(){
    const tool=tools.find(tool=>tool.name===$('#app-tool',root)?.value);if(!tool)return;
    $('#app-tool-description',root).innerHTML=`<p class="muted">${esc(tool.description||'')}</p>${phrases(tool.examples).length?`<p class="hint">Try: “${esc(phrases(tool.examples)[0])}”</p>`:''}`;
    $('#app-tool-fields',root).innerHTML=schemaFields(tool.input_schema||{});
    $('#app-tool-timeout',root).textContent=tool.timeout_s?`Up to ${tool.timeout_s} seconds`:'';
    $('#app-run',root).textContent=tool.title||title(tool.name);
    $('#app-tool-error',root).innerHTML='';guideForm();gates();
  }
  function guideForm(){
    const tool=tools.find(item=>item.name===$('#app-tool',root)?.value),form=$('#app-tool-form',root);
    const presentation=applyFormGuidance(form,{appId:id,tool});if(!presentation)return;
    $('.app-input-panel h2',root).textContent=presentation.title;
    $('#app-tool-description',root).innerHTML=`<p class="muted">${esc(presentation.description)}</p>`;
    $('#app-run',root).textContent=presentation.button;
  }
  function renderConfigFields(){
    const form=$('#app-config-form',root);if(!form)return;
    const fields=$('.form-grid',form),expanded=$('.app-advanced-fields',fields)?.open;
    fields.innerHTML=schemaFields(configSchema,config);
    if(expanded&&$('.app-advanced-fields',fields))$('.app-advanced-fields',fields).open=true;
  }
  function displayJob(){
    if(!job)return;
    const permissionResult=job.result?.permission_required;
    const outcome=terminal.has(job.state)&&job.result?.ok===false?'failed':job.state==='done'?'succeeded':job.state;
    $('#app-job-state',root).innerHTML=permissionResult?`<span class="badge ${job.result.state==='permission_denied'?'failed':'queued'}">${job.result.state==='permission_denied'?'Permission denied':'Approval needed'}</span>`:badge(outcome);
    const output=$('#app-job-output',root),done=terminal.has(job.state);
    const context={appName:app.name||id,toolName:job.tool||lastTool,arguments:lastArguments,jobId:job.id,interactionToken:interactionTokens.get(job.id),onAction:experienceAction};
    if(permissionResult){renderResult(output,job.result,id,context)}
    else if(job.error){output.innerHTML=`<div class="notice error">${esc(typeof job.error==='string'?job.error:job.error.message||pretty(job.error))}</div>`}
    else if(done&&job.result){renderResult(output,job.result,id,context)}
    else if(done){output.innerHTML=empty(['done','succeeded'].includes(job.state)?'Request completed.':'Request stopped. No further output will be applied.')}
    else{output.innerHTML=`<p class="muted">${job.state==='queued'?'Waiting for an available app worker…':'Working… You can leave this page while the app runs.'}</p>`}
    $('#app-job-actions',root).hidden=!!done;
    $('#app-cancel-job',root).disabled=!!done;gates();
    if(done&&(permissionResult||job.error||job.result?.ok===false||!['done','succeeded'].includes(job.state))){cancelExperienceInteraction(interactionTokens.get(job.id));interactionTokens.delete(job.id)}
    if(done&&permissionsJobId!==job.id){permissionsJobId=job.id;refreshPermissions(true);refreshHistory()}
  }
  function schedule(delay=1000){clearTimeout(timer);if(!life.signal.aborted&&!document.hidden&&job&&!terminal.has(job.state)&&pollCount<300&&pollFailures<8)timer=setTimeout(checkJob,delay)}
  async function checkJob(){
    clearTimeout(timer);if(polling||!job||life.signal.aborted)return;
    polling=true;pollCount++;const requestedId=job.id;
    try{
      const result=await api(`/api/app-jobs/${encodeURIComponent(requestedId)}`,{signal:life.signal,timeout:6000});if(life.signal.aborted||job?.id!==requestedId)return;
      job=result.job||result;recentJobs.set(id,job);pollFailures=0;displayJob();
    }catch(error){
      if(!life.signal.aborted&&job?.id===requestedId){pollFailures++;$('#app-job-output',root).innerHTML=`<div class="notice error">${esc(error.message)} The request will not be sent again.</div>`}
    }finally{polling=false;if(!life.signal.aborted){schedule(Math.min(10000,1000*2**pollFailures));if((pollCount>=300||pollFailures>=8)&&job&&!terminal.has(job.state))$('#app-job-output',root).insertAdjacentHTML('beforeend','<p class="hint section-gap">Automatic status checks paused. Use “Check status” to reconnect to this request.</p>')}}
  }
  function watchJob(next){
    if(!submitting){lastArguments=null;lastTool=null}
    if(job?.id!==next.id){cancelExperienceInteraction(interactionTokens.get(job?.id));interactionTokens.delete(job?.id);const output=$('#app-job-output',root),stage=$('[data-app-experience]',output);if(stage)teardownExperience(stage);resultSignatures.delete(output)}
    job=next;recentJobs.delete(id);recentJobs.set(id,job);if(recentJobs.size>16)recentJobs.delete(recentJobs.keys().next().value);pollCount=0;pollFailures=0;displayJob();schedule(400);
  }
  const jobOutput=$('#app-job-output',root);
  life.add(()=>{clearTimeout(timer);cancelInteractions();const stage=$('[data-app-experience]',jobOutput);if(stage)teardownExperience(stage)});
  life.on(document,'robbi:visibility',()=>{clearTimeout(timer);if(!document.hidden)schedule(0)});
  life.on(document,'robbi:status',()=>refreshPermissions());
  life.on($('#app-permissions',root),'click',event=>{const button=event.target.closest('[data-permission-decision]');if(button)decidePermission(button.dataset.capability,button.dataset.permissionDecision)});
  life.on(root,'click',event=>{if(event.target.closest('[data-review-permissions]')){$('#app-permissions',root).open=true;$('#app-permissions',root).scrollIntoView({block:'start'});$('#app-permissions-title',root).focus({preventScroll:true})}});
  life.on(document,'robbi:stop',()=>{cancelInteractions();if(job&&!terminal.has(job.state)){$('#app-job-output',root).innerHTML='<p class="muted">Stop requested. Waiting for job status…</p>';schedule(400)}});
  life.on($('#app-tool',root),'change',toolFields);
  life.on($('#app-tool-form',root),'change',guideForm);
  function experienceAction(payload){
    if(life.signal.aborted||submitting||!app.enabled||(job&&!terminal.has(job.state)))throw new Error('Wait for the current request to finish before playing again.');
    const tool=tools.find(item=>item.name===payload?.toolName);if(!tool)throw new Error('This action is not available in this app.');
    $('#app-tool',root).value=tool.name;toolFields();
    $('#app-tool-fields',root).innerHTML=schemaFields(tool.input_schema||{},payload.arguments||{});
    guideForm();
    return submitTool($('#app-tool-form',root));
  }
  async function submitTool(form){
    if(life.signal.aborted||submitting||!app.enabled||(job&&!terminal.has(job.state)))throw new Error('This app is not ready for another request.');
    if(!form.reportValidity())throw new Error('Check the highlighted action fields.');
    let args;try{args=schemaValues(form,{initial:formGuidanceDefaults(form)})}catch(error){issue($('#app-tool-error',root),error);throw error}
    cancelInteractions();const interaction=beginExperienceInteraction(id);pendingInteraction=interaction;
    submitting=true;lastArguments=args;lastTool=$('#app-tool',root).value;gates();$('#app-tool-error',root).innerHTML='';
    try{const result=await api(`${appUrl(id)}/tools/${encodeURIComponent($('#app-tool',root).value)}`,{method:'POST',body:{arguments:args},signal:life.signal});if(!life.signal.aborted){const next=result.job||result;interactionTokens.set(next.id,interaction);pendingInteraction=null;watchJob(next);return next}cancelExperienceInteraction(interaction)}
    catch(error){cancelExperienceInteraction(interaction);pendingInteraction=null;if(!life.signal.aborted)issue($('#app-tool-error',root),error);throw error}finally{submitting=false;if(!life.signal.aborted)gates()}
  }
  life.on($('#app-tool-form',root),'submit',event=>{event.preventDefault();submitTool(event.currentTarget).catch(()=>{})});
  life.on($('#app-cancel-job',root),'click',event=>{cancelInteractions();const requestedId=job.id;action(event.currentTarget,async()=>{const result=await api(`/api/app-jobs/${encodeURIComponent(requestedId)}/cancel`,{method:'POST',body:{},signal:life.signal});if(!life.signal.aborted&&job?.id===requestedId){if(result.job){job=result.job;recentJobs.set(id,job);displayJob()}schedule(100)}return result},{success:'Cancellation requested.'})});
  life.on($('#app-check-job',root),'click',()=>{pollCount=0;pollFailures=0;checkJob()});
  life.on($('#app-enabled',root),'click',async event=>{
    const next=!app.enabled;
    const result=await action(event.currentTarget,()=>api(`${appUrl(id)}/enabled`,{method:'POST',body:{enabled:next},signal:life.signal}),{success:next?'App enabled.':'App disabled.'});
    if(result&&!life.signal.aborted){app.enabled=next;$('.page-head .badge',root).outerHTML=appBadge(app);gates();if(job)schedule(100)}
  });
  life.on($('#app-config-form',root),'submit',async event=>{
    event.preventDefault();const form=event.currentTarget;
    const result=await action($('button',form),()=>api(`${appUrl(id)}/config`,{method:'PATCH',body:{config:schemaValues(form,{omitEmpty:false,initial:config})},signal:life.signal}),{output:$('.form-output',form),success:'App settings saved.'});
    if(result&&!life.signal.aborted){config=result.config||config;renderConfigFields();$('.form-output',form).textContent='';$('#app-config-saved',root).textContent='Saved';}
  });
  life.on($('#app-remove',root),'click',async event=>{const result=await action(event.currentTarget,()=>manageApp({operation:'remove',app_id:id},life.signal),{success:'App removed. Saved data is kept; Restore is available in App store.'});if(result&&!life.signal.aborted)location.hash='apps'});
  life.on($('#app-page-open',root),'click',event=>{
    const mount=$('#app-page-mount',root);if(mount.childElementCount){mount.hidden=!mount.hidden;event.currentTarget.textContent=mount.hidden?'Open interactive app':'Hide interactive app';return}
    mount.hidden=false;event.currentTarget.textContent='Hide interactive app';
    const frame=document.createElement('iframe');frame.className='app-custom-page';frame.title=`${app.name||id} dashboard`;frame.setAttribute('sandbox','allow-scripts allow-forms');frame.referrerPolicy='no-referrer';
    mountAppBridge(frame,id,life,{onJob:watchJob,onConfig:next=>{config=next;renderConfigFields()}});
    frame.src=`${appUrl(id)}/page`;mount.append(frame);
  });
  life.on($('#app-dashboard-shortcut',root),'click',()=>{const mount=$('#app-page-mount',root);if(mount.hidden)$('#app-page-open',root).click();mount.closest('section').scrollIntoView({block:'start'})});
  toolFields();gates();renderPermissions();if(job){displayJob();schedule(0)}await refreshPermissions(true);
  if(!life.signal.aborted&&app.dashboard?.page&&app.bundled&&app.enabled)$('#app-page-open',root).click();
}

export function renderResult(target,result,appId,options={}){
  const signature=JSON.stringify([appId,result,options.jobId]);
  if(resultSignatures.get(target)===signature&&target.childElementCount)return;
  const previous=$('[data-app-experience]',target);if(previous)teardownExperience(previous);
  resultSignatures.set(target,signature);
  if(result.permission_required){
    const denied=result.state==='permission_denied';
    const review=result.app&&result.app!==appId?`<a href="#apps/${encodeURIComponent(result.app)}">Review ${esc(title(result.app))} access</a>`:'<button type="button" class="small" data-review-permissions>Review permissions</button>';
    target.innerHTML=`<div class="notice${denied?' error':''}">${esc(result.text||(denied?'The owner has denied this app’s access.':'This app needs the owner’s permission before it can continue.'))}</div><p class="hint section-gap">Changing permissions never restarts a tool. Send a new request when you are ready.</p><div class="actions">${review}</div>`;
    return;
  }
  const items=[];
  items.push('<div data-app-experience></div>');
  if(result.text)items.push(`<p class="app-result-text" data-experience-fallback>${esc(result.text)}</p>`);
  if(result.error)items.push(`<div class="notice error">${esc(typeof result.error==='string'?result.error:pretty(result.error))}</div>`);
  const links=[];
  const addLink=(url,label)=>{
    if(typeof url!=='string'||url.length>8192||links.length>=12)return;
    try{
      const parsed=new URL(url);
      if(!['https:','http:'].includes(parsed.protocol)||parsed.username||parsed.password)return;
      if(!links.some(item=>item.url===parsed.href))links.push({url:parsed.href,label:String(label||parsed.hostname).slice(0,180)});
    }catch{}
  };
  const data=result.data;
  if(data&&typeof data==='object'&&!Array.isArray(data)){
    for(const key of ['url','link','maps_url','html_link','htmlLink','web_link'])addLink(data[key],data.title||data.name||'Open result');
    for(const key of ['event','bookmark'])if(data[key])addLink(data[key].url,data[key].title||data[key].name);
    for(const key of ['results','events','messages','links','bookmarks','attribution']){
      if(Array.isArray(data[key]))for(const row of data[key].slice(0,12)){
        if(row&&typeof row==='object')addLink(row.url||row.link||row.html_link||row.htmlLink||row.maps_url,row.title||row.name||row.address||row.summary||row.subject);
      }
    }
  }
  if(links.length)items.push(`<div class="actions app-result-links">${links.map(link=>`<a href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.label)}</a>`).join('')}</div>`);
  if(result.data!==undefined){
    const rowKey=['forecast','results','events','messages','entries','items','times','bookmarks','habits','ingredients'].find(key=>Array.isArray(data?.[key]));
    const rows=Array.isArray(data)?data:rowKey?data[rowKey]:null;
    if(rows?.length&&rows.every(row=>row&&typeof row==='object'&&!Array.isArray(row))){
      const columns=[...new Set(rows.flatMap(row=>Object.keys(row)))].slice(0,8);
      items.push(`<div class="app-table-wrap" data-experience-fallback><table class="app-data-table"><thead><tr>${columns.map(key=>`<th scope="col">${esc(title(key))}</th>`).join('')}</tr></thead><tbody>${rows.slice(0,14).map(row=>`<tr>${columns.map(key=>`<td>${esc(row[key]===null||row[key]===undefined?'—':typeof row[key]==='object'?JSON.stringify(row[key]):row[key])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
      if(rows.length>14)items.push(`<p class="hint" data-experience-fallback>Showing 14 of ${rows.length} records. Open Result data for the full list.</p>`);
    }
    const resultData=appId==='password-generator'&&Array.isArray(data?.passwords)?{...data,passwords:data.passwords.map(()=> 'Hidden — use Reveal or Copy above')}:result.data;
    items.push(raw('Result data',resultData));
  }
  const artifacts=(Array.isArray(result.artifacts)?result.artifacts:[]).slice(0,8);
  if(artifacts.length)items.push(`<div class="app-artifacts section-gap">${artifacts.map(artifact=>{
    const key=typeof artifact==='string'?artifact:artifact?.id;
    if(typeof key!=='string'||!key)return '';
    const url=`${appUrl(appId)}/artifacts/${encodeURIComponent(key)}`;
    return `<a href="${url}" target="_blank" rel="noopener"><img src="${url}" alt="${esc(artifact?.title||artifact?.description||'App image')}" loading="lazy"><span>${esc(artifact?.title||'Open image')}</span></a>`;
  }).join('')}</div>`);
  target.innerHTML=items.join('')||empty('Request completed.');
  const stage=$('[data-app-experience]',target);
  if(renderExperience(stage,result,appId,options).handled)$$('[data-experience-fallback]',target).forEach(node=>node.remove());
  else if(!result.text&&!result.error&&result.data===undefined&&!artifacts.length)stage.innerHTML=empty('Request completed.');
  const exported=data?.export,extensions={'text/plain':'.txt','text/markdown':'.md','application/json':'.json'};
  if(appId!=='password-generator'&&exported&&extensions[exported.media_type]&&typeof exported.content==='string'&&new TextEncoder().encode(exported.content).length<=65536){
    const button=document.createElement('button');button.type='button';button.className='section-gap';button.textContent='Download brief';
    const name=String(exported.filename||'app-result').replace(/\.(md|txt|json)$/i,'').replace(/[^a-zA-Z0-9_-]/g,'-').slice(0,80)||'app-result';
    button.addEventListener('click',()=>download(name+extensions[exported.media_type],exported.content,exported.media_type));target.append(button);
  }
}
