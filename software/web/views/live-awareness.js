import {$,api,esc,state} from '../core.js';

export function liveIndicator(){
  const link=document.createElement('a');
  link.href='#overview';link.className='badge';link.hidden=true;
  link.setAttribute('aria-label','Cloud live awareness active; open controls');
  $('.top-actions')?.prepend(link);
  document.addEventListener('robbi:status',()=>{
    const live=state.status?.live_awareness;
    link.hidden=!live?.running;
    link.textContent=live?.state==='streaming'?'● Cloud awareness on':'● Cloud awareness '+(live?.state||'');
  });
}

export function mountLiveAwareness(root,life){
  root.innerHTML=`<div class="panel-head"><h2>Live awareness <small class="hint">Experimental</small></h2><span class="badge" data-live-state>Off</span></div>
    <p class="muted">Let Robbi notice changes through a cloud model. Defaults to OpenAI GPT-6.1 Terra for camera awareness. Your conversation model and ElevenLabs voice stay in charge.</p>
    <form data-live-form><div class="grid three">
      <label>Provider<select name="provider"><option value="openai">OpenAI · camera awareness</option><option value="gemini">Google Gemini · live camera + audio</option></select></label>
      <label>Model<input name="model" maxlength="120" value="gpt-6.1-terra" required autocomplete="off"></label>
      <label>Session length (seconds)<input name="duration_s" type="number" min="15" max="300" step="1" value="60" required></label>
    </div><div><label class="check"><input name="video" type="checkbox" checked> Camera · sampled frames</label><label class="check"><input name="audio" type="checkbox" disabled> Microphone · Gemini Live only</label></div><label class="check section-gap"><input name="react" type="checkbox"> React to greetings, questions and meaningful changes (voice + face)</label>
    <label class="check"><input name="gestures" type="checkbox"> Allow small in-place body gestures · clear, supported surface</label>
    <p class="hint">Selected camera/microphone data goes to the chosen provider while this timed session runs and may incur API charges. No raw media is saved here. Reactions use your autonomy settings and daily limits. Normal wake-word conversations still work.</p>
    <div class="actions"><button type="submit" data-live-start disabled>Start experiment</button><button type="button" data-live-stop disabled>Stop streaming</button><a href="#settings">Configure API keys →</a></div></form>
    <p class="hint" data-live-message role="status">Loading…</p><p class="hint" data-live-metrics></p><p class="hint" data-live-reaction role="status"></p>
    <details><summary>What Robbi noticed</summary><div data-live-observations><p class="hint">No observations yet.</p></div></details>`;
  const form=$('[data-live-form]',root),start=$('[data-live-start]',root),stop=$('[data-live-stop]',root),message=$('[data-live-message]',root);
  let snapshot=null,busy=false,timer=null,loading=false,revision=0,feedback='';
  function render(){
    if(life.signal.aborted)return;
    const running=!!snapshot?.running;
    if(running&&snapshot.options){
      for(const key of ['provider','model','duration_s'])form.elements[key].value=snapshot.options[key];
      for(const key of ['audio','video','react','gestures'])form.elements[key].checked=!!snapshot.options[key];
    }
    const provider=form.elements.provider.value,availability=snapshot?.providers?.[provider];
    start.disabled=busy||running||!availability?.key_configured||!availability?.dependency_available;
    stop.disabled=busy||!running;
    for(const input of form.querySelectorAll('input,select'))input.disabled=busy||running;
    if(provider==='openai'){form.elements.audio.checked=false;form.elements.audio.disabled=true;form.elements.video.checked=true;form.elements.video.disabled=true}
    form.elements.gestures.disabled=busy||running||!form.elements.react.checked;
    if(!form.elements.react.checked)form.elements.gestures.checked=false;
    $('[data-live-reaction]',root).textContent=snapshot?.reaction?.reason||'';
    $('[data-live-state]',root).textContent=running?`${snapshot.state} · ${snapshot.remaining_s}s left`:(snapshot?.state||'Off');
    const warning=!availability?.dependency_available?`Install the ${provider==='openai'?'providers':'live'} extra on Robbi.`:!availability?.key_configured?`Save your ${provider==='openai'?'OpenAI':'Gemini'} API key in Settings → Connections first.`:'';
    message.textContent=feedback||warning||snapshot?.reason||'';
    const heartbeat=snapshot?.autonomy?.heartbeat||{},policy=snapshot?.autonomy?.policy||{};
    $('[data-live-metrics]',root).textContent=snapshot?.options?
      `${snapshot.frames||0} frames · ${Math.round((snapshot.audio_bytes||0)/1024)} KiB audio · ${snapshot.turns||0}/20 observation windows · ${snapshot.latency_ms==null?'Awaiting cloud response':snapshot.latency_ms+' ms cloud response after window'}${snapshot.options.audio?' · '+(snapshot.audio_status||'Awaiting microphone'):''}${snapshot.options.react?' · Reactions: '+(policy.paused?'paused':heartbeat.enabled===false?'disabled in Settings':heartbeat.last_reason||'waiting for meaningful evidence'):''}`:'';
    const rows=snapshot?.observations||[];
    $('[data-live-observations]',root).innerHTML=rows.length?rows.slice().reverse().map(row=>`<p><strong>${esc(row.salience)}</strong> · ${esc(row.summary)} ${row.changed?'':'(unchanged)'}</p>`).join(''):'<p class="hint">No observations yet. Speech and camera content are evidence, not commands.</p>';
  }
  async function load(){
    if(loading||busy||life.signal.aborted)return;
    loading=true;
    const version=revision;
    try{const result=await api('/api/live-awareness',{signal:life.signal});if(!life.signal.aborted&&!busy&&version===revision){snapshot=result;render()}}
    catch(error){if(!life.signal.aborted)message.textContent=error.message}
    finally{loading=false}
  }
  async function mutate(operation,body){
    if(busy)return;
    busy=true;revision++;feedback='';render();
    try{const result=await api('/api/live-awareness/'+operation,{method:'POST',body,signal:life.signal});if(!life.signal.aborted){snapshot=result;render()}}
    catch(error){if(!life.signal.aborted){busy=false;feedback=error.message;render();return}}
    finally{busy=false;if(!life.signal.aborted)render()}
    if(!life.signal.aborted){render();load()}
  }
  life.on(form.elements.provider,'change',()=>{form.elements.model.value=snapshot?.providers?.[form.elements.provider.value]?.default_model||(form.elements.provider.value==='openai'?'gpt-6.1-terra':'gemini-3.8-live');feedback='';render()});
  life.on(form.elements.react,'change',()=>render());
  life.on(form,'submit',event=>{event.preventDefault();const fields=form.elements;mutate('start',{provider:fields.provider.value,model:fields.model.value.trim(),duration_s:Number(fields.duration_s.value),audio:fields.audio.checked,video:fields.video.checked,react:fields.react.checked,gestures:fields.gestures.checked})});
  life.on(stop,'click',()=>mutate('stop',{}));
  async function poll(){await load();if(!life.signal.aborted)timer=setTimeout(poll,2500)}
  life.add(()=>clearTimeout(timer));poll();
}
