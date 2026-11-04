import {$,api,esc,heading,empty,formatTime,notify,action} from '../core.js';

const BASE='/api/audio-studio';
const activeStates=['queued','generating'];
const labels={queued:'Queued',generating:'Generating',draft:'Draft',kept:'Kept',discarded:'Discarded',failed:'Failed'};
const statusBadge=s=>`<span class="badge ${s==='failed'?'failed':s==='kept'?'ready':activeStates.includes(s)?'running':''}">${esc(labels[s]||s)}</span>`;

export function audio(root,life){
  let settings=null, clips=[], selected=null, selectedClip=null, offset=0, total=0, activeId=null;
  let timer, refreshVersion=0, previewKey='', listKey='', submitting=false, nextVoicePage='';
  const request=(path,options={})=>api(BASE+path,{...options,signal:life.signal});
  root.innerHTML=heading('Make Robbi sound like Robbi','Audio studio','Write a line, choose its delivery, then listen and keep your favourite take.', '<span class="badge">Eleven v4</span>')+`
    <div id="studio-error" class="notice error" role="alert" hidden></div>
    <div class="grid studio-grid">
      <section class="panel">
        <div class="panel-head"><h2>Create a clip</h2><span id="studio-connection" class="hint">Loading…</span></div>
        <form id="studio-compose">
          <label for="studio-text">What should Robbi say?</label>
          <textarea id="studio-text" name="text" rows="5" maxlength="10000" required placeholder="[sarcastic] Oh great. Another software update. Just what I needed."></textarea>
          <div class="studio-text-foot"><span>Keep audio directions in [square brackets].</span><span id="studio-count">0 / 10,000</span></div>
          <div class="chips studio-tags" aria-label="Insert an audio direction">
            ${['sarcastic','whispers','laughs','sighs','curious'].map(tag=>`<button type="button" class="small" data-tag="${tag}">[${tag}]</button>`).join('')}
          </div>
          <div class="studio-voice-field"><label for="studio-voice">Voice ID</label><input id="studio-voice" maxlength="128" required autocomplete="off" value="" placeholder="Choose your voice or paste its ID"></div>
          <div class="actions compact studio-voice-actions"><button type="button" id="studio-voices" class="small">Load my voices</button><button type="button" id="studio-default" class="small">Set as default</button></div>
          <label id="studio-voice-options-label" hidden>My ElevenLabs voices<select id="studio-voice-options"><option value="">Choose a voice…</option></select></label>
          <button id="studio-more-voices" type="button" class="small" hidden>Load more voices</button>
          <div class="actions"><button type="submit" class="primary" id="studio-generate" disabled>Generate audio</button><span class="hint">Uses ElevenLabs credits.</span></div>
          <p id="studio-progress" class="hint" role="status" aria-live="polite">Loading your library…</p>
        </form>
      </section>
      <section class="panel studio-preview" aria-label="Clip preview"><div class="panel-head"><h2>Listen & keep</h2></div><div id="studio-preview">${empty('Your new clip will appear here. Select a past generation below to listen again.')}</div></section>
    </div>
    <details id="studio-settings" class="section-gap"><summary>Connection settings</summary><div class="details-body">
      <form id="studio-key-form" class="studio-key-form"><label for="studio-key">ElevenLabs API key<input id="studio-key" type="password" autocomplete="new-password" maxlength="512" placeholder="Paste your API key" required><small>Saved on Robbi. Leave the current key in place by keeping this field empty.</small></label><button type="submit">Save API key</button></form>
      <p class="hint" id="studio-key-status"></p>
    </div></details>
    <section class="panel section-gap">
      <div class="panel-head studio-history-head"><div><h2>Generation history</h2><p class="hint" id="studio-storage"></p></div><div class="actions compact"><label class="sr-only" for="studio-filter">Filter history</label><select id="studio-filter"><option value="all">All generations</option><option value="kept">Kept clips</option><option value="draft">Drafts</option></select><button type="button" class="small" id="studio-refresh">Refresh</button></div></div>
      <div id="studio-history">${empty('Loading your history…')}</div>
      <div class="studio-pagination"><button type="button" class="small" id="studio-previous" disabled>Previous</button><span id="studio-page" class="hint"></span><button type="button" class="small" id="studio-next" disabled>Next</button></div>
    </section>`;
  const field=$('#studio-text',root), voice=$('#studio-voice',root), generate=$('#studio-generate',root);
  const preview=$('#studio-preview',root), history=$('#studio-history',root);
  const errorBox=$('#studio-error',root);
  const showError=error=>{errorBox.textContent=error.message;errorBox.hidden=false};
  function availability(){
    generate.disabled=!settings?.api_key_configured||!!activeId||submitting;
    generate.textContent=activeId?'Generating…':'Generate audio';
    $('#studio-progress',root).textContent=activeId?'Working in the background. You can leave this tab and return later.':settings?.api_key_configured?'Playback is in this browser. Kept clips stay on Robbi.':'Add your API key in Connection settings to begin.';
    $('#studio-connection',root).textContent=settings?.api_key_configured?'API key configured':'API key needed';
    $('#studio-key-status',root).textContent=settings?.api_key_configured?'An API key is configured. Save a new one here to replace it.':'You can also set ELEVENLABS_API_KEY in Robbi’s .env.secrets and restart.';
    const regenerate=$('[data-clip-action="regenerate"]',preview);
    if(regenerate)regenerate.disabled=!!activeId||submitting||!settings?.api_key_configured;
  }
  function renderPreview(clip){
    selectedClip=clip;
    const key=JSON.stringify(clip);
    if(key===previewKey)return;
    previewKey=key;
    if(!clip){preview.innerHTML=empty('Your new clip will appear here. Select a past generation below to listen again.');return}
    const playable=!!clip.audio_url;
    preview.innerHTML=`<div class="panel-head"><h3>${esc(clip.name||'Untitled take')}</h3>${statusBadge(clip.status)}</div>
      <p class="hint">${esc(formatTime(clip.created))}</p>
      <p class="studio-script">${esc(clip.text)}</p>
      <p class="hint studio-voice-meta">Voice ${esc(clip.voice_id)} · Eleven v4${clip.parent_id?' · Regenerated take':''}</p>
      ${clip.error?`<div class="notice error" role="alert">${esc(clip.error)}</div>`:''}
      ${activeStates.includes(clip.status)?'<p class="notice" role="status">Generating audio… Your finished clip will appear here automatically.</p>':''}
      ${playable?`<audio controls preload="metadata" src="${esc(clip.audio_url)}" aria-label="Listen to this clip"></audio><p id="studio-playback-error" class="notice error" role="alert" hidden></p>
        <form id="studio-keep" class="section-gap"><label for="studio-name">Clip name<input id="studio-name" maxlength="100" required value="${esc(clip.name)}" placeholder="e.g. Sarcastic startup greeting"></label><div class="actions"><button type="submit" class="primary">${clip.status==='kept'?'Save name':'Keep clip'}</button><a class="button-link" href="${esc(clip.audio_url)}?download=1" download>Download MP3</a>${clip.status==='draft'?'<button type="button" data-clip-action="discard">Discard</button>':''}</div></form>`:''}
      <div class="actions"><button type="button" data-clip-action="edit">Use text & voice</button><button type="button" data-clip-action="regenerate">Regenerate</button><button type="button" class="danger" data-clip-action="delete">Delete</button></div>
      <p class="hint">Regenerate creates a new take and uses credits. Discard removes audio but keeps its history. Delete removes both locally.</p>`;
    life.on($('audio',preview),'error',()=>{const box=$('#studio-playback-error',preview);if(box){box.hidden=false;box.textContent='Could not play this clip. Check the connection, download the MP3, or regenerate it.'}});
    availability();
  }
  function renderHistory(data){
    clips=data.clips;total=data.total;activeId=data.active_id;
    const key=JSON.stringify([clips,selected]);
    if(key!==listKey){
      listKey=key;
      history.innerHTML=clips.length?clips.map(c=>`<button type="button" class="studio-history-row${c.id===selected?' selected':''}" data-select="${c.id}" aria-pressed="${c.id===selected}"><span class="studio-history-copy"><strong>${esc(c.name||c.text.slice(0,80))}</strong><small>${esc(formatTime(c.created))} · ${esc(c.voice_id)}</small></span>${statusBadge(c.status)}</button>`).join(''):empty('No generations here yet. Create a clip above to get started.');
    }
    $('#studio-storage',root).textContent=`${total} ${total===1?'generation':'generations'} in this view · ${(data.storage_bytes/1048576).toFixed(1)} / ${(settings?.max_storage_bytes||134217728)/1048576} MB audio stored`;
    $('#studio-page',root).textContent=total?`${offset+1}–${Math.min(offset+20,total)} of ${total}`:'No entries';
    $('#studio-previous',root).disabled=offset===0;
    $('#studio-next',root).disabled=offset+20>=total;
    availability();
  }
  async function refresh(){
    const version=++refreshVersion;
    const data=await request(`/clips?offset=${offset}&status=${$('#studio-filter',root).value}`);
    if(life.signal.aborted||version!==refreshVersion)return;
    if(offset&&offset>=data.total){offset=Math.max(0,Math.floor((data.total-1)/20)*20);return refresh()}
    renderHistory(data);
    if(!selected&&data.active_id)selected=data.active_id;
    if(selected){
      let clip=clips.find(c=>c.id===selected);
      if(!clip){try{clip=await request(`/clips/${selected}`)}catch(e){if(e.status!==404)throw e}}
      if(life.signal.aborted||version!==refreshVersion)return;
      if(!clip)selected=null;
      renderPreview(clip||null);
    }
    errorBox.hidden=true;
  }
  async function mutate(button,work,success){
    const result=await action(button,work,{success});
    if(!life.signal.aborted){await refresh().catch(showError);availability()}
    return result;
  }
  async function newTake(work){
    if(submitting)return;
    submitting=true;availability();
    try{
      const clip=await work();
      if(life.signal.aborted)return;
      selected=clip.id;activeId=clip.id;offset=0;$('#studio-filter',root).value='all';renderPreview(clip);
      notify('Generation started. You can continue using Robbi.');
    }catch(error){if(!life.signal.aborted)notify(error.message,true)}
    finally{submitting=false;if(!life.signal.aborted){await refresh().catch(showError);availability();clearTimeout(timer);timer=setTimeout(poll,2000)}}
  }
  life.on(field,'input',()=>$('#studio-count',root).textContent=`${[...field.value].length.toLocaleString()} / 10,000`);
  life.on(root,'click',event=>{
    const button=event.target.closest('[data-tag]');if(!button)return;
    field.setRangeText(`[${button.dataset.tag}] `,field.selectionStart,field.selectionEnd,'end');
    field.dispatchEvent(new Event('input'));field.focus();
  });
  life.on($('#studio-compose',root),'submit',event=>{event.preventDefault();newTake(()=>request('/clips',{method:'POST',body:{text:field.value,voice_id:voice.value.trim()}}))});
  life.on($('#studio-default',root),'click',event=>mutate(event.currentTarget,async()=>{settings=await request('/settings',{method:'POST',body:{voice_id:voice.value.trim()}})},'Default voice saved.'));
  life.on($('#studio-key-form',root),'submit',event=>{
    event.preventDefault();const input=$('#studio-key',root);
    mutate($('button',event.currentTarget),async()=>{settings=await request('/settings',{method:'POST',body:{api_key:input.value.trim()}});input.value='';$('#studio-settings',root).open=false},'API key saved.');
  });
  async function loadVoices(button,more=false){
    await action(button,async()=>{
      const data=await request('/voices'+(more?`?page_token=${encodeURIComponent(nextVoicePage)}`:''),{timeout:25000});
      if(life.signal.aborted)return;
      const select=$('#studio-voice-options',root);
      if(!more)select.innerHTML='<option value="">Choose a voice…</option>';
      for(const v of data.voices){const option=document.createElement('option');option.value=v.voice_id;option.textContent=v.name;select.append(option)}
      select.value=voice.value;
      $('#studio-voice-options-label',root).hidden=false;
      nextVoicePage=data.next_page_token;$('#studio-more-voices',root).hidden=!nextVoicePage;
      if(!data.voices.length)notify('No voices returned. You can enter a voice ID manually.');
    },{success:''});
  }
  life.on($('#studio-voices',root),'click',event=>loadVoices(event.currentTarget));
  life.on($('#studio-more-voices',root),'click',event=>loadVoices(event.currentTarget,true));
  life.on($('#studio-voice-options',root),'change',event=>{if(event.target.value)voice.value=event.target.value});
  life.on(history,'click',event=>{const row=event.target.closest('[data-select]');if(row){selected=row.dataset.select;renderPreview(clips.find(c=>c.id===selected));refresh().catch(showError)}});
  life.on(preview,'submit',event=>{event.preventDefault();if(event.target.id==='studio-keep')mutate($('button[type=submit]',event.target),()=>request(`/clips/${selected}/keep`,{method:'POST',body:{name:$('#studio-name',preview).value}}),'Clip saved.');});
  life.on(preview,'click',event=>{
    const button=event.target.closest('[data-clip-action]');if(!button||!selectedClip)return;
    const kind=button.dataset.clipAction, id=selectedClip.id;
    if(kind==='edit'){field.value=selectedClip.text;voice.value=selectedClip.voice_id;field.dispatchEvent(new Event('input'));field.focus();return}
    if(kind==='regenerate'){newTake(()=>request(`/clips/${id}/regenerate`,{method:'POST',body:{}}));return}
    if(kind==='delete'&&!confirm('Delete this generation and its audio from Robbi? An in-flight request may already have used credits.'))return;
    mutate(button,async()=>{
      $('audio',preview)?.pause();
      await request(`/clips/${id}${kind==='discard'?'/discard':''}`,{method:kind==='delete'?'DELETE':'POST',body:{}});
      if(kind==='delete'){selected=null;renderPreview(null)}
    },kind==='delete'?'Generation deleted.':'Audio discarded; history retained.');
  });
  life.on($('#studio-filter',root),'change',()=>{offset=0;refresh().catch(showError)});
  life.on($('#studio-refresh',root),'click',event=>mutate(event.currentTarget,refresh,''));
  life.on($('#studio-previous',root),'click',()=>{offset=Math.max(0,offset-20);refresh().catch(showError)});
  life.on($('#studio-next',root),'click',()=>{offset+=20;refresh().catch(showError)});
  let polling=false;
  async function poll(){
    clearTimeout(timer);
    if(life.signal.aborted||polling)return;
    polling=true;
    try{if(!document.hidden)await refresh().catch(showError)}
    finally{polling=false;if(!life.signal.aborted)timer=setTimeout(poll,activeId?2000:15000)}
  }
  life.on(document,'visibilitychange',()=>{if(!document.hidden){clearTimeout(timer);poll()}});
  life.add(()=>{clearTimeout(timer);$('audio',preview)?.pause()});
  (async()=>{
    try{settings=await request('/settings');if(life.signal.aborted)return;voice.value=settings.voice_id;$('#studio-settings',root).open=!settings.api_key_configured;availability();await poll()}
    catch(error){if(!life.signal.aborted)showError(error)}
  })();
}
