import {$,api,heading,esc,action,notify,data} from '../core.js';
import {mapCanvas} from '../navigation-map.js';

export async function navigation(root,life){
  root.innerHTML=heading('MAPPING & NAVIGATION','Let Robbi find its way.','Scan, explore, and choose a destination on your saved map.')+`
  <section class="panel nav-map-panel">
    <div class="panel-head"><h2 id="nav-name">Current map</h2><span class="badge" id="nav-state">Connecting</span></div>
    <p id="nav-message" role="status" aria-live="polite">Reading navigation status…</p>
    <div class="actions"><button id="nav-scan" class="primary" data-idle>Scan room</button><button id="nav-explore" data-idle>Explore</button><button id="nav-stop" class="danger">Stop navigation</button><button type="submit" form="nav-goal" id="nav-drive" class="primary" disabled>Drive to point</button></div>
    <p class="hint">The first scan makes several turns to build a detailed map. Scan room also finds Robbi’s position automatically after loading a map or moving it.</p>
    <canvas id="navigation-map" aria-label="Map: dark obstacles, white free space, grey unknown. Tap to select a point; drag to pan. Coordinates can also be entered below." tabindex="0"></canvas>
    <div class="nav-map-footer"><span>■ Supported obstacles · □ Observed / confirmed open · ▧ Unknown / tentative · <span class="nav-green">● Robbi / route</span> · Orange line: front ToF</span><div class="actions compact"><button id="nav-zoom-out" aria-label="Zoom out">−</button><button id="nav-fit">Fit map</button><button id="nav-zoom-in" aria-label="Zoom in">+</button></div></div>
    <p class="hint" id="nav-pose">Position unknown.</p>
    <p class="hint" id="nav-range" role="status">Reading front distance sensor…</p>
    <p class="hint" id="nav-heading" role="status">Checking IMU heading…</p>
    <form id="nav-goal" class="nav-goal"><label>X (cm)<input name="x_cm" type="number" min="-3000" max="3000" value="0" required></label><label>Y (cm)<input name="y_cm" type="number" min="-3000" max="3000" value="0" required></label></form>
    <p class="hint" id="nav-route">Tap the map or enter coordinates, then press Drive to point. Robbi checks the route automatically.</p>
  </section>
  <div class="nav-columns section-gap">
    <section class="panel"><h2>Your maps</h2><form id="nav-save" class="nav-inline"><label>Map name<input name="name" maxlength="80" required placeholder="Apartment — ground floor"></label><button type="submit" data-idle>Save copy</button></form><label class="section-gap">Saved maps<select id="nav-library"><option value="">No saved maps</option></select></label><div class="actions section-gap"><button id="nav-load" data-idle>Load map</button><button id="nav-delete" data-idle>Delete saved map</button><button id="nav-new" data-idle>New map</button></div><p class="hint">Scans are checkpointed automatically. Loading or moving Robbi requires relocalization. Named maps stay in your library.</p></section>
    <section class="panel"><h2>Explore at your pace</h2><form id="nav-limits" class="form-grid"><label>Scan stops<input name="max_steps" type="number" value="30" min="1" max="100" step="1"></label><label>Total travel (m)<input name="distance_m" type="number" value="20" min=".1" max="100" step=".1"></label><label>Time limit (min)<input name="minutes" type="number" value="30" min="1" max="60"></label></form><p class="hint">Up to 2 m between scans, divided into short measured moves. Robbi rescans sooner near narrow passages or when its position estimate weakens.</p><p class="hint">Use a level floor without stairs. The forward distance sensor cannot see drops, and may miss glass or low objects.</p></section>
  </div>
  <details class="panel section-gap"><summary>Scan quality & sensor rate</summary><div class="details-body">
    <p id="nav-sampling" role="status">Reading ToF timing…</p>
    <div class="actions"><button data-tof-preset="room" data-idle>Room mapping · ~28 Hz</button><button data-tof-preset="fast" data-idle>Near range · up to 50 Hz</button><button data-tof-preset="quality" data-idle>Stronger returns · ~18 Hz</button></div>
    <p class="hint">Fast mode uses a 20 ms budget and short distance mode (about 1.3 m maximum). Room mapping uses 33 ms and long distance mode. Actual rate and valid returns depend on lighting and surfaces. These buttons save the ToF settings; no movement starts. A narrower ROI is available in Settings, with lower sensitivity.</p>
    <p id="nav-sampling-result" role="status"></p></div></details>
  <details class="panel section-gap"><summary>Position hint</summary><div class="details-body"><p>Scan room finds the position automatically. If rooms look alike, select Robbi’s approximate position on the map to help the next scan.</p><form id="nav-hint" class="nav-inline"><label>Approximate heading (optional)<input name="theta_deg" type="number" min="-180" max="180" placeholder="0 = right, 90 = up"></label><button type="submit" data-idle>Scan using selected position</button></form><p class="hint">A hint narrows the search; the new scan still verifies the position.</p></div></details>
  <details class="panel section-gap"><summary>Robot geometry & navigation settings</summary><div class="details-body"><p class="hint">Radius must include the wheels. Sensor offsets are measured from the wheel axle center. Resolution and sensor placement can only change on an empty map.</p><form id="nav-settings" class="form-grid"></form><button id="nav-save-settings" class="section-gap" data-idle>Save settings</button></div></details>
  <details class="panel section-gap"><summary>Localization details</summary><pre id="nav-diagnostics" class="details-body">No scan yet.</pre></details>`;
  let status=null,revision=-1,timer=null,busy=false,connected=false,lastLibrary='',selection=null,commandPending=false,libraryAt=0;
  const map=mapCanvas($('#navigation-map'),life,p=>{$('#nav-goal [name=x_cm]').value=p.x_cm;$('#nav-goal [name=y_cm]').value=p.y_cm;selectGoal(p)});
  function selectGoal(value){selection=value;map.select(value);render()}
  const fields={robot_radius_cm:['Robot radius (cm)',8,25],clearance_cm:['Extra clearance (cm)',2,15],leg_cm:['Maximum scan leg (cm)',20,200],segment_cm:['Measured segment (cm)',5,50],drive_speed:['Drive PWM (%)',25,100],turn_speed:['Turn PWM (%)',25,100],scan_speed:['Scan PWM ceiling (%)',25,100],scan_rate_deg_s:['Scan rotation target (°/s)',10,120],scan_bin_deg:['Point spacing (°)',.5,10],scan_max_fill_turns:['Automatic coverage repairs',0,12],max_range_cm:['Usable ToF range (cm)',50,400],empty_clearance_cm:['Confirmed empty space (cm, 0 = off)',0,200],sensor_forward_cm:['Sensor forward offset (cm)',-20,20],sensor_left_cm:['Sensor left offset (cm)',-20,20],sensor_yaw_deg:['Sensor bearing (degrees)',-180,180],resolution_cm:['Map cell size (cm)',3,10]};
  let settingsMounted=false,settingsSignature='';
  function render(){if(!status)return;$('#nav-name').textContent=status.map_name;$('#nav-state').textContent=status.active?status.phase.replaceAll('_',' '):status.localized?'Ready':'Scan room to start';$('#nav-message').textContent=status.message;$('#nav-message').className=status.last_error?'nav-error':'';
    const p=status.pose;$('#nav-pose').textContent=p?`x ${p.x_cm.toFixed(0)} cm · y ${p.y_cm.toFixed(0)} cm · ${p.theta_deg.toFixed(1)}° · Estimated position spread ${p.sigma_cm.toFixed(0)} cm · ${status.scan_count} scans`:'Position is unknown. Scan or relocalize before driving.';
    for(const button of root.querySelectorAll('[data-idle]'))button.disabled=!connected||commandPending||status.active||!!button.dataset.busy;
    for(const id of ['nav-scan','nav-explore'])$('#'+id).disabled||=status.filming_mode;
    const validGoal=selection&&Number.isFinite(selection.x_cm)&&Number.isFinite(selection.y_cm)&&$('#nav-goal').checkValidity();
    $('#nav-drive').disabled=!connected||commandPending||status.active||!status.localized||!validGoal;
    $('#nav-load').disabled||=!$('#nav-library').value;$('#nav-delete').disabled||=!$('#nav-library').value;
    $('#nav-route').textContent=status.goal_adjustment?`Using a clear stopping point near the selected destination (${status.goal_adjustment.destination.map(v=>v.toFixed(0)).join(', ')} cm).`:status.active&&status.job==='navigate'?'Checking the route and driving to the selected point.':selection?`Destination: ${selection.x_cm}, ${selection.y_cm} cm. Press Drive to point; the route is checked automatically.`:'Tap the map or enter coordinates, then press Drive to point. Robbi checks the route automatically.';
    $('#nav-diagnostics').textContent=JSON.stringify({scan:status.scan_metrics,match:status.match,travel_cm:status.travel_cm,map_cells:status.cells,phase:status.phase,last_error:status.last_error},null,2);
    const t=status.tof||{},m=status.scan_metrics||{};
    $('#nav-heading').textContent=status.heading?.fresh?'Heading: IMU + wheel encoders · measured turn corrections active.':'Heading: waiting for a fresh calibrated IMU reading.';
    $('#nav-range').textContent=`Front ToF: ${t.fresh&&Number.isFinite(t.distance_cm)?t.distance_cm.toFixed(0)+' cm':(t.range_state||'unavailable').replaceAll('_',' ')} · ${t.measured_hz??'—'} readings/s · ${t.valid_return_percent??'—'}% valid returns`;
    $('#nav-sampling').textContent=`ToF: ${t.measured_hz??'—'} readings/s · ${t.valid_return_percent??'—'}% valid · ${t.timing_budget_ms??'—'} ms budget. Last scan: ${m.points??'—'} directions · ${m.confirmed_empty_bearings??0} confirmed empty${m.bootstrap?' · '+m.sweeps+' sweeps':''} · ${m.total_elapsed_s??m.elapsed_s??'—'} s · largest gap ${m.largest_gap_deg??'—'}° · ${m.partial_coverage?'partial map · ':''}${m.fill_turns??0} coverage repairs${m.phase==='repairing_coverage'?' (in progress)':''}.`;
    const signature=JSON.stringify(status.settings);
    if(!settingsMounted||(!status.active&&signature!==settingsSignature)){$('#nav-settings').innerHTML=Object.entries(fields).map(([key,[label,min,max]])=>`<label>${label}<input name="${key}" type="number" value="${status.settings[key]}" min="${min}" max="${max}" step="${key==='scan_bin_deg'?.5:1}" required></label>`).join('');settingsMounted=true;settingsSignature=signature}
    map.update(null,{...status,route:status.active?status.route:[]});
  }
  async function refresh(){clearTimeout(timer);if(busy||life.signal.aborted||document.hidden)return;busy=true;try{const snapshot=await api('/api/navigation/map?since='+revision,{signal:life.signal,timeout:6000});if(life.signal.aborted)return;status=snapshot.status;connected=true;revision=snapshot.revision;map.update(snapshot,status);
      if(!status.active&&Date.now()-libraryAt>30000){const value=await api('/api/navigation/maps',{signal:life.signal});if(life.signal.aborted)return;libraryAt=Date.now();const encoded=JSON.stringify(value.sessions);if(encoded!==lastLibrary){lastLibrary=encoded;const old=$('#nav-library').value;$('#nav-library').innerHTML='<option value="">Choose a saved map</option>'+value.sessions.map(m=>`<option value="${esc(m.id)}" ${m.error?'disabled':''}>${esc(m.name)}${m.error?' (unreadable)':''}</option>`).join('');if(value.sessions.some(m=>m.id===old))$('#nav-library').value=old}}
      render();
    }catch(e){if(life.signal.aborted)return;connected=false;$('#nav-message').textContent=e.message;$('#nav-message').className='nav-error';for(const b of root.querySelectorAll('[data-idle],#nav-drive'))b.disabled=true;$('#nav-state').textContent='Connection unavailable'}finally{busy=false;if(!life.signal.aborted&&!document.hidden)timer=setTimeout(refresh,status?.active?900:3000)}}
  async function command(button,op,body={}){if(commandPending&&op!=='stop')return;commandPending=true;if(op!=='navigate')selectGoal(null);render();try{const result=await action(button,()=>api('/api/navigation/'+op,{method:'POST',body}),{success:op==='stop'?'Stop requested.':'Request accepted.'});if(life.signal.aborted)return result;if(result){if(op==='navigate')selectGoal(null);if(status)status.active=true;if(['save','load','delete','new'].includes(op))libraryAt=0}return result}finally{commandPending=false;if(!life.signal.aborted){render();await refresh()}}}
  for(const op of ['scan','stop'])life.on($('#nav-'+op),'click',e=>command(e.currentTarget,op));
  life.on($('#nav-explore'),'click',e=>{const f=$('#nav-limits');if(!f.reportValidity())return;const v=data(f);command(e.currentTarget,'explore',{max_steps:v.max_steps,max_distance_cm:v.distance_m*100,timeout_s:v.minutes*60})});
  life.on($('#nav-goal'),'input',()=>selectGoal(data($('#nav-goal'))));
  life.on($('#nav-goal'),'submit',e=>{e.preventDefault();if(!$('#nav-drive').disabled&&e.currentTarget.reportValidity())command($('#nav-drive'),'navigate',{goal:data(e.currentTarget)})});
  life.on($('#nav-hint'),'submit',e=>{e.preventDefault();const p=selection||data($('#nav-goal'));const text=e.currentTarget.elements.theta_deg.value;command($('button',e.currentTarget),'scan',{hint:{x_cm:p.x_cm,y_cm:p.y_cm,...(text!==''?{theta_deg:Number(text)}:{})}})});
  life.on($('#nav-save'),'submit',e=>{e.preventDefault();command($('button',e.currentTarget),'save',data(e.currentTarget))});
  life.on($('#nav-library'),'change',render);
  life.on($('#nav-load'),'click',async e=>{if(confirm('Load this map? Robbi will need to relocalize before driving.')){settingsMounted=false;await command(e.currentTarget,'load',{id:$('#nav-library').value});map.fit()}});
  life.on($('#nav-delete'),'click',e=>{if(confirm('Delete the selected saved map?'))command(e.currentTarget,'delete',{id:$('#nav-library').value})});
  life.on($('#nav-new'),'click',e=>{if(confirm('Start an empty map? Save a named copy first if you want to keep the current one.'))command(e.currentTarget,'new')});
  life.on($('#nav-save-settings'),'click',e=>{if($('#nav-settings').reportValidity())command(e.currentTarget,'settings',{settings:data($('#nav-settings'))})});
  for(const button of root.querySelectorAll('[data-tof-preset]'))life.on(button,'click',async()=>{
    const preset={room:{distance_mode:2,timing_budget_ms:33,intermeasurement_ms:35},fast:{distance_mode:1,timing_budget_ms:20,intermeasurement_ms:20},quality:{distance_mode:2,timing_budget_ms:50,intermeasurement_ms:55}}[button.dataset.tofPreset];
    await action(button,()=>api('/settings_data',{method:'POST',body:{tof:{poll_hz:200,...preset}},timeout:15000}),{output:$('#nav-sampling-result'),success:'ToF timing saved. Waiting for fresh measurements.'});await refresh();
  });
  life.on($('#nav-zoom-out'),'click',()=>map.zoom(1/1.25));life.on($('#nav-zoom-in'),'click',()=>map.zoom(1.25));life.on($('#nav-fit'),'click',()=>map.fit());
  life.on(document,'robbi:visibility',refresh);life.on(document,'robbi:stop',()=>{selectGoal(null);setTimeout(()=>{if(!life.signal.aborted)refresh()},200)});
  life.add(()=>clearTimeout(timer));await refresh();
}
