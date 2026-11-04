import {FORMAT,FACES,DEFAULT_SETTINGS,waypoint,example,validateProject,compile,sampleAt,rad,deg,wrap} from './motion.js';
import {createStage} from './scene.js';
import {MediaPreview,hasMedia,cueLabel} from './media-preview.js';
import {createCueEditor} from './cue-editor.js';
import {createCapture} from './capture.js';

const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const esc=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone=value=>JSON.parse(JSON.stringify(value));
// Browser-library IDs are opaque; getRandomValues also works over LAN HTTP.
const createLocalId=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('');
const STORAGE='robbi-motion-studio-v1',LIBRARY='robbi-motion-library-v1';
const fmt=t=>`${String(Math.floor(t/60)).padStart(2,'0')}:${(t%60).toFixed(1).padStart(4,'0')}`;
const title=value=>value.charAt(0).toUpperCase()+value.slice(1);
let project=example(),selected=0,currentTime=0,playing=false,loop=false,playbackRate=1,tool='select',stage,plan;
let localId=createLocalId(),serverId=null,serverSnapshot='',device=null,statusBusy=false,statusTimer,history=[],future=[],lastFrame=performance.now(),gesture=false,storageFailed=false;
let library=[];
let capturePose=null;
let captureDistance=0;
const mediaPreview=new MediaPreview(()=>stage,toast,text=>{const caption=$('#speech-caption');caption.textContent=text;caption.hidden=!text;});
try{const saved=JSON.parse(localStorage.getItem(STORAGE)||'null');if(saved){project=validateProject(saved.project);localId=saved.id||localId;}const stored=JSON.parse(localStorage.getItem(LIBRARY)||'[]');if(Array.isArray(stored))library=stored.filter(c=>{try{validateProject(c.project);return typeof c.id==='string';}catch{return false;}}).slice(0,100);}catch{ /* An invalid draft never prevents opening the editor. */ }
plan=compile(project);
function toast(message,error=false){const element=$('#toast');element.textContent=message;element.classList.toggle('error',error);element.hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>element.hidden=true,error?6500:3300);}
function persist(){try{localStorage.setItem(STORAGE,JSON.stringify({id:localId,project}));$('#footer-status').textContent='All changes saved in this browser';$('#save-state').textContent='Saved locally';storageFailed=false;}catch{$('#footer-status').textContent='Browser storage full — export a file to keep your work';if(!storageFailed)toast('Browser storage is full. Export your animation to keep it.',true);storageFailed=true;}}
function snapshot(){return JSON.stringify({project,selected,localId,serverId,serverSnapshot});}
function pushUndo(){const s=snapshot();if(history.at(-1)!==s){history.push(s);if(history.length>80)history.shift();}future=[];updateHistory();}
function updateHistory(){$('#undo').disabled=!history.length;$('#redo').disabled=!future.length;}
function restore(saved){const data=JSON.parse(saved);project=data.project;selected=data.selected;localId=data.localId;serverId=data.serverId;serverSnapshot=data.serverSnapshot;playing=false;rebuild();}
function undo(){if(!history.length)return;future.push(snapshot());restore(history.pop());updateHistory();}
function redo(){if(!future.length)return;history.push(snapshot());restore(future.pop());updateHistory();}
function commit(change,{record=true}={}){const before=clone(project);try{if(record)pushUndo();change(project);project=validateProject(project);rebuild();}catch(error){project=before;toast(error.message,true);rebuild();}}
function beginGesture(){if(gesture)return;gesture=true;playing=false;pushUndo();}
function endGesture(){gesture=false;persist();$('#stage-selection').hidden=true;}
function selectPoint(index){selected=Math.max(0,Math.min(project.waypoints.length-1,index));playing=false;currentTime=plan.arrivals[selected]||0;renderList();renderInspector();stage?.select(selected);updatePreview();}
function setTool(value){tool=value;$$('[data-tool]').forEach(b=>b.classList.toggle('active',b.dataset.tool===value));stage?.setTool(value);$('#stage-hint').textContent=value==='add'?'Click the floor to place a waypoint. Esc to finish.':value==='rotate'?'Drag an arrow, or click the floor to face that direction.':'Drag a waypoint to move. Drag its arrow to turn.';}
function addPoint(x,y){if(project.waypoints.length>=64)return toast('An animation can contain up to 64 waypoints.',true);const previous=project.waypoints[selected];commit(p=>{const next=waypoint(x,y,previous.heading,{speed:previous.speed,face:previous.face,direction:previous.direction,path:previous.path});p.waypoints.splice(selected+1,0,next);selected++;});currentTime=plan.arrivals[selected];updatePreview();}
function duplicatePoint(){if(project.waypoints.length>=64)return;commit(p=>{p.waypoints.splice(selected+1,0,clone(p.waypoints[selected]));selected++;});toast('Duplicated in place. Change its heading for a turn.');}
function deletePoint(){if(project.waypoints.length===1)return toast('Keep one waypoint as the starting pose.');commit(p=>{p.waypoints.splice(selected,1);selected=Math.min(selected,p.waypoints.length-1);});}
function replaceProject(value,options={}){const next=validateProject(value);saveLocal(false);pushUndo();project=next;selected=0;currentTime=0;playing=false;localId=options.localId||createLocalId();serverId=options.serverId||null;serverSnapshot=serverId?JSON.stringify(next):'';rebuild();stage?.fit();setTool('select');}

