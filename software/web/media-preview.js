import {$,api} from './core.js';

export function mediaPreview(root,life){
  const dialog=document.createElement('dialog');dialog.className='media-dialog';
  dialog.innerHTML='<div class="panel-head"><div><h2 id="preview-title">Preview</h2><p class="hint">On this device only</p></div><button type="button" id="preview-close" aria-label="Close preview">✕</button></div><div id="preview-stage"></div><p id="preview-status" role="status"></p>';
  dialog.setAttribute('aria-labelledby','preview-title');root.append(dialog);
  const stage=$('#preview-stage',dialog),status=$('#preview-status',dialog);
  let epoch=0,frame=0,abort=null;
  function stop(){epoch++;abort?.abort();cancelAnimationFrame(frame);for(const el of stage.querySelectorAll('audio,video')){el.pause();el.removeAttribute('src');el.load()}stage.replaceChildren()}
  life.on($('#preview-close',dialog),'click',()=>dialog.close());life.on(dialog,'close',stop);
  life.on(document,'visibilitychange',()=>{if(document.hidden&&dialog.open)dialog.close()});
  life.add(()=>{stop();dialog.remove()});
  return async item=>{
    stop();const current=epoch;abort=new AbortController();$('#preview-title',dialog).textContent=item.label;status.textContent='Loading preview…';if(!dialog.open)dialog.showModal();
    try{
      const spec=await api('/api/media-library/preview?'+new URLSearchParams({kind:item.kind,name:item.name}),{signal:abort.signal});if(current!==epoch||life.signal.aborted)return;
      if(['sounds','videos','faces'].includes(spec.type)){
        const tag={sounds:'audio',videos:'video',faces:'img'}[spec.type],media=document.createElement(tag);media.src=spec.url;media.setAttribute('aria-label',item.label);
        if(tag==='img')media.alt=item.label;else{media.controls=true;media.preload='metadata';if(tag==='video')media.playsInline=true}
        media.addEventListener('error',()=>{if(current===epoch)status.textContent='Preview unavailable. The file may be missing or unsupported by this browser.'},{once:true});stage.append(media);status.textContent=tag==='img'?'':'Press play to preview here. Robbi stays unchanged.';return;
      }
      const canvas=document.createElement('canvas');canvas.width=280;canvas.height=240;canvas.setAttribute('aria-label',item.label+' screen preview');stage.append(canvas);const ctx=canvas.getContext('2d');
      const load=src=>new Promise((resolve,reject)=>{const img=new Image();img.onload=()=>resolve(img);img.onerror=()=>reject(Error('A preview image is unavailable.'));img.src=src});
      let image,frames=[],duration;
      if(spec.type==='sprite'){image=await load(spec.url);let end=0;frames=spec.order.map((cell,i)=>({cell,end:(end+=spec.durations_ms[i])}));duration=end}
      else{if(!spec.frames.length){status.textContent='This timeline has no face frames. Robot playback may include sound, video or movement.';return}duration=spec.duration_ms;const urls=[...new Set(spec.frames.map(f=>f.url))];if(urls.length>24)throw Error('This timeline is too large for a browser preview.');const images=new Map();for(const url of urls){if(current!==epoch)return;images.set(url,await load(url))}frames=spec.frames.map(f=>({...f,image:images.get(f.url)}))}
      if(current!==epoch||life.signal.aborted)return;status.textContent=spec.type==='sprite'?'Screen animation preview · silent':'Screen frames only · movement and sound stay off';
      const started=performance.now();let previous=-1;
      function tick(now){if(current!==epoch||document.hidden)return;const t=(now-started)%duration;
        const index=spec.type==='sprite'?Math.max(0,frames.findIndex(f=>t<f.end)):Math.max(0,frames.findLastIndex(f=>t>=f.at_ms));
        if(index!==previous){previous=index;ctx.clearRect(0,0,280,240);if(spec.type==='sprite'){const cell=frames[index].cell,w=image.width/spec.columns,h=image.height/spec.rows;ctx.drawImage(image,cell%spec.columns*w,Math.floor(cell/spec.columns)*h,w,h,0,0,280,240)}else ctx.drawImage(frames[index].image,0,0,280,240)}frame=requestAnimationFrame(tick)}frame=requestAnimationFrame(tick);
    }catch(e){if(current===epoch&&!life.signal.aborted)status.textContent=e.message}
  };
}
