import {$,api,esc,issue,formatTime} from '../core.js';

export function mountMorningBriefing(root,life){
  const panel=document.createElement('section');panel.className='panel section-gap';panel.id='morning-briefing';
  panel.innerHTML=`<div class="panel-head"><div><h2>Morning briefing</h2><p class="muted">A short spoken update at the time you choose.</p></div><button type="button" id="briefing-toggle" disabled>Loading…</button></div>
    <p id="briefing-status" class="hint" role="status"></p>
    <form id="briefing-settings"><div class="form-grid"><label>Briefing time<input name="time" type="time" required value="08:00"></label><label>Timezone<input name="timezone" required maxlength="80" placeholder="Europe/Berlin" value="Europe/Berlin"></label></div>
    <fieldset class="section-gap"><legend>Include in the briefing</legend><div class="form-grid">${[['weather','Weather'],['calendar','Today’s appointments'],['habits','Habits'],['mail','Unread email summaries']].map(([key,label])=>`<label class="check"><input type="checkbox" name="${key}"> ${label}</label>`).join('')}</div><p class="hint">Email summaries include senders and subjects and will be read aloud. Each app keeps its existing access permissions.</p></fieldset>
    <div class="actions"><button type="submit">Save schedule</button><button type="button" id="briefing-preview">Preview</button><button type="button" id="briefing-run">Read now</button></div></form>
    <div id="briefing-feedback" class="section-gap" role="status" aria-live="polite"></div><div id="briefing-result" class="section-gap" aria-live="polite"></div>`;
  $('.grid',root)?.insertAdjacentElement('afterend',panel)||root.append(panel);
  let snapshot=null,timer=null,busy=false,jobId=null,failures=0;
  const form=$('#briefing-settings',panel),feedback=$('#briefing-feedback',panel),output=$('#briefing-result',panel);
  const controls=()=>{$('#briefing-toggle',panel).disabled=!snapshot||busy;for(const id of ['briefing-preview','briefing-run'])$('#'+id,panel).disabled=busy||!!jobId||snapshot?.running};
  function render(value,fill=false){
    snapshot=value;const config=value.config;
    $('#briefing-toggle',panel).textContent=config.enabled?'Turn off':'Turn on';
    $('#briefing-status',panel).textContent=(config.enabled?`On · ${config.time} · ${config.timezone}`:'Off · runs only when you request it')+(value.running?' · Preparing briefing…':value.next_run?` · Next: ${formatTime(value.next_run)}`:'')+(value.error?` · ${value.error}`:'');
    if(fill){form.elements.time.value=config.time;form.elements.timezone.value=config.timezone;for(const key of ['weather','calendar','habits','mail'])form.elements[key].checked=!!config.sections[key]}
    if(value.last_result)showResult(value.last_result);controls();
  }
  function showResult(result){
    output.innerHTML=`<p class="app-result-text">${esc(result.text||'Briefing completed.')}</p>${result.delivery_error?`<p class="notice error">${esc(result.delivery_error)}</p>`:''}${result.sections?.length?`<details><summary>Briefing sections</summary><ul>${result.sections.map(section=>`<li><strong>${esc(section.title)}</strong>: ${esc(section.text)}${section.permission_required?` <a href="#apps/${encodeURIComponent(section.app)}">Review access</a>`:''}</li>`).join('')}</ul></details>`:''}`;
  }
  async function refresh(fill=false){try{const value=await api('/api/morning-briefing',{signal:life.signal});if(!life.signal.aborted)render(value,fill)}catch(error){if(!life.signal.aborted)issue(feedback,error)}}
  async function save(patch,fill){busy=true;controls();feedback.textContent='Saving…';try{const result=await api('/api/morning-briefing',{method:'PATCH',body:patch,signal:life.signal});if(life.signal.aborted)return;render(result,fill);feedback.textContent='Saved.'}catch(error){if(!life.signal.aborted)issue(feedback,error)}finally{busy=false;if(!life.signal.aborted)controls()}}
  life.on($('#briefing-toggle',panel),'click',()=>{if(snapshot&&!busy)save({enabled:!snapshot.config.enabled},false)});
  life.on(form,'submit',event=>{event.preventDefault();if(busy)return;save({time:form.elements.time.value,timezone:form.elements.timezone.value.trim(),sections:Object.fromEntries(['weather','calendar','habits','mail'].map(key=>[key,form.elements[key].checked]))},true)});
  async function poll(){
    if(life.signal.aborted||!jobId)return;
    try{const {job}=await api('/api/morning-briefing/jobs/'+encodeURIComponent(jobId),{signal:life.signal});if(life.signal.aborted)return;failures=0;if(['done','failed','cancelled'].includes(job.state)){jobId=null;if(job.result){showResult(job.result);feedback.textContent=job.result.cancelled?'Briefing cancelled.':job.result.preview?'Preview ready.':job.result.delivered?'Briefing read aloud.':'Briefing finished; see the result below.'}else issue(feedback,new Error(job.error||'The briefing did not finish.'));await refresh();controls();return}}catch(error){if(life.signal.aborted)return;failures++;issue(feedback,error);if(failures>=3){jobId=null;controls();return}}
    timer=setTimeout(poll,1000);
  }
  async function run(preview){if(busy||jobId)return;busy=true;controls();feedback.textContent=preview?'Preparing preview…':'Preparing briefing…';try{const {job}=await api('/api/morning-briefing/run',{method:'POST',body:{preview},signal:life.signal});if(life.signal.aborted)return;jobId=job.id;failures=0;poll()}catch(error){if(!life.signal.aborted)issue(feedback,error)}finally{busy=false;if(!life.signal.aborted)controls()}}
  life.on($('#briefing-preview',panel),'click',()=>run(true));life.on($('#briefing-run',panel),'click',()=>run(false));
  const statusTimer=setInterval(()=>{if(!busy&&!jobId)refresh()},15000);
  life.add(()=>{clearTimeout(timer);clearInterval(statusTimer)});refresh(true);
}