function rebuild(){
  plan=compile(project);selected=Math.min(selected,project.waypoints.length-1);currentTime=Math.min(currentTime,plan.duration);
  $('#project-name').value=project.name;$('#point-count').textContent=project.waypoints.length;
  $('#snap-size').textContent=`${project.settings.gridSize} cm`;$('#stage-size').textContent=`${project.settings.stageSize} × ${project.settings.stageSize} cm`;
  $('#total-time').textContent=fmt(plan.duration);$('#total-distance').textContent=plan.distance.toFixed(1);$('#track-width').textContent=`${project.settings.wheelBase} cm track`;
  renderList();renderInspector();renderTimeline();stage?.setPlan(plan,selected);updatePreview();persist();updateHistory();updateDevice();
}
function renderList(){
  $('#waypoint-list').innerHTML=project.waypoints.map((p,i)=>{
    const previous=project.waypoints[i-1],stationary=previous&&Math.hypot(p.x-previous.x,p.y-previous.y)<.01;
    const mode=i===0?'Starting pose':stationary?'Turn in place':p.direction==='reverse'?'Reverse':'Forward';
    return `<button class="point-row ${i===selected?'selected':''} ${p.direction==='reverse'?'reverse':''} ${plan.warnings.some(w=>w.index===i)?'invalid':''}" data-point="${i}" aria-label="Select waypoint ${i+1}" aria-pressed="${i===selected}"><span class="point-index">${String(i+1).padStart(2,'0')}</span><span><strong>${i===0?'Start position':`Waypoint ${String(i+1).padStart(2,'0')}`}</strong><small>${mode}${p.hold?` · ${p.hold.toFixed(1)}s pause`:''}</small></span><span class="point-type" aria-hidden="true">${i===0?'◎':stationary?'⟳':p.direction==='reverse'?'↙':'↗'}</span></button>`;
  }).join('');
}
function renderInspector(){
  const p=project.waypoints[selected],first=selected===0;
  $('#selected-title').textContent=first?'Start position':`Waypoint ${String(selected+1).padStart(2,'0')}`;$('#selected-number').textContent=String(selected+1).padStart(2,'0');
  for(const [key,id] of [['x','x'],['y','y'],['heading','heading'],['speed','speed'],['hold','hold'],['tension','tension'],['path','path'],['face','face']])$('#point-'+id).value=p[key];
  $('#heading-output').textContent=`${Math.round(p.heading)}°`;$('#heading-dial').setAttribute('aria-valuenow',p.heading);$('#dial-arrow').style.transform=`rotate(${90-p.heading}deg)`;
  $('#speed-output').textContent=`${p.speed} cm/s`;$('#tension-output').textContent=`${Math.round(p.tension*100)}%`;
  $$('[data-direction]').forEach(b=>{b.classList.toggle('active',b.dataset.direction===p.direction);b.disabled=first;});
  for(const id of ['point-path','point-speed','point-tension'])$('#'+id).disabled=first;
  $('#tension-field').hidden=p.path!=='curve'||first;$('#first-point-note').hidden=!first;
  $('#cue-summary').hidden=!hasMedia(p);$('#cue-summary').textContent=cueLabel(p);$('#edit-cues').textContent=hasMedia(p)?'Edit media & speech ↗':'Media & speech ↗';
  $('#face-next').disabled=selected===project.waypoints.length-1;$('#delete-point').disabled=project.waypoints.length===1;$('#duplicate-point').disabled=project.waypoints.length>=64;$('#move-up').disabled=first;$('#move-down').disabled=selected===project.waypoints.length-1;
  const warning=plan.warnings.find(w=>w.index===selected)||plan.warnings[0],validation=$('#path-validation');validation.classList.toggle('invalid',!!warning);
  validation.querySelector('span').textContent=warning?'!':'✓';validation.querySelector('strong').textContent=warning?'Adjust the path':'Movement looks good';validation.querySelector('small').textContent=warning?warning.message.replace(/^Waypoint \d+: /,''): 'Within motion limits';
}
function renderTimeline(){
  const duration=Math.max(.1,plan.duration),totalMarks=Math.max(2,Math.min(9,Math.floor($('#timeline-tracks').clientWidth/75)));
  $('#timeline-ruler').innerHTML=Array.from({length:totalMarks+1},(_,i)=>`<span style="left:${i/totalMarks*100}%">${(duration*i/totalMarks).toFixed(duration<10?1:0)}s</span>`).join('');
  $('#motion-track').innerHTML=plan.segments.map(s=>`<div class="timeline-block ${s.kind} ${s.direction<0?'reverse':''}" style="left:${s.start/duration*100}%;width:${s.duration/duration*100}%" title="${s.kind==='hold'?'Pause':s.kind==='turn'?'Turn in place':s.direction<0?'Reverse':'Forward'} · ${s.duration.toFixed(2)}s">${s.duration/duration>.06?(s.kind==='hold'?'Ⅱ Pause':s.kind==='turn'?'⟳ Turn':s.direction<0?'↙ Reverse':'↗ Forward'):''}</div>`).join('');
  $('#expression-track').innerHTML=project.waypoints.map((p,i)=>{const start=plan.arrivals[i]||0,end=plan.arrivals[i+1]??plan.duration,label=p.cue.screen==='face'?p.face:p.cue.screen==='video'?'▶ Video':'▧ Image';return `<div class="expression-block screen-${p.cue.screen}" style="left:${start/duration*100}%;width:${(end-start)/duration*100}%" title="${esc(cueLabel(p))}">${(end-start)/duration>.07?esc(label):''}</div>`;}).join('');
  $('#audio-track').innerHTML=plan.cues.filter(c=>c.sound||c.speech).map(c=>{const next=plan.arrivals[c.index+1]??plan.duration,end=Math.min(next,c.start+c.duration);return `<div class="audio-block" style="left:${c.start/duration*100}%;width:${Math.max(0,end-c.start)/duration*100}%" title="${esc(c.speech||'Sound cue')}">${(end-c.start)/duration>.06?(c.sound?'♫ Sound':'')+(c.sound&&c.speech?' + ':'')+(c.speech?'Speech':''):''}</div>`;}).join('');
  $('#scrubber').max=Math.max(.001,plan.duration);$('#scrubber').disabled=!plan.duration;
}
function updatePreview(){
  const pose=capturePose||sampleAt(plan,currentTime);stage?.setPose(pose);
  mediaPreview.update(plan,currentTime,playing,playbackRate);
  $('#current-time').textContent=fmt(currentTime);$('#scrubber').value=currentTime;$('#playhead').style.left=`${currentTime/Math.max(.001,plan.duration)*100}%`;
  $('#play').textContent=playing?'Ⅱ':'▶';$('#play').setAttribute('aria-label',playing?'Pause preview':'Play preview');$('#play').disabled=!plan.duration;
  $('#live-speed').textContent=(pose.kind==='move'?pose.v*pose.direction:0).toFixed(1);$('#live-heading').textContent=Math.round(deg(wrap(pose.heading)));
  $('#movement-state').textContent=!playing?'Ready to preview':pose.kind==='hold'?'Pausing':pose.kind==='turn'?'Turning in place':pose.direction<0?'Driving in reverse':'Driving forward';
  if(capturePose)$('#movement-state').textContent='Recording Robbi · motors locked';
  if(capturePose){$('#current-time').textContent=fmt(capturePose.t);$('#total-time').textContent='01:00.0';$('#total-distance').textContent=captureDistance.toFixed(1);}
  if(stage&&!$('#stage-selection').hidden){const p=project.waypoints[selected],point=stage.screenPoint(p.x,p.y,5),box=$('#viewport').getBoundingClientRect();$('#stage-selection').style.left=`${point.x-box.left}px`;$('#stage-selection').style.top=`${point.y-box.top}px`;$('#stage-point-label').textContent=`${p.x.toFixed(0)}, ${p.y.toFixed(0)} · ${p.heading.toFixed(0)}°`;}
}
function togglePlay(){if(!plan.duration)return;if(currentTime>=plan.duration-.001)currentTime=0;playing=!playing;lastFrame=performance.now();updatePreview();}
function frame(){const now=performance.now(),dt=Math.min(.1,(now-lastFrame)/1000);lastFrame=now;if(playing){currentTime=mediaPreview.limitTime(plan,currentTime,currentTime+dt*playbackRate);if(currentTime>=plan.duration){if(loop)currentTime%=plan.duration;else{currentTime=plan.duration;playing=false;}}}updatePreview();}

