import {$,api,heading} from '../core.js';
import {AttitudeTracker,angles} from '../imu/orientation.mjs';
import {mountCalibration} from '../imu/calibration.js';

export async function imu(root,life){
  root.innerHTML=heading('ORIENTATION & SENSORS','Robbi, in motion.','A live view of Robbi’s tilt and rotation, straight from its IMU.')+`
    <div class="imu-layout"><section class="panel imu-panel">
      <div class="panel-head"><h2>Live 3D</h2><span class="badge" id="imu-live-state" role="status">Connecting</span></div>
      <div class="imu-stage" id="imu-stage"><div class="imu-model-note" id="imu-model-note">Loading Robbi’s 3D model…</div></div>
      <p class="hint">Drag to orbit · Scroll or pinch to zoom</p>
      <div class="imu-angles" aria-label="Orientation relative to the upright pose">
        ${['Roll','Pitch','Yaw'].map(n=>`<div><span>${n}</span><strong id="imu-${n.toLowerCase()}">—</strong></div>`).join('')}
      </div>
      <div class="actions"><button id="imu-align">Calibrate axes</button><button id="imu-zero" disabled>Set upright pose</button><button id="imu-pause">Pause live view</button><button id="imu-camera" disabled>Reset camera</button></div>
      <p class="imu-status" id="imu-status">Waiting for a fresh orientation sample…</p>
      <p class="hint" id="imu-reference">Place Robbi upright on a level surface, then set the pose. It is saved for driving, navigation and pickup detection.</p>
    </section><aside class="panel imu-details"><h2>Sensor health</h2>
      <dl class="key-values"><dt>IMU</dt><dd>LSM6DS3TR-C</dd><dt>Sample rate</dt><dd id="imu-rate">—</dd><dt>Sample age</dt><dd id="imu-age">—</dd><dt>Temperature</dt><dd id="imu-temperature">—</dd><dt>Gyro calibration</dt><dd id="imu-calibration">—</dd></dl>
      <div class="imu-calibration"><button id="imu-calibrate" class="small" disabled>Calibrate gyro</button><p class="hint" id="imu-calibration-note">Keep Robbi still on a level surface. Calibration measures gyro drift.</p></div>
      <p class="hint section-gap">Heading is relative and may drift over time. This view shows orientation; the IMU alone does not track position across a room.</p>
      <details class="section-gap"><summary>Acceleration & angular velocity</summary><table class="imu-axis-grid"><caption>Sensor axes · angular velocity includes measured bias</caption><thead><tr><th>Axis</th><th>m/s²</th><th>°/s</th></tr></thead><tbody id="imu-vectors"></tbody></table></details>
    </aside></div><section class="panel section-gap" id="imu-alignment"></section>`;
  const tracker=new AttitudeTracker(),host=$('#imu-stage',root),note=$('#imu-model-note',root);
  const status=$('#imu-status',root),badge=$('#imu-live-state',root),zero=$('#imu-zero',root),pause=$('#imu-pause',root);
  const calibrate=$('#imu-calibrate',root),calibrationNote=$('#imu-calibration-note',root);
  let scene,paused=false,timer,loading=false,pollAbort=null,lastSample=null,valid=false,calibrating=false,lastSuccess=0;
  const alignment=mountCalibration($('#imu-alignment',root),life,{
    apply(mount,reference){tracker.setMount(mount,reference);if(lastSample){const result=tracker.accept(lastSample,performance.now());if(result.ok)showAngles(result.rotation)}},
    mode(active){pause.disabled=active;zero.disabled=active||!valid;if(active){paused=false;pause.textContent='Pause live view';scene?.setVisible(!document.hidden);poll()}},
  });
  const number=(n,d=1)=>Number.isFinite(n)?n.toFixed(d):'—';
  function state(label,message,ok=false){badge.textContent=label;badge.className='badge '+(ok?'ready':['Paused','Connecting'].includes(label)?'':'degraded');status.textContent=message;status.dataset.state=ok?'ready':'error';valid=ok;zero.disabled=!ok||paused||alignment.active||calibrating;calibrate.disabled=paused||!ok||alignment.active||calibrating||lastSample?.calibration==='collecting'}
  function showAngles(rotation){
    // Readouts and model share exactly the same relative quaternion.
    const values=angles(rotation);for(const key of ['roll','pitch','yaw'])$('#imu-'+key,root).textContent=number(values[key])+'°';scene?.setOrientation(rotation);
  }
  function readings(sample){
    $('#imu-rate',root).textContent=number(sample.measured_hz)+' Hz';$('#imu-age',root).textContent=Number.isFinite(sample.age_s)?Math.round(sample.age_s*1000)+' ms':'—';
    $('#imu-temperature',root).textContent=number(sample.temperature_c)+' °C';
    $('#imu-calibration',root).textContent=sample.calibration==='collecting'?`Collecting · ${Math.round((sample.calibration_progress||0)*100)}%`:({complete:'Complete',timed_out:'Needs a still surface',waiting:'Waiting',disabled:'Disabled'}[sample.calibration]||'Unavailable');
    $('#imu-vectors',root).innerHTML=['X','Y','Z'].map((axis,i)=>`<tr><th>${axis}</th><td>${number(sample.acceleration_m_s2?.[i],3)}</td><td>${number(sample.gyro_deg_s?.[i],3)}</td></tr>`).join('');
  }
  async function poll(){
    clearTimeout(timer);if(loading||paused||document.hidden||life.signal.aborted)return;
    loading=true;pollAbort=new AbortController();let delay=100;
    try{
      const envelope=await api(alignment.path(),{signal:pollAbort.signal,timeout:1800});
      if(life.signal.aborted||paused||document.hidden)return;
      const sample=alignment.sample(envelope);
      lastSample=sample;lastSuccess=performance.now();readings(sample);
      const result=tracker.accept(sample,lastSuccess);
      if(result.ok){showAngles(result.rotation);state('Live',sample.calibration==='collecting'?'Keep Robbi still while gyro calibration finishes.':'Following Robbi’s orientation.',true);if(result.restarted)$('#imu-reference',root).textContent='Robbi restarted. The view reference was reset; set upright pose again if needed.'}
      else{scene?.freeze();state(result.label,result.message);delay=1000}
    }catch(error){if(!life.signal.aborted&&!paused&&!document.hidden){alignment.failed(error);scene?.freeze();state('Disconnected','Connection interrupted. Holding the last pose and retrying.');delay=2000}}
    finally{loading=false;pollAbort=null;if(!life.signal.aborted&&!paused&&!document.hidden)timer=setTimeout(poll,delay)}
  }
  function suspend(hide=false){clearTimeout(timer);pollAbort?.abort();scene?.freeze();if(hide)scene?.setVisible(false)}
  life.on(pause,'click',()=>{paused=!paused;pause.textContent=paused?'Resume live view':'Pause live view';if(paused){suspend();state('Paused','Live updates are paused. The last pose is held.')}else{scene?.setVisible(!document.hidden);state('Connecting','Reconnecting to the IMU…');poll()}});
  life.on(zero,'click',async()=>{
    if(!valid||paused||calibrating)return;
    calibrating=true;zero.disabled=true;
    const reference=$('#imu-reference',root);reference.textContent='Keep Robbi upright and still…';
    try{
      const result=await api('/api/imu-view/upright',{method:'POST',body:{},signal:life.signal,timeout:5000});
      if(life.signal.aborted)return;
      const q=tracker.zero();if(q)showAngles(q);
      reference.textContent=result.message+' Controls recover after two seconds of stillness.';
    }catch(error){if(!life.signal.aborted)reference.textContent=error.message}
    finally{calibrating=false;if(!life.signal.aborted)zero.disabled=!valid||paused||alignment.active}
  });
  life.on($('#imu-camera',root),'click',()=>scene?.resetCamera());
  life.on($('#imu-align',root),'click',()=>{$('#alignment-start',root).click();$('#imu-alignment',root).scrollIntoView({block:'center'})});
  life.on(calibrate,'click',async()=>{
    if(calibrating||calibrate.disabled)return;calibrating=true;calibrate.disabled=true;
    try{const result=await api('/imu/calibrate',{method:'POST',body:{},signal:life.signal,timeout:5000});if(!life.signal.aborted)calibrationNote.textContent=result.message||'Keep Robbi still for calibration.'}
    catch(error){if(!life.signal.aborted)calibrationNote.textContent=error.message}
    finally{calibrating=false;if(!life.signal.aborted)calibrate.disabled=paused||!valid||lastSample?.calibration==='collecting'}
  });
  life.on(document,'visibilitychange',()=>{if(document.hidden)suspend(true);else{scene?.setVisible(true);if(!paused)poll()}});
  // A hanging connection must not leave a green Live badge on a frozen model.
  const watchdog=setInterval(()=>{if(valid&&!paused&&!document.hidden&&performance.now()-lastSuccess>800){scene?.freeze();state('Stale','Waiting for fresh readings. Holding the last pose.')}},250);
  life.add(()=>{suspend(true);clearInterval(watchdog);scene?.dispose()});
  poll();
  try{
    const {createIMUScene}=await import('../imu/scene.js');if(life.signal.aborted)return;
    scene=createIMUScene(host,life.signal,(message,error)=>{note.textContent=message;note.hidden=!message;host.dataset.model=error?'error':'ready';$('#imu-camera',root).disabled=error});
    if(tracker.current)scene.setOrientation(tracker.current);scene.setVisible(!document.hidden);
  }catch(error){if(!life.signal.aborted){note.textContent='3D view unavailable. Check the connection and this browser’s WebGL 2 support. Sensor readings remain available.';host.dataset.model='error'}}
}

