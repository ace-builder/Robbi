import {$,api,esc,heading,formatTime} from '../core.js';

export function video(root,life){
  root.innerHTML=heading('CAMERA','Video capture','Record a 7-second clip and save it directly on Robbi.')+`
    <section class="panel"><div class="panel-head"><h2>New recording</h2><span class="badge">1080p · 30 fps · MP4</span></div>
    <p class="hint">Uses the full HD camera stream. Video only, without microphone audio. You can leave this tab while recording.</p>
    <div class="actions"><button id="capture-start" disabled>Record 7 seconds</button><button id="capture-stop" disabled>Stop recording</button></div>
    <p id="capture-state" role="status">Connecting…</p><p id="capture-path" class="hint" style="overflow-wrap:anywhere"></p></section>
    <section class="panel section-gap"><h2>Saved clips</h2><div id="capture-list"></div></section>`;
  const find=s=>$(s,root);
  let timer,busy=false,polling=false,signature='',latest=null,requestError='';
  function draw(result){
    latest=result;
    const c=result.current,active=['starting','recording','saving'].includes(c.state);
    find('#capture-start').disabled=busy||active;
    find('#capture-stop').disabled=busy||!active;
    find('#capture-state').textContent=requestError|| (c.state==='recording'?`Recording… ${Math.ceil(c.remaining_s)} seconds left`:
      c.state==='starting'?'Preparing camera for 30 fps…':c.state==='saving'?'Saving video…':c.state==='failed'?`Recording failed: ${c.error}`:
      c.state==='saved'?(c.outcome==='cancelled'?'Stopped. Partial clip saved.':'Saved on Robbi. Ready for another clip.'):'Ready to record.');
    find('#capture-path').textContent=`Saved on Pi: ${result.directory}`;
    const next=JSON.stringify(result.clips);
    if(next===signature)return;signature=next;
    find('#capture-list').innerHTML=result.clips.length?result.clips.map(clip=>`<article class="section-gap">
      <strong>${esc(formatTime(clip.created))}</strong><p class="hint">${(clip.duration_ms/1000).toFixed(1)} s · ${(clip.bytes/1048576).toFixed(1)} MB${clip.metadata.width?` · ${clip.metadata.width} × ${clip.metadata.height}`:''}${Number.isFinite(clip.metadata.measured_fps)?` · ${clip.metadata.measured_fps.toFixed(1)} fps`:''}</p>
      ${clip.error?`<p class="notice error">${esc(clip.error)}</p>`:''}
      ${clip.video_url?`<video controls preload="none" playsinline style="width:100%;max-width:640px;background:#111" src="${esc(clip.video_url)}"></video><div class="actions"><a href="${esc(clip.video_url)}?download=1">Download MP4</a></div>`:`<p>${esc(clip.status)}</p>`}
      ${clip.status!=='recording'?`<button data-delete="${esc(clip.id)}">Delete</button>`:''}
    </article>`).join(''):'<p class="empty">No clips yet. Record your first 7-second video.</p>';
  }
  async function poll(){
    clearTimeout(timer);
    if(life.signal.aborted||polling)return;polling=true;
    try{const result=await api('/api/video-capture',{signal:life.signal,timeout:5000});if(!life.signal.aborted)draw(result)}
    catch(e){if(!life.signal.aborted){find('#capture-state').textContent=e.message;find('#capture-start').disabled=true}}
    finally{polling=false;if(!life.signal.aborted)timer=setTimeout(poll,document.hidden?3000:['starting','recording','saving'].includes(latest?.current?.state)?500:2000)}
  }
  async function write(path,method){
    if(busy)return;busy=true;requestError='';if(latest)draw(latest);
    try{await api(path,{method,body:{},timeout:5000})}
    catch(e){requestError=e.message;if(!life.signal.aborted)find('#capture-state').textContent=e.message}
    finally{busy=false;if(!life.signal.aborted)poll()}
  }
  life.on(find('#capture-start'),'click',()=>write('/api/video-capture','POST'));
  life.on(find('#capture-stop'),'click',()=>write('/api/video-capture/stop','POST'));
  life.on(find('#capture-list'),'click',e=>{const b=e.target.closest('[data-delete]');if(b)write('/api/video-capture/'+encodeURIComponent(b.dataset.delete),'DELETE')});
  life.add(()=>clearTimeout(timer));poll();
}
