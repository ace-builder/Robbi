import {api,esc,empty} from '../core.js';

// Server-owned turns survive navigation; only unsent/uncertain UI notices live here.
const local={pending:null,notices:[],turns:[]};

export function mountConversationFeed(chat,life){
  let timer,loading=false,signature='';
  function render(){
    const nearBottom=chat.scrollHeight-chat.scrollTop-chat.clientHeight<70;
    const rows=[];
    const row=(role,text,error=false,pending=false)=>`<div class="chat-message ${error?'error':role.startsWith('You')?'user':''}"><strong>${esc(role)}</strong>${esc(text)}${pending?'<span class="hint"> · In progress</span>':''}</div>`;
    for(const turn of local.turns){
      const voice=turn.source==='speech_input'?' · Voice':'';
      rows.push(row('You'+voice,turn.text));
      rows.push(row('Robbi'+voice,turn.reply||'Thinking…',['failed','cancelled','interrupted'].includes(turn.state),['thinking','working'].includes(turn.state)));
    }
    if(local.pending)rows.push(row('You',local.pending.text));
    for(const note of local.notices)rows.push(row(note.role,note.text,note.error));
    const html=rows.join('')||empty('Typed messages and voice conversations will appear here.');
    if(html!==signature){const first=!signature;signature=html;chat.innerHTML=html;if(nearBottom||first)chat.scrollTop=chat.scrollHeight}
  }
  async function refresh(){
    clearTimeout(timer);
    if(loading||life.signal.aborted||document.hidden)return;
    loading=true;
    try{
      const result=await api('/api/conversation',{signal:life.signal,timeout:8000});
      if(life.signal.aborted)return;
      local.turns=result.turns||[];
      if(local.pending&&local.turns.some(t=>t.source==='ui_text'&&t.text===local.pending.text&&!local.pending.ids.has(t.id)))local.pending=null;
      render();
    }catch(error){
      if(!life.signal.aborted&&!local.turns.length&&!local.pending)chat.innerHTML=empty(`Conversation unavailable. ${error.message}`);
    }finally{loading=false;if(!life.signal.aborted&&!document.hidden)timer=setTimeout(refresh,2000)}
  }
  life.on(document,'visibilitychange',()=>{clearTimeout(timer);if(!document.hidden)refresh()});
  life.add(()=>clearTimeout(timer));render();refresh();
  return {refresh,
    pending(text){local.pending={text,ids:new Set(local.turns.map(t=>t.id))};render()},
    notice(role,text,error=false){local.notices.push({role,text,error});local.notices=local.notices.slice(-5);render()},
  };
}
