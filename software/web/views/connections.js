import {$,api,esc,action,issue} from '../core.js';

export async function connections(root,life){
  root.innerHTML=`<details class="setting-group" open><summary>Connections & API keys</summary><div class="details-body">
    <p>Connect a language model or Tavily search. Keys stay on Robbi and are never returned by the dashboard.</p>
    <form id="connection-form" autocomplete="off"><div class="form-grid">
      <label>Service<select id="connection-provider" disabled><option>Loading…</option></select></label>
      <label>API key <span id="connection-key-status" class="muted"></span><input id="connection-key" type="password" autocomplete="new-password" spellcheck="false" maxlength="4096" placeholder="Leave blank to keep the saved key"></label>
      <label id="connection-endpoint-field" hidden>API base URL<input id="connection-endpoint" type="url" placeholder="http://llm-server:11434/v1"><span class="muted">Ollama: port 11434/v1 · LM Studio: port 1234/v1. Use the server’s LAN address reachable from Robbi.</span></label>
      <label id="connection-model-field">Model ID<input id="connection-model" list="connection-models" maxlength="200" placeholder="Enter any model ID supported by this provider"><datalist id="connection-models"></datalist></label>
    </div><label class="check section-gap"><input type="checkbox" id="connection-activate"><span id="connection-activate-label">Use for typed and spoken conversations</span></label>
    <div class="actions"><button type="submit">Save connection</button><button type="button" id="connection-test" class="secondary">Test connection</button><button type="button" id="connection-discover" class="secondary">Find models</button></div>
    <p class="muted">Tests send a small request to your selected provider. A successful connection test checks JSON replies; tool ability depends on the model. Separate conversation routes remain available below.</p>
    <div id="connection-result" role="status" aria-live="polite"></div></form>
  </div></details>`;
  let snapshot, drafts={};
  const value=()=>({provider:$('#connection-provider').value,api_key:$('#connection-key').value,
    model:$('#connection-model').value.trim(),base_url:$('#connection-provider').value==='local'?$('#connection-endpoint').value.trim():undefined,
    activate:$('#connection-activate').checked});
  function select(){
    const id=$('#connection-provider').value, selected=snapshot.providers.find(p=>p.id===id), search=id==='tavily';
    $('#connection-key').value='';$('#connection-key-status').textContent=selected.key_configured?'(saved)':'(not set)';
    $('#connection-endpoint-field').hidden=id!=='local';$('#connection-endpoint').value=selected.base_url||'';
    $('#connection-model-field').hidden=search;$('#connection-discover').hidden=search;$('#connection-models').innerHTML='';
    $('#connection-model').value=drafts[id]||(snapshot.active.provider===id?snapshot.active.model:'');
    $('#connection-activate').checked=search?snapshot.search_enabled:snapshot.active.provider===id;
    $('#connection-activate-label').textContent=search?'Enable web search':'Use for typed and spoken conversations';
    $('#connection-result').textContent='';
  }
  life.on($('#connection-provider'),'change',select);
  life.on($('#connection-model'),'input',()=>{drafts[$('#connection-provider').value]=$('#connection-model').value});
  life.on($('#connection-form'),'submit',async event=>{
    event.preventDefault();const result=await action($('button[type=submit]',root),()=>api('/api/connections/save',{method:'POST',body:value(),timeout:20000}),{output:$('#connection-result'),success:'Connection saved on Robbi.'});
    if(result&&!life.signal.aborted){snapshot=result;$('#connection-key').value='';$('#connection-key-status').textContent=snapshot.providers.find(p=>p.id===$('#connection-provider').value).key_configured?'(saved)':'(not set)';$('#connection-result').textContent='Saved. Applied immediately.'}
  });
  for(const operation of ['test','models']){
    const button=$(operation==='test'?'#connection-test':'#connection-discover');
    life.on(button,'click',async()=>{
      const result=await action(button,()=>api('/api/connections/'+operation,{method:'POST',body:value(),timeout:25000}),{output:$('#connection-result'),success:null});
      if(!result||life.signal.aborted)return;
      if(operation==='models'){$('#connection-models').innerHTML=result.models.map(m=>`<option value="${esc(m)}"></option>`).join('');$('#connection-result').textContent=`Found ${result.models.length} models. Select one or type its exact ID.`}
      else $('#connection-result').textContent=result.message+` (${result.elapsed_ms} ms)`+(result.results?'\n'+result.results.map(r=>r.title).join('\n'):'');
    });
  }
  try{snapshot=await api('/api/connections',{signal:life.signal});if(life.signal.aborted)return;$('#connection-provider').innerHTML=snapshot.providers.map(p=>`<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');$('#connection-provider').disabled=false;$('#connection-provider').value=snapshot.active.provider;select()}catch(e){if(!life.signal.aborted)issue($('#connection-result'),e)}
}
