import {$,api,conversationId,esc} from '../core.js';
import {normalize} from './orientation.mjs';

const POSES=[
  {id:'upright',title:'Stand Robbi upright',text:'Place Robbi in its normal standing position on a steady surface.'},
  {id:'left',title:'Lay Robbi on its left side',text:'Use Robbi’s own left, as if you were looking out through its camera. The left wheel faces down.'},
  {id:'right',title:'Lay Robbi on its right side',text:'Turn it onto the other side. The right wheel faces down.'},
  {id:'front',title:'Rest Robbi on its front',text:'The face and camera point down. Support the shell gently. Slightly past 90° is fine; do not force it flat.'},
];

export function mountCalibration(host,life,{apply,mode}){
  host.innerHTML=`<div class="panel-head"><div><h2>Align the 3D model</h2><p class="hint" id="alignment-summary">Checking saved alignment…</p></div><button id="alignment-start" class="small">Calibrate axes</button></div><div id="alignment-wizard" hidden></div>`;
  const start=$('#alignment-start',host),summary=$('#alignment-summary',host),wizard=$('#alignment-wizard',host);
  let session=null,step=0,poses={},upright=null,capture=null,busy=false,remaining=120,finished=false;
  function render(message=''){
    if(!session)return;
    const selected=POSES[step],review=step===POSES.length;
    wizard.hidden=false;wizard.innerHTML=`<div class="alignment-steps" aria-label="Calibration steps">${POSES.map((p,i)=>`<button type="button" class="small ${i===step?'primary':''}" data-step="${i}" ${busy||i>Object.keys(poses).length?'disabled':''}>${poses[p.id]?'✓ ':''}${i+1}. ${esc(p.id)}</button>`).join('')}</div>
      <h3>${review?'Return Robbi upright':esc(selected.title)}</h3>
      <p>${review?'All four poses are captured. Put Robbi back in its normal standing position, then save the alignment.':esc(selected.text)}</p>
      <p class="hint" id="alignment-progress" role="status">${esc(message||(capture?'Hold still… collecting a stable sample.':review?'Alignment is saved on Robbi and used by every dashboard.':'Press Capture pose, then hold still for about two seconds.'))}</p>
      <div class="actions">${review?'<button type="button" class="primary" id="alignment-save">Save alignment</button>':`<button type="button" class="primary" id="alignment-capture" ${capture?'disabled':''}>${capture?'Hold still…':'Capture pose'}</button>`}<button type="button" id="alignment-cancel" ${busy?'disabled':''}>Cancel</button></div>
      <p class="hint">Motor commands are held during calibration · <span id="alignment-time">${Math.ceil(remaining)}</span>s remaining.<br>Put Robbi upright before saving, cancelling or leaving this tab.</p>`;
  }
  function release(){
    if(!session)return;const id=session;session=null;capture=null;busy=false;mode(false);wizard.hidden=true;start.disabled=false;
    // Best-effort release on navigation; the server also expires lost heartbeats.
    fetch('/api/imu-view/session/'+id,{method:'DELETE',keepalive:true,headers:{'Content-Type':'application/json','X-Robbi-Conversation':conversationId},body:'{}'}).catch(()=>{});
  }
  life.on(start,'click',async()=>{
    if(busy)return;busy=true;start.disabled=true;
    try{
      const result=await api('/api/imu-view/session',{method:'POST',body:{},signal:life.signal});
      if(life.signal.aborted)return;
      session=result.id;remaining=result.remaining_s;step=0;poses={};upright=null;capture=null;busy=false;mode(true);render();
    }catch(error){if(!life.signal.aborted){summary.textContent=error.message;start.disabled=false;busy=false}}
  });
  life.on(wizard,'click',async event=>{
    const button=event.target.closest('button');if(!button||button.disabled)return;
    if(button.id==='alignment-cancel'){release();summary.textContent='Calibration cancelled. The previous alignment is unchanged.';return}
    if(button.dataset.step!==undefined){step=Number(button.dataset.step);capture=null;render();return}
    if(button.id==='alignment-capture'){capture={samples:[],started:performance.now(),count:null};render();return}
    if(button.id==='alignment-save'){
      busy=true;button.disabled=true;
      try{
        const result=await api('/api/imu-view/calibration',{method:'POST',body:{session_id:session,poses},signal:life.signal});
        if(life.signal.aborted)return;
        apply(result.calibration.mount_wxyz,upright);finished=true;release();summary.textContent='Axes calibrated and saved on Robbi. Tilt and turn to check the model.';
      }catch(error){if(!life.signal.aborted){busy=false;render(error.message)}}
    }
  });
  life.on(document,'visibilitychange',()=>{if(document.hidden&&session){release();summary.textContent='Calibration cancelled when this tab was hidden. Put Robbi upright and start again.'}});
  life.add(release);
  api('/api/imu-view/calibration',{signal:life.signal}).then(result=>{
    if(life.signal.aborted||session||finished)return;
    if(result.calibration){apply(result.calibration.mount_wxyz);summary.textContent='Saved axis alignment is active. Recalibrate if the model follows the wrong axes.'}
    else summary.textContent=result.error||'Four simple poses correct the IMU’s mounting angle and rotation axes.';
  }).catch(error=>{if(!life.signal.aborted)summary.textContent='Saved alignment unavailable. '+error.message});
  return {
    path(){return session?'/api/imu-view/session/'+session:'/imu/data'},
    get active(){return !!session},
    failed(error){if(session&&error.status===400){release();summary.textContent=error.message;return true}return false},
    sample(envelope){
      if(!envelope.imu)return envelope;
      const sample=envelope.imu;remaining=envelope.remaining_s;
      const timer=$('#alignment-time',host);if(timer)timer.textContent=Math.ceil(remaining);
      if(!capture||!session)return sample;
      const q=normalize(sample.quaternion),accel=sample.acceleration_m_s2,gyro=sample.gyro_deg_s,bias=sample.gyro_bias_deg_s;
      const vector=v=>Array.isArray(v)&&v.length===3&&v.every(Number.isFinite);
      const good=sample.fresh&&q&&vector(accel)&&vector(gyro)&&vector(bias)&&sample.calibration==='complete'&&Math.abs(Math.hypot(...accel)-9.80665)<1.3&&Math.hypot(...gyro.map((v,i)=>v-bias[i]))<3;
      const progress=$('#alignment-progress',host);
      if(performance.now()-capture.started>20000){capture=null;render('No steady sample yet. Support Robbi on a stable surface, then try Capture pose again.');return sample}
      if(!good){capture.samples=[];if(progress)progress.textContent='Keep Robbi completely still; waiting for stable IMU readings.';return sample}
      if(capture.count===sample.sample_count)return sample;capture.count=sample.sample_count;
      const anchor=capture.samples[0];
      if(anchor&&Math.hypot(...accel.map((v,i)=>v-anchor.accel[i]))>.3)capture.samples=[];
      capture.samples.push({accel:[...accel],q,time:performance.now()});
      const elapsed=capture.samples.at(-1).time-capture.samples[0].time;
      if(progress)progress.textContent=`Hold still… ${Math.min(100,Math.round(elapsed/15))}%`;
      if(elapsed>=1500&&capture.samples.length>=8){
        const id=POSES[step].id;poses[id]=[0,1,2].map(i=>capture.samples.reduce((sum,s)=>sum+s.accel[i],0)/capture.samples.length);
        if(id==='upright')upright=[...q];
        capture=null;step=Object.keys(poses).length===4?POSES.length:step+1;render();
      }
      return sample;
    },
  };
}
