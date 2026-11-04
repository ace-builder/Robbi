import {$,$$,api,esc,heading,action,issue,empty} from '../core.js';

export async function wake(root,life){
  root.innerHTML=heading('MAKE ROBBI HEAR YOU','Wake word.','Record on Robbi’s microphone. Train a personal detector in this browser, then test it in your room.')+`
  <div class="notice">Personal training calibrates Robbi’s lightweight MFCC/DTW detector. It does not train a neural model. Record at least 4 wake phrases, 3 ordinary phrases and 2 noise clips; varied distances and tones help.</div>
  <section class="panel section-gap"><h2>1. Record examples</h2><p id="wake-engine" class="muted"></p>
    <form id="wake-capture"><div class="form-grid"><label>Example type<select name="category"><option value="positive">Positive — “Hey Robbi”</option><option value="negative">Negative — ordinary / similar-sounding speech</option><option value="noise">Noise — room sounds, no wake phrase</option></select></label><label>Duration (seconds)<input name="duration" type="number" min="1" max="4" step="0.5" value="3"></label><label>Label (optional)<input name="sample_name" maxlength="64" placeholder="Quiet, across the room…"></label></div>
    <p class="muted">After clicking Record, wait two seconds, then speak once. For noise clips, leave the microphone listening to the room. Recordings stay on Robbi.</p>
    <div class="actions"><button type="submit">Record example</button><button type="button" id="wake-reload" class="secondary">Refresh</button></div><p id="wake-capture-status" role="status" aria-live="polite"></p></form>
    <div id="wake-dataset"></div>
  </section>
  <section class="panel section-gap"><h2>2. Train & review</h2><p>Robbi prepares compact audio features. Your browser performs the matching and fits a threshold, checking ordinary speech and noise for false triggers. Keep this tab open.</p>
    <div class="actions"><button id="wake-train">Train in this browser</button><button id="wake-cancel" class="secondary" hidden>Cancel</button><button id="wake-apply" disabled>Apply calibration</button></div>
    <p id="wake-training-status" role="status" aria-live="polite">Existing wake settings stay active until you apply a new calibration.</p><progress id="wake-progress" max="100" value="0" hidden></progress>
    <div id="wake-review"></div>
  </section>
  <section class="panel section-gap"><h2>3. Test in the room</h2><p>After applying, say “Hey Robbi” several times from different distances. Then try unrelated speech and background noise. Poor results need more representative examples; a small dataset cannot guarantee recognition.</p><a href="#interact">Open Interact to check recognized speech</a></section>
  <details class="setting-group section-gap"><summary>Advanced: neural wake models</summary><div class="details-body"><p>NanoWakeWord training is a separate computer/GPU workflow. Export your private recordings, train externally, then upload the ONNX model. Check Settings → Voice before replacing a model: uploading reloads it if the active neural detector uses that file.</p><div class="actions"><a class="button secondary" href="/wake_export.zip">Export private training dataset</a></div><form id="wake-onnx"><label>Trained model<input name="file" type="file" accept=".onnx" required></label><button type="submit">Upload ONNX model</button></form></div></details>`;
  let worker=null,job=null,calibration=null,run=0;
  const status=text=>$('#wake-training-status').textContent=text;
  async function refresh(){try{const snapshot=await api('/wake_refs',{signal:life.signal});if(life.signal.aborted)return;$('#wake-engine').textContent=`Selected engine: ${snapshot.engine?.configured||'unknown'} · active: ${snapshot.engine?.active||'unavailable'}`;const ds=snapshot.datasets||{};$('#wake-dataset').innerHTML=['positive','negative','noise'].map(category=>`<details class="section-gap"><summary>${category[0].toUpperCase()+category.slice(1)} · ${(ds[category]||[]).length} recordings</summary><div class="details-body">${(ds[category]||[]).map(s=>`<div class="wake-sample"><div class="panel-head"><strong>${esc(s.name)}</strong><button class="small danger" data-remove="${esc(s.name)}" data-category="${category}">Delete</button></div><audio controls preload="none" src="/wake_refs/${category}/${encodeURIComponent(s.name)}" aria-label="${esc(s.name)}"></audio></div>`).join('')||empty('No recordings yet.')}</div></details>`).join('')}catch(e){if(!life.signal.aborted)issue($('#wake-dataset'),e)}}
  function invalidate(){calibration=null;$('#wake-apply').disabled=true;$('#wake-review').textContent=''}
  life.on($('#wake-reload'),'click',refresh);
  life.on($('#wake-capture'),'submit',async e=>{
    e.preventDefault();invalidate();$('#wake-capture-status').textContent='Get ready for two seconds, then speak…';
    const form=new FormData(e.currentTarget),result=await action($('button[type=submit]',e.currentTarget),()=>api('/wake_refs/record',{method:'POST',body:{category:form.get('category'),duration:Number(form.get('duration')),sample_name:form.get('sample_name'),prep_delay_s:2},timeout:15000}),{success:'Example recorded.'});
    if(!life.signal.aborted){$('#wake-capture-status').textContent=result?'Saved. Listen below to check your recording.':'Recording failed. Check microphone health in Overview.';await refresh()}
  });
  life.on($('#wake-dataset'),'click',async e=>{const button=e.target.closest('[data-remove]');if(!button||!confirm('Delete this wake recording?'))return;invalidate();await action(button,()=>api(`/wake_refs/${button.dataset.category}/${encodeURIComponent(button.dataset.remove)}`,{method:'DELETE'}),{success:'Recording deleted.'});if(!life.signal.aborted)await refresh()});
  async function cancel(){run++;worker?.terminate();worker=null;const old=job;job=null;if(old)api(`/api/wake-training/${old}/cancel`,{method:'POST',body:{}}).catch(()=>{});$('#wake-cancel').hidden=true;$('#wake-train').disabled=false;$('#wake-progress').hidden=true;invalidate();status('Cancelled. Your existing detector is unchanged.')}
  life.on($('#wake-cancel'),'click',cancel);
  life.add(()=>{run++;worker?.terminate();if(job)api(`/api/wake-training/${job}/cancel`,{method:'POST',body:{}}).catch(()=>{})});
  life.on($('#wake-train'),'click',async()=>{
    const current=++run;invalidate();$('#wake-train').disabled=true;$('#wake-cancel').hidden=false;$('#wake-progress').hidden=false;status('Preparing audio features on Robbi…');
    try{
      const receipt=await api('/api/wake-training/prepare',{method:'POST',body:{}});if(life.signal.aborted||current!==run){api(`/api/wake-training/${receipt.id}/cancel`,{method:'POST',body:{}}).catch(()=>{});return}job=receipt.id;
      while(current===run&&!life.signal.aborted){
        const progress=await api(`/api/wake-training/${job}`,{signal:life.signal});if(current!==run||life.signal.aborted)return;
        if(progress.state==='failed'||progress.state==='cancelled')throw Error(progress.error||'Preparation cancelled');
        status(`Preparing on Robbi: ${progress.done} / ${progress.total||'…'} examples`);$('#wake-progress').value=progress.total?40*progress.done/progress.total:0;
        if(progress.state==='done')break;await new Promise(resolve=>setTimeout(resolve,700));
      }
      if(current!==run||life.signal.aborted)return;
      const features=await api(`/api/wake-training/${job}/features`,{timeout:30000,signal:life.signal});if(current!==run||life.signal.aborted)return;
      status('Training in your browser…');worker=new Worker('/static/wake-worker.js',{type:'module'});
      const result=await new Promise((resolve,reject)=>{worker.onmessage=e=>{if(e.data.type==='progress'){status(`Browser matching: ${e.data.done} / ${e.data.total} examples`);$('#wake-progress').value=40+60*e.data.done/e.data.total}else if(e.data.type==='done')resolve(e.data.result);else reject(Error(e.data.error))};worker.onerror=()=>reject(Error('Browser training failed. Try a recent browser.'));worker.postMessage(features)});
      if(current!==run||life.signal.aborted)return;worker.terminate();worker=null;calibration=result;
      status(result.message);$('#wake-review').textContent=`Held-out/leave-one-out clip checks: ${result.hits}/${result.positives} wake phrases, ${result.falseHits}/${result.negatives} false triggers. Preparation: ${features.prepare_seconds.toFixed(1)} s on Robbi; matching: ${result.browser_seconds.toFixed(1)} s in browser. ${features.note}`;
      $('#wake-apply').disabled=!result.valid;$('#wake-progress').value=100;
    }catch(e){if(current===run&&!life.signal.aborted){status(e.message);worker?.terminate();worker=null}}
    finally{if(current===run&&!life.signal.aborted){$('#wake-train').disabled=false;$('#wake-cancel').hidden=true}}
  });
  life.on($('#wake-apply'),'click',async e=>{if(!calibration?.valid)return;const result=await action(e.currentTarget,()=>api(`/api/wake-training/${job}/apply`,{method:'POST',body:{threshold:calibration.threshold,fingerprint:calibration.fingerprint},timeout:20000}),{success:'Personal wake calibration applied.'});if(result&&!life.signal.aborted){status(result.message);invalidate();await refresh()}});
  life.on($('#wake-onnx'),'submit',e=>{e.preventDefault();action($('button',e.currentTarget),()=>api('/wake_model',{method:'POST',body:new FormData(e.currentTarget),timeout:30000}),{success:'Model uploaded. Check the active engine and test recognition in Settings → Voice.'})});
  await refresh();
}
