import {CUE_DEFAULTS,FACES,validateCue} from './motion.js';
import {mediaUrl,cueLabel} from './media-preview.js';

const $=s=>document.querySelector(s);
const option=(value,label)=>{const el=document.createElement('option');el.value=value;el.textContent=label;return el;};

export function createCueEditor({getPoint,apply,preview,stopPreview,notify,onCatalog}){
  let catalog=[],draft,face,monitorVersion=0,durationVersion=0,refreshJob,soundProbe;
  const dialog=$('#cue-dialog');
  async function loadCatalog(force){
    const response=await fetch('/api/motion-studio/media'+(force?'?refresh=1':''),{signal:AbortSignal.timeout(30000)});
    const data=await response.json();if(!response.ok)throw Error(data.error||'The media library could not be loaded.');
    catalog=data.media;onCatalog(catalog);populate();return catalog;
  }
  async function refresh(force=false){
    if(refreshJob){if(!force)return refreshJob;await refreshJob.catch(()=>{});}
    const job=loadCatalog(force);refreshJob=job;
    try{return await job;}finally{if(refreshJob===job)refreshJob=null;}
  }
  function stopSoundProbe(){if(soundProbe){soundProbe.removeAttribute('src');soundProbe.load();soundProbe=null;}}
  function estimateSound(){
    stopSoundProbe();const entry=catalog.find(e=>e.id===$('#cue-sound').value);if(!entry)return;
    const setDuration=duration=>{if(Number.isFinite(duration))$('#cue-duration').value=Math.max(Number($('#cue-duration').value),Math.min(110,Math.ceil(duration*10)/10));};
    if(entry.duration){setDuration(entry.duration);return;}
    const probe=new Audio(),version=durationVersion;soundProbe=probe;probe.preload='metadata';
    probe.addEventListener('loadedmetadata',()=>{if(soundProbe===probe&&dialog.open&&version===durationVersion)setDuration(probe.duration);});
    probe.src=mediaUrl(entry.id);
  }
  function populate(){
    const screen=$('#cue-screen').value;
    const picker=$('#cue-media'),sound=$('#cue-sound'),selectedMedia=picker.value||draft?.media||'',selectedSound=sound.value||draft?.sound||'';
    picker.replaceChildren(option('','Choose '+(screen==='video'?'a video':'an image')),...catalog.filter(e=>e.kind===screen).map(e=>option(e.id,`${e.name} · ${e.source}`)));
    sound.replaceChildren(option('','No sound'),...catalog.filter(e=>e.kind==='sound').map(e=>option(e.id,`${e.name} · ${e.source}`)));
    picker.value=selectedMedia;sound.value=selectedSound;
    $('#cue-face-field').hidden=screen!=='face';$('#cue-media-field').hidden=screen==='face';
    showMonitor();
  }
  function showMonitor(estimate=false){
    const host=$('#cue-monitor'),screen=$('#cue-screen').value,selected=$('#cue-media').value;
    const previous=host.querySelector('video');
    if(!estimate&&screen==='video'&&previous?.getAttribute('src')===mediaUrl(selected))return;
    const version=++monitorVersion,initialDurationVersion=durationVersion;
    if(previous){previous.pause();previous.removeAttribute('src');previous.load();}
    host.replaceChildren();
    if(screen==='face'){const img=new Image();img.alt='Selected face';img.src='/static/motion-studio/faces/'+$('#cue-face').value+'.png';host.append(img);}
    else if(selected){
      const media=document.createElement(screen==='video'?'video':'img');media.src=mediaUrl(selected);media.setAttribute('aria-label','Selected screen media');
      if(screen==='video'){media.controls=true;media.muted=true;media.playsInline=true;media.preload='metadata';media.addEventListener('loadedmetadata',()=>{if(estimate&&dialog.open&&version===monitorVersion&&initialDurationVersion===durationVersion&&Number.isFinite(media.duration))$('#cue-duration').value=Math.max(Number($('#cue-duration').value),Math.min(110,Math.ceil(media.duration*10)/10));});}
      host.append(media);
    }else{const hint=document.createElement('span');hint.textContent='Choose or upload screen media';host.append(hint);}
    const entry=catalog.find(e=>e.id===selected);$('#cue-monitor-caption').textContent=entry?entry.name:'280 × 240 screen preview';
  }
  function read(){
    const cue=validateCue({screen:$('#cue-screen').value,media:$('#cue-screen').value==='face'?'':$('#cue-media').value,sound:$('#cue-sound').value,speech:$('#cue-speech').value,duration:Number($('#cue-duration').value),wait:$('#cue-wait').checked});
    return {...getPoint(),face:$('#cue-face').value,cue};
  }
  async function open(){
    stopPreview();draft={...getPoint().cue};face=getPoint().face;$('#cue-title').textContent='Screen, sound & speech';
    $('#cue-screen').value=draft.screen;$('#cue-face').value=face;$('#cue-speech').value=draft.speech;$('#cue-duration').value=draft.duration;$('#cue-wait').checked=draft.wait;
    $('#cue-media').replaceChildren();$('#cue-sound').replaceChildren();populate();dialog.showModal();
    try{await refresh();}catch(error){notify(error.message,true);}
  }
  $('#cue-face').replaceChildren(...FACES.map(f=>option(f,f.charAt(0).toUpperCase()+f.slice(1))));
  $('#edit-cues').addEventListener('click',open);
  $('#cue-screen').addEventListener('change',()=>{draft.media='';$('#cue-media').replaceChildren();populate();});
  $('#cue-face').addEventListener('change',()=>showMonitor());
  $('#cue-duration').addEventListener('input',()=>durationVersion++);
  $('#cue-media').addEventListener('change',()=>{
    draft.media=$('#cue-media').value;
    const entry=catalog.find(e=>e.id===$('#cue-media').value);
    if(entry?.soundtrack&&!$('#cue-sound').value){$('#cue-sound').value=entry.soundtrack;estimateSound();}
    showMonitor(true);
  });
  $('#cue-sound').addEventListener('change',()=>{draft.sound=$('#cue-sound').value;estimateSound();});
  $('#cue-refresh').addEventListener('click',async()=>{try{await refresh(true);notify('Media library refreshed.');}catch(error){notify(error.message,true);}});
  $('#cue-upload').addEventListener('click',()=>$('#cue-file').click());
  $('#cue-file').addEventListener('change',async event=>{
    const file=event.target.files[0];if(!file)return;
    const button=$('#cue-upload');button.disabled=true;button.textContent='Uploading…';
    try{
      if(file.size>12*1024*1024)throw Error('Media files are limited to 12 MiB.');
      const form=new FormData();form.append('file',file);
      const response=await fetch('/api/motion-studio/media',{method:'POST',body:form,signal:AbortSignal.timeout(30000)}),data=await response.json();
      if(!response.ok)throw Error(data.error||'Upload failed.');
      await refresh(true);const entry=data.media;
      if(entry.kind==='sound'){draft.sound=entry.id;$('#cue-sound').value=entry.id;if(entry.duration)$('#cue-duration').value=Math.max(Number($('#cue-duration').value),Math.ceil(entry.duration*10)/10);}
      else{$('#cue-screen').value=entry.kind;draft.media=entry.id;$('#cue-media').replaceChildren();populate();$('#cue-media').value=entry.id;showMonitor(true);}
      notify('Media uploaded and selected.');
    }catch(error){notify(error.message,true);}finally{button.disabled=false;button.textContent='↑ Upload media';event.target.value='';}
  });
  $('#cue-preview-button').addEventListener('click',()=>{try{preview(read());}catch(error){notify(error.message,true);}});
  $('#cue-clear').addEventListener('click',()=>{apply({...getPoint(),cue:{...CUE_DEFAULTS}});dialog.close();});
  $('#cue-apply').addEventListener('click',()=>{try{const point=read();apply(point);dialog.close();notify('Cue saved: '+cueLabel(point));}catch(error){notify(error.message,true);}});
  dialog.addEventListener('close',()=>{monitorVersion++;stopSoundProbe();stopPreview();dialog.querySelector('video')?.pause();});
  return {refresh,entries:()=>catalog};
}
