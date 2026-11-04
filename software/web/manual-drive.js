import {$,$$,api,esc,fields,data,issue,state} from './core.js';

const paths={
  forward:'M16 27V5M7 14l9-9 9 9', backward:'M16 5v22M7 18l9 9 9-9',
  left:'M26 26V18A10 10 0 0 0 16 8H5M12 1 5 8l7 7',
  right:'M6 26V18A10 10 0 0 1 16 8h11M20 1l7 7-7 7',
  arc_left:'M26 27C26 15 19 7 5 7M12 1 5 7l7 7',
  arc_right:'M6 27C6 15 13 7 27 7M20 1l7 6-7 7',
  around:'M6 27V12a10 10 0 0 1 20 0v15M19 20l7 7 5-7',
  wiggle:'M4 17C10 2 22 2 28 17M4 17l-1-8M4 17l8-2M28 17l1-8M28 17l-8-2',
  wiggle_forward:'M16 29 8 23 24 15 16 7V2M10 8l6-6 6 6',
  sine:'M16 29c-18-8 18-12 0-20V2M10 8l6-6 6 6', stop:'M8 8h16v16H8z',
};
const warningText={front_range_unavailable:'Front range assistance unavailable',imu_heading_unavailable:'Used encoders while the IMU was unavailable',imu_heading_stale:'Used encoders while IMU readings were delayed',imu_heading_reset:'Recovered from an IMU restart',imu_heading_jump:'Ignored an inconsistent IMU reading',imu_heading_direction:'Used encoders because IMU direction was inconsistent',imu_encoder_disagreement:'Sensor estimates differed',control_loop_late:'Recovered a delayed control update',turn_accuracy_not_reached:'Final angle is approximate',turn_not_settled:'Final settling was incomplete',turn_excessive_error:'Final angle differs from the target'};
const icon=name=>`<svg viewBox="0 0 32 32" aria-hidden="true" focusable="false"><path d="${paths[name]}"/></svg>`;
const button=(key,label,detail='')=>`<button type="button" class="drive-button" data-drive="${key}">${icon(key)}<span>${label}</span><small data-detail="${key}">${detail}</small></button>`;

// Each button sends an explicit measured target. No timed forward fallback.
export function commandFor(key, v){
  const base={speed:Number(v.speed)};
  switch(key){
    case 'forward': case 'backward':return {...base,mode:'distance',distance_cm:Number(v.distance_cm)*(key==='backward'?-1:1)};
    case 'left': case 'right': case 'around':return {...base,mode:'turn',angle_deg:key==='around'?180:Number(v.angle_deg)*(key==='right'?-1:1)};
    case 'arc_left':case 'arc_right':return {...base,mode:'arc',angle_deg:Number(v.angle_deg)*(key==='arc_right'?-1:1),radius_cm:Number(v.radius_cm)};
    case 'wiggle':return {...base,mode:'wiggle',angle_deg:Number(v.swing_deg),cycles:Number(v.cycles)};
    case 'wiggle_forward':case 'sine':return {...base,mode:key,distance_cm:Number(v.distance_cm),angle_deg:Number(v.swing_deg),radius_cm:Number(v.radius_cm),cycles:Number(v.cycles),...(v.amplitude_cm!==''&&v.amplitude_cm!=null?{amplitude_cm:Number(v.amplitude_cm)}:{})};
    case 'avoid':return {...base,mode:'avoid_obstacle',distance_cm:Number(v.backoff_cm),angle_deg:Number(v.angle_deg)};
    default:throw new Error('Unknown movement');
  }
}

