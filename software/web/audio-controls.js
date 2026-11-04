import {$,state,api,ready,notify} from './core.js';

// Serialize output updates, coalesce slider positions, never replay offline commands.
export function audioControls(){
  const host=$('#global-audio');
  host.innerHTML=`<button id="audio-mute" type="button" class="audio-mute" aria-label="Mute Robbi" aria-pressed="false" disabled>Mute</button><label class="sr-only" for="audio-volume">Robbi speaker volume</label><input id="audio-volume" type="range" min="0" max="100" step="1" value="50" disabled><output id="audio-volume-value" for="audio-volume">—</output>`;
  const slider=$('#audio-volume'),button=$('#audio-mute'),value=$('#audio-volume-value');
  let confirmed=null,muted=false,pending=null,busy=false,timer=null,editing=false,revision=0,holdUntil=0;
  function draw(){
    const online=state.connected&&state.status;
    slider.disabled=!online||!ready('audio');button.disabled=!online||busy;
    if(!editing&&!busy&&pending===null&&confirmed!==null)slider.value=confirmed;
    value.textContent=confirmed===null?'—':`${slider.value}%`;
    slider.setAttribute('aria-valuetext',`${slider.value} percent${muted?', muted':''}`);
    button.textContent=muted?'Unmute':'Mute';button.setAttribute('aria-label',muted?'Unmute Robbi':'Mute Robbi');button.setAttribute('aria-pressed',String(muted));
    host.classList.toggle('is-muted',muted);host.title=online?(muted?'Robbi is muted':'Robot speaker volume'):'Waiting for Robbi';
  }
  function sync(){
    if(!state.connected){revision++;pending=null;editing=false;clearTimeout(timer);holdUntil=0}
    if(state.status&&!busy&&pending===null&&!editing&&Date.now()>holdUntil){
      const volume=state.status.audio?.volume_percent;if(Number.isFinite(volume))confirmed=volume;
      if(typeof state.status.behavior?.muted==='boolean')muted=state.status.behavior.muted;
    }
    draw();
  }
  async function flush(){
    clearTimeout(timer);if(busy||pending===null||!state.connected)return;
    const next=pending,epoch=revision;pending=null;busy=true;draw();
    try{const result=await api('/volume',{method:'POST',body:{volume:next},timeout:5000});if(epoch===revision){confirmed=result.volume_percent;holdUntil=Date.now()+6500}}
    catch(e){if(epoch===revision){pending=null;editing=false;notify(e.message,true)}}
    finally{busy=false;draw();if(pending!==null&&state.connected)flush()}
  }
  slider.addEventListener('input',()=>{editing=true;pending=Number(slider.value);value.textContent=`${slider.value}%`;slider.setAttribute('aria-valuetext',`${slider.value} percent`);clearTimeout(timer);timer=setTimeout(flush,180)});
  slider.addEventListener('change',()=>{editing=false;flush()});slider.addEventListener('blur',()=>{editing=false;draw()});
  button.addEventListener('click',async()=>{if(busy||!state.connected)return;const epoch=revision;busy=true;draw();try{const result=await api('/mute',{method:'POST',body:{muted:!muted},timeout:5000});if(epoch===revision){muted=result.muted;holdUntil=Date.now()+6500}}catch(e){notify(e.message,true)}finally{busy=false;draw();flush()}});
  document.addEventListener('robbi:status',sync);
  window.addEventListener('pagehide',()=>{revision++;clearTimeout(timer);pending=null});
  draw();
}
