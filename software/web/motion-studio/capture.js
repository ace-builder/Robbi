// Physical demonstrations stay separate from the draft until explicitly imported.
export function createCapture({api,getProject,onLive,onImport,notify}){
  const panel=document.createElement('section');panel.id='capture-panel';panel.className='capture-panel';panel.hidden=true;panel.setAttribute('aria-label','Record Robbi');
  panel.innerHTML=`<div class="capture-heading"><div><span class="eyebrow">TEACH BY MOVING</span><h2>Record Robbi</h2></div><button id="capture-close" class="icon-button" aria-label="Close recording panel">×</button></div>
    <p class="capture-intro">Roll Robbi gently on a flat surface. His IMU records turns; wheel encoders measure forward and backward travel. Motors stay disabled while recording.</p>
    <p class="capture-limit">Carrying or sliding Robbi cannot measure distance. IMU turns mode keeps the robot in one place. Tilt is shown live and kept in the raw take; editable animations stay on the floor. Relative heading can drift; calibrate while still before recording.</p>
    <div id="capture-setup"><label>Take name<input id="capture-name" maxlength="80" value="Recorded movement"></label><label>Record mode<select id="capture-mode"><option value="wheels">Rolling path · IMU + encoders</option><option value="imu">Turns only · IMU</option></select></label></div>
    <p id="capture-readiness" role="status">Checking Robbi’s sensors…</p>
    <div class="capture-buttons"><button id="capture-calibrate">Calibrate IMU</button><button id="capture-refresh" aria-label="Refresh recording sensors">↻ Refresh</button><button id="capture-start" class="primary">● Start recording</button><button id="capture-stop" class="danger-button" hidden>■ Stop & keep take</button></div>
    <div id="capture-live" hidden><div class="capture-clock"><span class="record-dot"></span><strong id="capture-time">0.0s</strong><span>/ 60s</span></div><p id="capture-pose"></p><p class="muted">Start here defines (0, 0) and the forward direction. Keep this tab connected. Stop preserves the raw take.</p></div>
    <div id="capture-review"><label>Path detail<select id="capture-detail"><option value=".5">Fine · 0.5 cm</option><option value="1.5" selected>Balanced · 1.5 cm</option><option value="4">Simple · 4 cm</option><option value="10">Coarse · 10 cm</option></select></label><p class="muted">Create editable waypoints with reverse travel, turns, speed estimates, and pauses. Playback eases and stops at waypoints, so timing may differ. Add faces, speech, sound, images, or video after importing.</p><div class="capture-list-heading"><span class="eyebrow">SAVED SENSOR TAKES</span><button id="capture-reload" class="text-button">Refresh</button></div><div id="capture-takes"></div></div>`;
  document.body.append(panel);
  const $=s=>panel.querySelector(s);
  let current=null,capabilities={},takes=[],timer,busy=false,polling=false,visible=false,live=false,disposed=false,revision=0;
  const active=()=>current?.state==='recording';
  function render(){
    const recording=active();
    $('#capture-setup').hidden=recording;$('#capture-live').hidden=!recording;$('#capture-review').hidden=recording;
    $('#capture-stop').hidden=!recording;$('#capture-start').hidden=recording;$('#capture-calibrate').hidden=recording;$('#capture-refresh').hidden=recording;
    $('#capture-close').disabled=recording||busy;$('#capture-stop').disabled=busy;
    $('#capture-start').disabled=busy||!capabilities[$('#capture-mode').value];$('#capture-calibrate').disabled=busy||!capabilities.can_calibrate;
    $('#capture-readiness').textContent=recording?`${current.mode==='imu'?'IMU turns':'Rolling path'} · motor output locked`:[...new Set([current?.reason,capabilities.reason].filter(Boolean))].join(' ')||'Checking Robbi’s sensors…';
    panel.classList.toggle('recording',recording);
    if(recording){const p=current.pose;$('#capture-time').textContent=p.t.toFixed(1)+'s';$('#capture-pose').textContent=`X ${p.x.toFixed(1)} cm · Y ${p.y.toFixed(1)} cm · Heading ${p.heading.toFixed(0)}°\nTilt ${p.roll.toFixed(0)}° / ${p.pitch.toFixed(0)}° · ${current.count} samples`;}
    $('#capture-takes').replaceChildren();
    if(!takes.length){const empty=document.createElement('p');empty.className='muted';empty.textContent='Your recorded takes will appear here. Open this editor on Robbi to capture a movement.';$('#capture-takes').append(empty);}
    for(const take of takes){
      const row=document.createElement('div');row.className='capture-take';
      const label=document.createElement('strong');label.textContent=take.name;
      const meta=document.createElement('small');meta.textContent=`${take.duration.toFixed(1)}s · ${take.mode==='wheels'?'Rolling path':'IMU turns'}${take.state==='interrupted'?' · partial take':''}`;
      const buttons=document.createElement('div');buttons.className='capture-buttons';
      const use=document.createElement('button');use.textContent='Edit as animation';use.disabled=busy;use.addEventListener('click',()=>run(async()=>{const result=await api(`/capture/takes/${take.id}/convert`,{tolerance:Number($('#capture-detail').value)});onImport(result);panel.hidden=true;visible=false;notify('Saved to the device library and opened for editing. The raw take is kept.');}));
      const raw=document.createElement('a');raw.href=`/api/motion-studio/capture/takes/${take.id}`;raw.download=`${take.id}.robbi-take.json`;raw.textContent='Raw data ↓';raw.className='text-button';
      const remove=document.createElement('button');remove.textContent='×';remove.className='icon-button';remove.setAttribute('aria-label',`Delete take ${take.name}`);remove.disabled=busy;remove.addEventListener('click',()=>run(async()=>{await api(`/capture/takes/${take.id}`,{},'DELETE');await refresh();}));
      buttons.append(use,raw,remove);row.append(label,meta,buttons);$('#capture-takes').append(row);
    }
  }
  function accept(state){
    const wasLive=live;current=state;live=active();
    if(live){visible=true;panel.hidden=false;onLive(current);}
    else if(wasLive){onLive(null);notify(state.reason||'Raw take saved.',state.state==='interrupted'||state.state==='unsaved');}
    render();
  }
  async function refresh(){
    const requested=revision,result=await api('/capture');if(requested!==revision||disposed)return;capabilities=result.capabilities;takes=result.takes;
    if(result.current.state==='unsaved'&&!takes.some(t=>t.id===result.current.id))takes.unshift({...result.current,duration:result.current.pose.t});
    accept(result.current);schedule();
  }
  function schedule(){clearTimeout(timer);if(!disposed&&(active()||visible))timer=setTimeout(poll,active()?200:2000);}
  async function poll(){
    if(polling||disposed)return;polling=true;
    const requested=revision;
    try{if(active()){const result=await api('/capture/poll',{id:current.id});if(requested!==revision||disposed)return;accept(result);if(!active())await refresh();}else await refresh();}
    catch(error){if(requested===revision){$('#capture-readiness').textContent='Connection lost: '+error.message+' Robbi keeps a partial take after 5 seconds.';await refresh().catch(()=>{});}}
    finally{polling=false;schedule();}
  }
  async function run(operation){if(busy)return;revision++;busy=true;render();try{await operation();}catch(error){notify(error.message,true);}finally{busy=false;render();schedule();}}
  $('#capture-mode').addEventListener('change',render);
  $('#capture-close').addEventListener('click',()=>{if(active())return;panel.hidden=true;visible=false;clearTimeout(timer);});
  $('#capture-start').addEventListener('click',()=>run(async()=>{
    const name=$('#capture-name').value.trim();if(!name)throw Error('Give the take a name first.');
    try{accept(await api('/capture',{name,mode:$('#capture-mode').value,settings:getProject().settings}));}
    catch(error){await refresh().catch(()=>{});throw error;}
  }));
  $('#capture-stop').addEventListener('click',()=>run(async()=>{accept(await api('/capture/stop',{id:current.id}));await refresh();}));
  $('#capture-calibrate').addEventListener('click',()=>run(async()=>{const result=await api('/capture/calibrate',{});notify(result.message||'Keep Robbi completely still for 3 seconds.');current=null;await refresh();}));
  for(const id of ['capture-refresh','capture-reload'])$('#'+id).addEventListener('click',()=>run(refresh));
  // Reattach after a reload without losing a live take or releasing its motor lock.
  refresh().catch(()=>{});
  return {
    active,
    open(){visible=true;panel.hidden=false;run(refresh);},
    dispose(){disposed=true;clearTimeout(timer);if(active())navigator.sendBeacon('/api/motion-studio/capture/stop',new Blob([JSON.stringify({id:current.id})],{type:'application/json'}));},
  };
}
