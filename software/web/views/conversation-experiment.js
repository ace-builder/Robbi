import {api as request, heading} from '../core.js';

export function conversationExperiment(root,life){
root.innerHTML=heading('V2 · opt-in','Conversation Lab','Keep talking while Robbi works in the background.')+`<div class="conversation-experiment"><section class="panel"><div class="actions"><button data-conversation="start">Start conversation</button><button data-conversation="stop" class="secondary">Stop session</button><label class="check"><input data-conversation="microphone" type="checkbox" checked>Robbi’s microphone</label><strong data-conversation="state">Idle</strong><span data-conversation="listening"></span><span data-conversation="spend"></span></div>
<p>Normal V2 wake-word conversations resume when you stop this session. No wake word during this session. Listening continues while background jobs run. Microphone pauses while Robbi speaks to avoid echo; typed messages can interrupt playback.</p>
<label class="check"><input data-conversation="arm" type="checkbox">Enable motor control</label><p class="muted">Use a clear floor. Existing ToF checks and the stop control remain active.</p><p data-conversation="error" role="alert"></p></section>
<section class="panel"><details><summary>Character & memory · <span data-conversation="mood">Loading…</span></summary><p data-conversation="mood-detail"></p><p data-conversation="history-detail"></p><button type="button" data-conversation="reset-mood" class="secondary">Reset mood</button><p class="hint">Mood carries across restarts. Resetting it keeps conversation history and memories.</p></details></section>
<section class="panel"><h2>Conversation</h2><form data-conversation="message-form"><input data-conversation="text" maxlength="1000" placeholder="Ask something, then keep talking…" aria-label="Message"><button>Send</button></form><div data-conversation="events" role="log" aria-live="polite"></div></section>
<section class="panel"><h2>Background jobs</h2><div data-conversation="jobs"><p>No jobs yet.</p></div><p class="muted">Say or type “cancel everything” to cancel tasks. New conversation alone does not cancel a lookup.</p></section>
<section class="panel"><details><summary>Connections & budget</summary><p>Speech runs on your PC. Text models can use OpenRouter or your own compatible local server. Settings apply only to this experiment.</p>
<form data-conversation="settings-form" class="grid two">
<label>PC speech server (use its LAN IP)<input name="speech_url" type="url" required placeholder="http://your-pc:8093"></label>
<label>Text model API<input name="model_url" type="url" required></label>
<label>Fast conversation model<input name="foreground_model" required></label><label>Background model<input name="worker_model" required></label>
<label>Local voice<input name="voice" required></label><label>Speech language<input name="language" required></label>
<label>Cloud budget per session ($)<input name="budget_usd" type="number" step="0.01" min="0.01" max="10" required></label>
<label>End-of-utterance silence (ms)<input name="silence_ms" type="number" step="20" min="300" max="1500" required></label>
<label>Job deadline (seconds)<input name="job_seconds" type="number" min="10" max="180" required></label>
<label>Maximum tools per job<input name="max_calls" type="number" min="1" max="20" required></label>
<button data-conversation="save-settings">Save experiment settings</button></form>
<p class="muted">OpenRouter uses the existing server-side OPENROUTER_API_KEY. Keys are never sent to the browser. Custom local endpoints use ROBBI_CONTINUOUS_LOCAL_KEY if needed. Changing settings does not download or start a model.</p></details></section>
<section class="panel"><details><summary>Latency & diagnostics</summary><pre data-conversation="diagnostics"></pre></details></section>
</div>`;
const $=name=>root.querySelector(`[data-conversation="${name}"]`);
let last=0,jobsRevision="",timer,polling=false,submitting=false,diagnostics=[];
const api=(path,body)=>request('/api/conversation/'+path,{method:body===undefined?'GET':'POST',body,signal:life.signal,timeout:6000});
function action(fn){return async e=>{try{$('error').textContent='';await fn(e);if(!life.signal.aborted)await poll()}catch(err){if(!life.signal.aborted)$('error').textContent=err.message}}}
life.on($('start'),'click',action(()=>api('start',{microphone:$('microphone').checked})));
life.on($('stop'),'click',action(()=>api('stop',{})));
life.on($('reset-mood'),'click',action(()=>api('mood/reset',{})));
life.on($('arm'),'change',action(()=>api('arm',{enabled:$('arm').checked})));
life.on($('message-form'),'submit',action(async e=>{e.preventDefault();if(submitting)return;const text=$('text').value;submitting=true;$('text').value='';try{await api('text',{text})}catch(error){if(!$('text').value)$('text').value=text;throw error}finally{submitting=false}}));
life.on($('settings-form'),'submit',action(async e=>{e.preventDefault();const data=Object.fromEntries(new FormData(e.target));for(const k of ['budget_usd','silence_ms','job_seconds','max_calls'])data[k]=Number(data[k]);await api('settings',data);$('error').textContent='Settings saved.'}));
async function poll(){clearTimeout(timer);if(polling||document.hidden||life.signal.aborted)return;polling=true;try{
const d=await api('status?since='+last+'&jobs_revision='+encodeURIComponent(jobsRevision));if(life.signal.aborted)return;
if(d.events_reset){last=0;diagnostics=[];$('events').replaceChildren()}
jobsRevision=d.jobs_revision||'';
const mood=d.mood||{};$('mood').textContent=mood.mood||'unavailable';$('reset-mood').disabled=!mood.enabled;
$('mood-detail').textContent=(mood.style||'')+(mood.rapport!==undefined?' Rapport: '+mood.rapport+'/100.':'')+(mood.persistence_error?' '+mood.persistence_error:'');
$('history-detail').textContent=d.history?`${d.history.retained} / ${d.history.limit} messages saved locally. ${d.history.error||''}`:'';
const active=['starting','live','stopping'].includes(d.state),live=d.state==='live';
$('state').textContent=d.state;$('start').disabled=active;$('stop').disabled=!active;$('microphone').disabled=active;if(active)$('microphone').checked=d.microphone;$('arm').disabled=!live;$('arm').checked=d.armed;$('text').disabled=!live;$('message-form').querySelector('button').disabled=!live;
for(const control of $('settings-form').elements)control.disabled=active;
$('listening').textContent=d.listening?'● Listening':live?(d.microphone?'Microphone paused during output':'Typed conversation'):'';
$('spend').textContent=d.usage.budget_usd!==undefined?`$${Number(d.usage.reserved_or_charged_usd||0).toFixed(4)} / $${Number(d.usage.budget_usd).toFixed(2)}`:'';
for(const e of d.events){if(e.id<=last)continue;last=e.id;if(['you','robbi','error'].includes(e.kind)){const row=document.createElement('div');row.className='event '+e.kind;const title=document.createElement('strong');title.textContent=e.kind+' · '+new Date(e.time*1000).toLocaleTimeString();row.append(title,document.createTextNode(e.text));$('events').append(row);$('events').scrollTop=$('events').scrollHeight;}else diagnostics.push(e.kind+': '+e.text)}
while($('events').children.length>100)$('events').firstChild.remove();diagnostics=diagnostics.slice(-60);$('diagnostics').textContent=diagnostics.join('\n');
if(d.conversation_jobs){
const expanded=new Set([...$('jobs').querySelectorAll('article')].filter(row=>row.querySelector('details')?.open).map(row=>row.dataset.job));
$('jobs').replaceChildren(...(d.conversation_jobs||[]).map(j=>{const row=document.createElement('article'),title=document.createElement('strong'),text=document.createElement('p');row.dataset.job=j.id;title.textContent=`${j.state} · ${j.calls} tool calls · ${j.id}`;text.textContent=j.request;row.append(title,text);if(j.result){const result=document.createElement('p');result.textContent=j.result;row.append(result)}if(j.evidence?.length){const detail=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');detail.open=expanded.has(j.id);summary.textContent='Actual tool results';pre.textContent=JSON.stringify(j.evidence,null,2);detail.append(summary,pre);row.append(detail)}return row}));
if(!$('jobs').children.length)$('jobs').textContent='No background jobs yet.';
}
}catch(err){if(!life.signal.aborted){$('error').textContent=err.message;$('start').disabled=true;$('arm').disabled=true}}finally{polling=false;if(!document.hidden&&!life.signal.aborted)timer=setTimeout(poll,1000)}}
api('settings').then(data=>{if(life.signal.aborted)return;for(const [k,v] of Object.entries(data)){const el=$('settings-form').elements.namedItem(k);if(el)el.value=v}}).catch(e=>{if(!life.signal.aborted)$('error').textContent=e.message});
life.on(document,'visibilitychange',()=>{clearTimeout(timer);if(!document.hidden)poll()});life.add(()=>clearTimeout(timer));poll();
}