export function mountManualDrive(root,life){
  root.innerHTML=`<div class="drive-layout">
    <section class="panel drive-panel">
      <div class="panel-head"><h2>Manual driving</h2><span class="badge" id="drive-state">Connecting</span></div>
      <label class="check drive-arm"><input type="checkbox" id="arm-motion">Enable manual controls for this visit</label>
      <p class="drive-notice" id="motion-notice" role="status">Checking motion hardware…</p>
      <p class="hint">Manual commands can continue without fresh ToF readings. Keep the path clear and stay ready to press Stop. Raw wheel tests allow up to 100% duty and bypass the normal speed ceiling.</p><form id="drive-values"><div class="drive-values">${fields([
        {name:'distance_cm',label:'Distance (cm)',type:'number',min:.5,max:100,step:.5,value:20,required:true},
        {name:'angle_deg',label:'Turn (°)',type:'number',min:1,max:180,value:90,required:true},
        {name:'speed',label:'Speed (%)',type:'number',min:1,max:100,value:100,required:true},
      ])}</div>
      <div class="drive-pad" role="group" aria-label="Driving controls">
        ${button('arc_left','Curve left')}${button('forward','Forward')}${button('arc_right','Curve right')}
        ${button('left','Turn left')}<button type="button" class="drive-button stop" id="drive-stop">${icon('stop')}<span>Stop</span><small>All movement</small></button>${button('right','Turn right')}
        ${button('wiggle_forward','Wiggle forward')}${button('backward','Backward')}${button('around','Turn around','180°')}
      </div>
      <div class="drive-extras">${button('wiggle','Wiggle in place')}${button('sine','Sine forward')}</div>
      <details class="drive-options"><summary>Movement options</summary><div class="details-body"><div class="form-grid">${fields([
        {name:'radius_cm',label:'Curve radius (cm)',type:'number',min:6,max:100,step:.5,value:15,required:true},
        {name:'swing_deg',label:'Wiggle / sine angle (°)',type:'number',min:5,max:60,value:15,required:true},
        {name:'cycles',label:'Wiggle / sine cycles',type:'number',min:1,max:6,value:1,required:true},
        {name:'amplitude_cm',label:'Wave amplitude (cm, blank = auto)',type:'number',min:0,max:30,step:.1,value:''},
        {name:'backoff_cm',label:'Avoidance backoff (cm)',type:'number',min:.5,max:50,step:.5,value:10,required:true},
      ])}</div><p class="hint">Curves use the turn angle and radius. Forward wiggle and sine follow a continuous wave; in-place wiggle alternates measured turns. Amplitude is half the maximum sideways excursion. Each click runs once.</p><p class="hint" id="wave-geometry"></p><button type="button" data-drive="avoid">Back off & turn right</button></div></details>
      </form>
      <div class="drive-feedback" id="motion-result" role="status">Choose a movement. Nothing runs automatically.</div>
      <details id="drive-result-details" hidden><summary>Movement measurements</summary><div class="details-body"><pre id="drive-result-json"></pre></div></details>
    </section>
    <section class="panel drive-sensors">
      <div class="panel-head"><h2>Live feedback</h2><span class="badge" id="drive-heading-badge">Checking IMU</span></div>
      <p id="drive-heading-note" class="hint">Checking heading assistance…</p>
      <dl class="key-values" id="sensor-summary"></dl>
      <p class="hint" id="tof-hint"></p>
      <a href="#imu">Open IMU · Live 3D →</a>
      <details class="section-gap"><summary>IMU calibration</summary><div class="details-body"><p class="hint">Keep Robbi still on a level surface. Movement is locked while calibration is requested.</p><button id="imu-calibrate" type="button">Calibrate gyro bias</button><div id="imu-calibration-result" class="hint" role="status"></div></div></details>
      <details class="section-gap"><summary>Raw wheel test</summary><div class="details-body"><p class="hint">Short bench tests. Raw wheel PWM uses no heading correction.</p><form id="raw-drive"><fieldset id="raw-controls" disabled><div class="form-grid">${fields([
        {name:'left',label:'Left wheel (%)',type:'number',min:-100,max:100,value:0,required:true},
        {name:'right',label:'Right wheel (%)',type:'number',min:-100,max:100,value:0,required:true},
        {name:'duration',label:'Duration (seconds)',type:'number',min:.05,max:2,step:.05,value:.2,required:true},
      ])}</div><div class="actions"><button type="submit">Run wheel test</button></div></fieldset></form></div></details>
    </section></div>`;
  const find=s=>$(s,root),all=s=>$$(s,root),arm=find('#arm-motion'),form=find('#drive-values');
  let snapshot=null,received=0,requestBusy=false,polling=false,timer,watchdog,lastCommand='',disposed=false,revision=0;
  const live=()=>snapshot&&received>0&&performance.now()-received<2500&&!document.hidden;
  const hardwareReady=()=>live()&&snapshot.motion?.enabled&&snapshot.motion?.available;
  const available=()=>hardwareReady()&&snapshot.motion?.encoder_enabled&&!snapshot.handling?.pickup_detected;
  const running=()=>snapshot?.motion?.running||snapshot?.command?.state==='running';
  const allowed=()=>available()&&arm.checked&&!requestBusy&&!running();
  const rawAllowed=()=>hardwareReady()&&arm.checked&&!requestBusy&&!running();
  function gate(){
    const fresh=live(),m=snapshot?.motion||{},t=snapshot?.tof||{};
    // Sensor/network interruptions preserve the owner's selection. They never
    // queue or replay a command. Stop, hiding the page and leaving reset it.
    all('[data-drive]').forEach(b=>b.disabled=!allowed());
    find('#raw-controls').disabled=!rawAllowed();
    $$('[data-motion]').forEach(b=>b.disabled=!allowed());
    find('#imu-calibrate').disabled=!fresh||!snapshot?.imu?.available||!!running()||requestBusy;
    const status=!fresh?'Offline':requestBusy||running()?'Moving':!available()?'Waiting':arm.checked?'Ready':'Controls off';
    find('#drive-state').textContent=status;
    find('#drive-state').className=`badge ${status==='Ready'?'ready':status==='Offline'?'failed':''}`;
    if(!fresh){find('#drive-heading-badge').textContent='Feedback offline';find('#drive-heading-badge').className='badge disabled'}
    find('#motion-notice').textContent=!fresh?'Connecting to live motion status. Commands are locked.':
      !m.enabled?'Motors are disabled in Settings.':!m.available?`Motor driver unavailable. ${m.last_error||''}`:
      !m.encoder_enabled?'Wheel encoders must be enabled for measured driving.':
      snapshot?.handling?.pickup_detected?'Robbi detects a tilted or held pose. If already upright, open IMU and click Set upright pose.':
      !t.fresh?(t.range_state==='no_return'?'ToF has no valid range. ':'Waiting for fresh front ToF readings. ')+(arm.checked?'Manual movement remains available without range assistance.':'Enable controls for manual movement without range assistance.'):
      running()?'Movement in progress. Press Stop to interrupt.':requestBusy?'Sending request…':
      arm.checked?'Ready. One click, one movement. Keep the area clear.':'Enable manual controls, then choose a movement.';
  }
  function feedback(){
    const m=snapshot.motion||{},t=snapshot.tof||{},imu=snapshot.imu||{},h=m.heading||{};
    const assisted=h.enabled&&h.available;
    find('#drive-heading-badge').textContent=assisted?'IMU + encoders':'Encoders only';
    find('#drive-heading-badge').className='badge '+(assisted?'ready':'degraded');
    find('#drive-heading-note').textContent=assisted?'Gyro heading, accelerometer tilt correction and wheel distance guide measured movements.':
      h.enabled?'IMU is not ready. Manual movement uses encoders and blends the IMU back in after stable readings return.':'IMU heading assistance is disabled in Motion settings.';
    const range=t.fresh?`${t.distance_cm} cm`:({no_return:'No valid range',stale:'Stale reading',waiting:'Starting',disabled:'Disabled',unavailable:'Disconnected'})[t.range_state]||'Unavailable';
    find('#sensor-summary').innerHTML=[['Front distance',range],['Reading age',t.age_s==null?'—':`${Math.round(t.age_s*1000)} ms`],['Wheels',m.running?'Moving':'Stopped'],['Encoders',m.encoder_enabled?'Enabled':'Disabled'],['IMU',imu.fresh?'Ready':imu.available?'Waiting for readings':'Unavailable']].map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
    find('#tof-hint').textContent=t.fresh?'Fresh obstacle readings assist manual movement. Sensor interruptions produce warnings without locking the controls.':t.range_state==='no_return'?'The sensor is connected but cannot measure this surface or distance. Manual controls remain available.':t.last_error||'Range assistance unavailable. Manual controls remain available.';
    const command=snapshot.command;
    if(command){
      const signature=command.id+command.state;
      if(signature!==lastCommand){
        lastCommand=signature;
        if(command.state==='running')find('#motion-result').textContent='Moving…';
        else{
          const result=command.result||{},heading=result.heading;
          const segments=result.segments||[result];
          const allAssisted=segments.length&&segments.every(s=>s.heading?.source==='imu_encoder'&&Number.isFinite(s.heading.imu_deg));
          const measured=allAssisted?` IMU heading change: ${segments.reduce((total,s)=>total+s.heading.imu_deg,0).toFixed(1)}°.`:
            heading?.source==='encoders'?' Encoder feedback only.':'';
          const warnings=[...new Set([...(command.warnings||[]),...(result.warnings||[]),...(heading?.warnings||[])])];
          const note=warnings.length?' Notes: '+warnings.map(w=>warningText[w]||w.replaceAll('_',' ')).join(', ')+'.':'';
          find('#motion-result').textContent=(result.ok?(result.precision_reached===false?'Movement finished; final angle is approximate.':'Movement finished.'):`Stopped: ${(result.reason||result.error||'Movement did not complete').replaceAll('_',' ')}.`)+measured+note;
          find('#drive-result-json').textContent=JSON.stringify(result,null,2);
          find('#drive-result-details').hidden=false;
        }
      }
    }
    gate();
  }
  async function refresh(){
    clearTimeout(timer);if(polling||disposed||document.hidden)return;polling=true;
    const version=revision;
    try{const result=await api('/api/manual-motion',{timeout:2000,signal:life.signal});if(disposed||version!==revision)return;snapshot=result;received=performance.now();feedback()}
    catch{if(!disposed){received=0;gate()}}
    finally{polling=false;if(!disposed&&!document.hidden)timer=setTimeout(refresh,live()&&(running()||arm.checked)?500:1000)}
  }
  function labels(){
    const v=data(form);
    for(const key of ['forward','backward','wiggle_forward','sine'])find(`[data-detail="${key}"]`).textContent=`${v.distance_cm||'—'} cm`;
    for(const key of ['left','right'])find(`[data-detail="${key}"]`).textContent=`${v.angle_deg||'—'}° · in place`;
    for(const key of ['arc_left','arc_right'])find(`[data-detail="${key}"]`).textContent=`${v.angle_deg||'—'}° · R ${v.radius_cm||'—'} cm`;
    find('[data-detail="wiggle"]').textContent=`±${v.swing_deg||'—'}° · ${v.cycles||'—'}×`;
    const k=2*Math.PI*Number(v.cycles)/Number(v.distance_cm);
    const maxA=Math.min(1/(Number(v.radius_cm)*k*k),Math.tan(Number(v.swing_deg)*Math.PI/180)/k);
    const a=v.amplitude_cm===''?maxA:Number(v.amplitude_cm);
    find('#wave-geometry').textContent=Number.isFinite(maxA)?`This wave: ${(2*a).toFixed(2)} cm maximum sideways excursion; feasible amplitude up to ${maxA.toFixed(2)} cm. ${a>maxA?'Increase distance, reduce cycles, or lower amplitude before driving.':'Longer travel allows a wider wave.'}`:'';
  }
  async function send(payload,path='/api/manual-motion'){
    if(!(path==='/api/manual-motion/raw'?rawAllowed():allowed()))return;
    revision++;requestBusy=true;gate();
    find('#motion-result').textContent='Sending movement…';
    try{
      const result=await api(path,{method:'POST',body:payload,timeout:5000});
      if(disposed)return;
      if(result.command){snapshot.command=result.command;feedback()}
      else find('#motion-result').textContent='Wheel test requested. No IMU correction for raw PWM.';
    }catch(e){received=0;if(!disposed)issue(find('#motion-result'),e)}
    finally{requestBusy=false;if(!disposed){gate();refresh()}}
  }
  life.on(form,'submit',e=>e.preventDefault());
  life.on(form,'input',labels);
  life.on(root,'click',e=>{
    const b=e.target.closest('[data-drive]');if(!b||b.disabled)return;
    // Validate only parameters used by this button; a closed option is revealed
    // before showing its validation message.
    const payload=commandFor(b.dataset.drive,data(form));
    for(const key of Object.keys(payload).filter(k=>k!=='mode')){
      const name=key==='angle_deg'&&['wiggle','wiggle_forward','sine'].includes(b.dataset.drive)?'swing_deg':key==='distance_cm'&&b.dataset.drive==='avoid'?'backoff_cm':key;
      if(b.dataset.drive==='around'&&name==='angle_deg')continue;
      const input=find(`[name="${name}"]`);
      if(input&&!input.checkValidity()){const details=input.closest('details');if(details)details.open=true;input.reportValidity();return}
    }
    send(payload);
  });
  life.on(find('#raw-drive'),'submit',e=>{e.preventDefault();send(data(e.currentTarget),'/api/manual-motion/raw')});
  life.on(arm,'change',()=>{gate();refresh()});
  life.on(find('#drive-stop'),'click',()=>$('#stop').click());
  life.on(document,'robbi:stop',()=>{arm.checked=false;find('#motion-result').textContent='Stop requested. Waiting for motion status…';gate();refresh()});
  life.on(document,'robbi:gates',gate);
  life.on(document,'robbi:visibility',()=>{if(document.hidden){arm.checked=false;clearTimeout(timer);gate()}else refresh()});
  life.on(find('#imu-calibrate'),'click',async()=>{
    requestBusy=true;gate();
    try{await api('/imu/calibrate',{method:'POST',body:{},timeout:20000});if(!disposed)find('#imu-calibration-result').textContent='Calibration requested. Keep Robbi still.'}
    catch(e){if(!disposed)find('#imu-calibration-result').textContent=e.message}
    finally{requestBusy=false;if(!disposed){gate();refresh()}}
  });
  watchdog=setInterval(gate,500);
  life.add(()=>{disposed=true;clearTimeout(timer);clearInterval(watchdog)});
  labels();gate();refresh();
  return {allowed};
}
