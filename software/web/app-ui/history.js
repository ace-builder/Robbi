import {$,api,esc,title,formatTime,issue,empty} from '../core.js';

export function mountToolHistory(target,life,{appId=null,onReuse=null,onUndone=()=>{},onJob=()=>{}}={}){
  target.innerHTML=`<div class="panel-head"><h2>Recent actions</h2><button type="button" class="small" data-history-refresh>Refresh</button></div><p class="hint">Undo is available for supported saved changes in this session.${onReuse?' Reuse inputs to edit a previous request before running it again.':''}</p><div data-history-list>${empty('No recent actions yet.')}</div><div data-history-feedback role="status" aria-live="polite"></div>`;
  let entries=[],loading=false,again=false,pending=false,timer=null;
  const feedback=$('[data-history-feedback]',target),list=$('[data-history-list]',target);
  function render(){list.innerHTML=entries.slice(0,8).map(item=>{
    const available=item.undo?.available&&!pending;
    return `<div class="list-row"><div class="grow"><strong>${esc(item.app?title(item.app)+' · '+title(item.tool):title(item.operation||'Saved change'))}</strong><p class="hint">${esc(formatTime(item.created_at))}${item.undo?.reason?' · '+esc(item.undo.reason):''}</p></div><div class="actions"><button type="button" class="small" data-history-undo="${esc(item.id)}" ${available?'':'disabled'}>Undo</button>${onReuse&&item.kind!=='undo'?`<button type="button" class="small" data-history-reuse="${esc(item.id)}" ${pending?'disabled':''}>Reuse inputs</button>`:''}</div></div>`;
  }).join('')||empty('No recent actions in this browser session.')}
  async function refresh(){if(life.signal.aborted)return;if(loading){again=true;return}loading=true;try{const result=await api(appId?`/api/apps/${encodeURIComponent(appId)}/history`:'/api/tool-history',{signal:life.signal});if(!life.signal.aborted){entries=result.entries||[];render()}}catch(error){if(!life.signal.aborted)issue(feedback,error)}finally{loading=false;if(again&&!life.signal.aborted){again=false;refresh()}}}
  async function poll(path,attempt=0){
    if(life.signal.aborted)return;
    try{const response=await api(path,{signal:life.signal});if(life.signal.aborted)return;const job=response.job||response;
      if(['done','failed','cancelled'].includes(job.state)){pending=false;feedback.textContent=job.result?.text||job.error||'Undo completed.';await refresh();if(job.result?.ok)onUndone(job.result);return}
      if(attempt>=120){pending=false;feedback.textContent='Undo is taking longer than expected. Check status before trying again.';await refresh();return}
      timer=setTimeout(()=>poll(path,attempt+1),1000);
    }catch(error){if(!life.signal.aborted){pending=false;issue(feedback,error);refresh()}}
  }
  life.on(target,'click',async event=>{
    if(event.target.closest('[data-history-refresh]'))return refresh();
    const undo=event.target.closest('[data-history-undo]'),reuse=event.target.closest('[data-history-reuse]');if((!undo&&!reuse)||pending)return;
    const entry=entries.find(item=>item.id===(undo?.dataset.historyUndo||reuse?.dataset.historyReuse));if(!entry)return;
    pending=true;render();feedback.textContent=undo?'Requesting undo…':'Loading previous inputs…';
    try{
      if(reuse){const inputs=await api(`/api/app-jobs/${encodeURIComponent(entry.id)}/inputs`,{signal:life.signal});if(!life.signal.aborted){onReuse(inputs);feedback.textContent='Previous inputs loaded. Edit them and choose Run tool.'}pending=false;render();return}
      const isApp=!!entry.app;
      const {job}=await api(isApp?`/api/app-jobs/${encodeURIComponent(entry.id)}/undo`:`/api/tool-history/${encodeURIComponent(entry.id)}/undo`,{method:'POST',body:isApp?{}:{kind:'productivity'},signal:life.signal});
      if(life.signal.aborted)return;if(isApp)onJob(job);
      poll(isApp?`/api/app-jobs/${encodeURIComponent(job.id)}`:`/api/jobs/${encodeURIComponent(job.id)}`);
    }catch(error){if(!life.signal.aborted){pending=false;issue(feedback,error);refresh()}}
  });
  life.on(document,'robbi:tool-change',refresh);life.add(()=>clearTimeout(timer));refresh();return refresh;
}