function saveLocal(show=true){
  const existing=library.find(c=>c.id===localId);if(existing){existing.project=clone(project);existing.updated=Date.now();}else library.unshift({id:localId,project:clone(project),updated:Date.now()});
  library=library.sort((a,b)=>b.updated-a.updated).slice(0,100);
  try{localStorage.setItem(LIBRARY,JSON.stringify(library));persist();renderLibrary();if(show)toast('Animation saved to your browser library.');}catch{toast('Browser storage is full. Download your animation instead.',true);}
}
function renderLibrary(){
  $('#local-library').innerHTML=library.map(c=>`<div class="library-item"><button data-open-local="${esc(c.id)}" title="${esc(c.project.name)}">${esc(c.project.name)}</button><button class="icon-button" data-delete-local="${esc(c.id)}" aria-label="Delete ${esc(c.project.name)} from browser library">×</button></div>`).join('')||'<div class="empty-library">Your saved movements will live here.</div>';
}
async function api(path,body,method){
  const response=await fetch('/api/motion-studio'+path,{method:method||(body?'POST':'GET'),headers:body?{'Content-Type':'application/json'}:{},...(body?{body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(6000)});
  const data=await response.json().catch(()=>({error:`Device returned HTTP ${response.status}`}));if(!response.ok)throw Error(data.error||`Device returned HTTP ${response.status}`);return data;
}
async function loadDeviceLibrary(){try{const result=await api('/library');$('#device-library').innerHTML=result.clips.map(c=>`<div class="library-item"><button data-open-device="${c.id}" title="${esc(c.name)}">${esc(c.name)}</button><button class="icon-button" data-delete-device="${c.id}" aria-label="Delete ${esc(c.name)} from device library">×</button></div>`).join('')||'<div class="empty-library">No animations on this device yet.</div>';}catch(error){$('#device-library').textContent=error.message;}}
async function pollDevice(){
  if(statusBusy)return;statusBusy=true;
  try{device=await api('/status');updateDevice();}catch(error){device={ready:false,device:'Device unavailable',reason:error.message};updateDevice();}finally{statusBusy=false;clearTimeout(statusTimer);if($('#transfer-dialog').open||device?.playback?.state==='running')statusTimer=setTimeout(pollDevice,1700);}
}
function updateDevice(){
  if(!device)return;
  $('#device-name').textContent=device.device;$('#device-library-label').textContent=`${device.device.toUpperCase()} LIBRARY`;$('#device-readiness').textContent=device.ready?'Ready':'Preview';$('#device-reason').textContent=device.reason||'Calibrated encoders and obstacle protection are ready. Playback moves the physical robot.';
  $('#device-dot').style.background=device.ready?'#b9efb3':'#8195a3';
  const saved=serverId&&serverSnapshot===JSON.stringify(project),running=device.playback?.state==='running',motion=plan.segments.some(s=>s.kind==='move'||s.kind==='turn'),caps=device.capabilities||{},needAudio=project.waypoints.some(p=>p.cue.sound||p.cue.speech),needSpeech=project.waypoints.some(p=>p.cue.speech),needVideo=project.waypoints.some(p=>p.cue.screen==='video');
  const ready=!!caps.display&&(!motion||device.ready)&&(!needAudio||caps.audio)&&(!needSpeech||caps.speech)&&(!needVideo||caps.video);
  if(!motion&&caps.display){$('#device-readiness').textContent='Screen ready';$('#device-reason').textContent=needAudio&&!caps.audio?'Enable Robbi’s audio output for this clip.':'This clip uses screen and media cues. It does not need motor calibration.';}
  if(needSpeech&&!caps.speech&&device.device==='Robbi')$('#device-reason').textContent='Configure Robbi’s speech provider for this clip.';
  if(needVideo&&!caps.video&&device.device==='Robbi')$('#device-reason').textContent='Video playback is unavailable on Robbi.';
  $('#save-device').textContent=saved?'Saved to device ✓':'Save to device library';$('#play-robot').disabled=!ready||!saved||!!plan.warnings.length||running;
  $('#stop-robot').hidden=!running;$('#live-stop').hidden=!running;
  $('#play-robot').title=!saved?'Save the current animation to this device first.':plan.warnings.length?'Resolve path warnings before physical playback.':'';
  const playback=device.playback;
  $('#robot-playback-status').textContent=running?`Playing ${playback.name}: ${playback.completed} / ${playback.total} beats`:(playback?.state==='failed'?`Playback stopped: ${playback.reason}`:playback?.state==='completed'?'Playback completed.':playback?.state==='cancelled'?'Playback stopped.':!saved?'Save to the device library to enable playback.':'');
  $('#export-warnings').hidden=!plan.warnings.length;$('#export-warnings').textContent=plan.warnings.map(w=>w.message).join(' ');
}
function openTransfer(){const bundled=project.waypoints.some(p=>p.cue.screen!=='face'||p.cue.sound);$('#download-button strong').textContent=bundled?'Download animation + media':'Download animation';$('#download-button small').textContent=bundled?'Portable .robbi.zip · animation, images, videos, and sounds':'Editable .robbi.json · waypoints, expressions, and speech';$('#export-name').textContent=project.name;$('#export-summary').textContent=`${project.waypoints.length} waypoints · ${plan.duration.toFixed(1)}s · ${plan.distance.toFixed(0)} cm`;$('#transfer-dialog').showModal();updateDevice();pollDevice();}
async function saveDevice(){
  const savedProject=clone(project),savedLocalId=localId;
  const result=await api('/library',{id:serverId,project:savedProject});
  if(localId===savedLocalId){serverId=result.id;serverSnapshot=JSON.stringify(savedProject);}
  updateDevice();await loadDeviceLibrary();toast(`Saved ${savedProject.name} to ${device?.device||'device'} library.`);return result;
}
async function download(){
  const media=project.waypoints.some(p=>p.cue.screen!=='face'||p.cue.sound);let blob;
  if(media){const response=await fetch('/api/motion-studio/bundle/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project}),signal:AbortSignal.timeout(30000)});if(!response.ok){const data=await response.json();throw Error(data.error||'The media bundle could not be exported.');}blob=await response.blob();}
  else blob=new Blob([JSON.stringify(validateProject(project),null,2)+'\n'],{type:'application/json'});
  const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=(project.name.toLowerCase().replace(/[^a-z0-9_-]+/g,'-').replace(/^-|-$/g,'')||'robbi-animation')+(media?'.robbi.zip':'.robbi.json');a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast(media?'Animation and media downloaded together.':'Animation downloaded.');
}
async function guarded(button,operation){if(button.dataset.busy)return;button.dataset.busy='1';button.disabled=true;try{await operation();}catch(error){toast(error.message,true);}finally{delete button.dataset.busy;button.disabled=false;updateDevice();}}

$('#point-face').innerHTML=FACES.map(face=>`<option value="${face}">${title(face)}</option>`).join('');
const cueEditor=createCueEditor({getPoint:()=>project.waypoints[selected],apply:point=>{mediaPreview.stopAudition();playing=false;commit(p=>p.waypoints[selected]=point);currentTime=plan.arrivals[selected]||0;updatePreview();},preview:point=>{playing=false;mediaPreview.audition(point);},stopPreview:()=>{playing=false;mediaPreview.stopAudition();},notify:toast,onCatalog:entries=>mediaPreview.setCatalog(entries)});
const liveStop=document.createElement('button');liveStop.id='live-stop';liveStop.textContent='■ Stop Robbi';liveStop.className='danger-button';liveStop.hidden=true;$('.top-actions').prepend(liveStop);
const recordButton=document.createElement('button');recordButton.id='record-button';recordButton.className='text-button';recordButton.textContent='● Record Robbi';$('.workspace-meta').prepend(recordButton);
let captureTrail=[];
const capture=createCapture({api,getProject:()=>clone(project),notify:toast,onImport:result=>{replaceProject(result.project,{serverId:result.id});saveLocal(false);loadDeviceLibrary();},onLive:state=>{
  playing=false;mediaPreview.reset();
  for(const selector of ['.editor','.topbar','.workspace-heading','.app-footer'])$(selector).inert=!!state;
  document.body.classList.toggle('capturing',!!state);
  $('.status-pill').lastChild.textContent=state?' Recording':' Simulation';
  if(state){const p=state.pose;capturePose={...p,heading:rad(p.heading),face:'happy',kind:'move',direction:p.v<0?-1:1,v:Math.abs(p.v)};
    if(!captureTrail.length||p.t<captureTrail.at(-1).t){captureTrail=[];captureDistance=0;}
    if(!captureTrail.length||p.t>captureTrail.at(-1).t){const last=captureTrail.at(-1);if(last)captureDistance+=Math.hypot(p.x-last.x,p.y-last.y);captureTrail.push(p);}
    stage?.setCapture(captureTrail);
    $('#stage-hint').textContent=state.mode==='imu'?'Live IMU · turn or tilt Robbi. Position stays at the origin.':'Live sensors · roll gently with both wheels on the floor.';
  }else{capturePose=null;captureTrail=[];stage?.setCapture(null);setTool(tool);rebuild();}
  updatePreview();
}});
recordButton.addEventListener('click',()=>capture.open());
$('#project-name').addEventListener('change',event=>{const name=event.target.value.trim();if(!name){event.target.value=project.name;return;}commit(p=>p.name=name);});
$('#undo').addEventListener('click',undo);$('#redo').addEventListener('click',redo);$('#save-button').addEventListener('click',()=>saveLocal());
$('#waypoint-list').addEventListener('click',event=>{const button=event.target.closest('[data-point]');if(button)selectPoint(Number(button.dataset.point));});
$$('[data-tool]').forEach(b=>b.addEventListener('click',()=>setTool(b.dataset.tool)));$('#add-point').addEventListener('click',()=>setTool('add'));
$('#duplicate-point').addEventListener('click',duplicatePoint);$('#delete-point').addEventListener('click',deletePoint);
for(const [id,offset] of [['move-up',-1],['move-down',1]])$('#'+id).addEventListener('click',()=>commit(p=>{const next=selected+offset;if(next<0||next>=p.waypoints.length)return;[p.waypoints[selected],p.waypoints[next]]=[p.waypoints[next],p.waypoints[selected]];selected=next;}));
for(const key of ['x','y','heading','hold','path','face'])$('#point-'+key).addEventListener('change',event=>{if(!event.target.checkValidity()){event.target.reportValidity();return;}commit(p=>p.waypoints[selected][key]=['path','face'].includes(key)?event.target.value:Number(event.target.value));});
for(const key of ['speed','tension']){const input=$('#point-'+key);input.addEventListener('pointerdown',beginGesture);input.addEventListener('input',event=>commit(p=>p.waypoints[selected][key]=Number(event.target.value),{record:!gesture}));input.addEventListener('change',endGesture);input.addEventListener('pointercancel',endGesture);}
$$('[data-direction]').forEach(b=>b.addEventListener('click',()=>commit(p=>p.waypoints[selected].direction=b.dataset.direction)));
$('#face-next').addEventListener('click',()=>{const a=project.waypoints[selected],b=project.waypoints[selected+1];if(!b)return;let h=Math.atan2(b.y-a.y,b.x-a.x);if(b.direction==='reverse')h+=Math.PI;commit(p=>p.waypoints[selected].heading=Math.round(deg(wrap(h))));});
const dial=$('#heading-dial');let dialDragging=false;
function turnDial(event){const box=dial.getBoundingClientRect(),dx=event.clientX-(box.left+box.width/2),dy=box.top+box.height/2-event.clientY;if(Math.hypot(dx,dy)<5)return;const step=event.shiftKey?1:5,h=Math.round(deg(Math.atan2(dy,dx))/step)*step;commit(p=>p.waypoints[selected].heading=h,{record:false});}
dial.addEventListener('pointerdown',event=>{if(event.button!==0)return;beginGesture();dialDragging=true;dial.setPointerCapture(event.pointerId);turnDial(event);});dial.addEventListener('pointermove',event=>{if(dialDragging)turnDial(event);});for(const event of ['pointerup','pointercancel','lostpointercapture'])dial.addEventListener(event,()=>{if(dialDragging){dialDragging=false;endGesture();}});
dial.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)){event.preventDefault();const sign=['ArrowLeft','ArrowUp'].includes(event.key)?1:-1;commit(p=>p.waypoints[selected].heading=Math.round(deg(wrap(rad(p.waypoints[selected].heading+sign*(event.shiftKey?1:5))))));event.stopPropagation();}});
$('#snap-toggle').addEventListener('change',event=>stage?.setSnap(event.target.checked));$('#footprint-toggle').addEventListener('change',event=>stage?.setFootprints(event.target.checked));
function setView(view){stage?.setView(view);$('#view-3d').classList.toggle('active',view==='3d');$('#view-top').classList.toggle('active',view==='top');}
$('#view-3d').addEventListener('click',()=>setView('3d'));$('#view-top').addEventListener('click',()=>setView('top'));$('#fit-view').addEventListener('click',()=>stage?.fit());
$('#play').addEventListener('click',togglePlay);$('#rewind').addEventListener('click',()=>{mediaPreview.reset();playing=false;currentTime=0;updatePreview();});$('#loop').addEventListener('click',()=>{loop=!loop;$('#loop').setAttribute('aria-pressed',loop);});$('#playback-speed').addEventListener('change',event=>playbackRate=Number(event.target.value));$('#scrubber').addEventListener('input',event=>{mediaPreview.reset();playing=false;currentTime=Number(event.target.value);updatePreview();});
for(const [tab,other] of [['waypoints','library'],['library','waypoints']])$('#'+tab+'-tab').addEventListener('click',()=>{$('#'+tab+'-tab').setAttribute('aria-selected','true');$('#'+other+'-tab').setAttribute('aria-selected','false');$('#'+tab+'-panel').hidden=false;$('#'+other+'-panel').hidden=true;if(tab==='library'){renderLibrary();loadDeviceLibrary();}});
$$('[data-preset]').forEach(b=>b.addEventListener('click',()=>replaceProject(example(b.dataset.preset))));$('#new-button').addEventListener('click',()=>replaceProject({format:FORMAT,name:'Untitled movement',settings:{...DEFAULT_SETTINGS},waypoints:[waypoint()]}));
$('#local-library').addEventListener('click',event=>{const open=event.target.closest('[data-open-local]'),remove=event.target.closest('[data-delete-local]');if(open){const clip=library.find(c=>c.id===open.dataset.openLocal);if(clip)replaceProject(clip.project,{localId:clip.id});}if(remove){library=library.filter(c=>c.id!==remove.dataset.deleteLocal);try{localStorage.setItem(LIBRARY,JSON.stringify(library));renderLibrary();}catch(error){toast(error.message,true);}}});
$('#device-library').addEventListener('click',async event=>{const open=event.target.closest('[data-open-device]'),remove=event.target.closest('[data-delete-device]');if(open)guarded(open,async()=>{const result=await api('/library/'+open.dataset.openDevice);replaceProject(result.project,{serverId:open.dataset.openDevice});toast('Loaded from device library.');});if(remove)guarded(remove,async()=>{await api('/library/'+remove.dataset.deleteDevice,{},'DELETE');if(serverId===remove.dataset.deleteDevice){serverId=null;serverSnapshot='';}await loadDeviceLibrary();});});
$('#refresh-library').addEventListener('click',loadDeviceLibrary);
$('#import-button').addEventListener('click',()=>$('#import-file').click());$('#import-file').addEventListener('change',async event=>{const file=event.target.files[0];if(!file)return;try{let value;if(file.name.toLowerCase().endsWith('.zip')){if(file.size>25*1024*1024)throw Error('Animation bundles must be smaller than 25 MiB.');const form=new FormData();form.append('file',file);const response=await fetch('/api/motion-studio/bundle/import',{method:'POST',body:form,signal:AbortSignal.timeout(30000)}),data=await response.json();if(!response.ok)throw Error(data.error||'The animation bundle could not be imported.');value=data.project;mediaPreview.setCatalog(data.media);}else{if(file.size>512*1024)throw Error('Animation JSON files must be smaller than 512 KiB.');value=JSON.parse(await file.text());}replaceProject(validateProject(value));cueEditor.refresh().catch(error=>toast('Imported, but the media library could not refresh: '+error.message,true));toast('Animation imported.');}catch(error){toast('Import failed: '+error.message,true);}finally{event.target.value='';}});
$('#transfer-button').addEventListener('click',openTransfer);$('#download-button').addEventListener('click',event=>guarded(event.currentTarget,download));$('#save-device').addEventListener('click',event=>guarded(event.currentTarget,saveDevice));
$('#play-robot').addEventListener('click',event=>guarded(event.currentTarget,async()=>{const result=await api('/play',{id:serverId});device.playback=result;playing=false;updateDevice();pollDevice();}));
async function stopRobot(button){await guarded(button,async()=>{await api('/stop',{});await pollDevice();toast('Stop requested on Robbi.');});}
$('#stop-robot').addEventListener('click',event=>stopRobot(event.currentTarget));$('#live-stop').addEventListener('click',event=>stopRobot(event.currentTarget));
$('#setup-button').addEventListener('click',()=>{$$('[data-setting]').forEach(input=>input.value=project.settings[input.dataset.setting]);$('#setup-dialog').showModal();pollDevice();});
$('#apply-setup').addEventListener('click',()=>{for(const input of $$('[data-setting]'))if(!input.checkValidity()){input.reportValidity();return;}commit(p=>$$('[data-setting]').forEach(input=>p.settings[input.dataset.setting]=Number(input.value)));$('#setup-dialog').close();stage?.fit();});
$('#match-robot').addEventListener('click',async event=>guarded(event.currentTarget,async()=>{await pollDevice();const g=device?.geometry;if(!g?.wheel_base_cm||!g?.wheel_diameter_cm)throw Error('Robot geometry is unavailable. Open this editor on Robbi when its runtime is ready.');$('[data-setting=wheelBase]').value=g.wheel_base_cm;$('[data-setting=wheelDiameter]').value=g.wheel_diameter_cm;toast('Geometry copied from Robbi. Apply settings to use it.');}));
for(const id of ['help-button','footer-help'])$('#'+id).addEventListener('click',()=>$('#help-dialog').showModal());
document.addEventListener('keydown',event=>{
  if(capture.active())return;
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s'&&!$('dialog[open]')){event.preventDefault();if(event.target.id==='project-name')event.target.blur();saveLocal();return;}
  if(event.target.closest('input,select,textarea,[contenteditable=true]')||$('dialog[open]'))return;
  const key=event.key.toLowerCase(),ctrl=event.ctrlKey||event.metaKey;
  if(ctrl&&['z','y','s','d'].includes(key)){event.preventDefault();if(key==='z')event.shiftKey?redo():undo();if(key==='y')redo();if(key==='s')saveLocal();if(key==='d')duplicatePoint();return;}
  if(event.code==='Space'){event.preventDefault();togglePlay();}
  if(key==='a')setTool('add');if(key==='v'||key==='escape')setTool('select');if(key==='r')setTool('rotate');if(key==='f')stage?.fit();if(key==='t')setView($('#view-top').classList.contains('active')?'3d':'top');if(key==='delete'||key==='backspace'){event.preventDefault();deletePoint();}
  if(['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();const step=event.shiftKey?1:project.settings.gridSize;commit(p=>{const w=p.waypoints[selected];if(tool==='rotate'){const sign=['ArrowLeft','ArrowUp'].includes(event.key)?1:-1;w.heading=Math.round(deg(wrap(rad(w.heading+sign*(event.shiftKey?1:5)))));}else{if(event.key==='ArrowUp')w.y+=step;if(event.key==='ArrowDown')w.y-=step;if(event.key==='ArrowLeft')w.x-=step;if(event.key==='ArrowRight')w.x+=step;}});}
});
document.addEventListener('visibilitychange',()=>{if(document.hidden){playing=false;updatePreview();}else lastFrame=performance.now();});
window.addEventListener('pagehide',()=>{persist();clearTimeout(statusTimer);capture.dispose();mediaPreview.dispose();stage?.dispose();});
const timelineObserver=new ResizeObserver(renderTimeline);timelineObserver.observe($('#timeline-tracks'));

rebuild();renderLibrary();pollDevice();cueEditor.refresh().catch(()=>{});
try{
  stage=await createStage($('#viewport'),{
    select:selectPoint,add:addPoint,
    begin:()=>{beginGesture();$('#stage-selection').hidden=false;},end:endGesture,
    move:(i,x,y)=>commit(p=>{p.waypoints[i].x=Math.max(-250,Math.min(250,x));p.waypoints[i].y=Math.max(-250,Math.min(250,y));},{record:false}),
    rotate:(i,h)=>commit(p=>p.waypoints[i].heading=h,{record:false}),frame,
    modelLoaded:()=>$('#model-status').textContent='CAD SHELL · 1:1 SCALE',
    modelError:error=>{$('#model-status').textContent='CAD MODEL UNAVAILABLE';toast(error.message,true);},
  });
  stage.setPlan(plan,selected);stage.fit();updatePreview();
}catch(error){$('#scene-loading')?.remove();const notice=document.createElement('div');notice.className='viewport-error';notice.textContent='The 3D view could not start. Enable WebGL in your browser and reload. '+error.message;$('#viewport').append(notice);toast('3D view unavailable. Your animation can still be edited and exported.',true);}
// Read-only diagnostics for repeatable geometry and pointer-interaction checks.
window.robbiStudio={snapshot:()=>({project:clone(project),selected,currentTime,playing,duration:plan.duration,warnings:clone(plan.warnings)}),screenPoint:(x,y,height)=>stage?.screenPoint(x,y,height)};
