export const state = {health:null,status:null,connected:false,view:'overview',updated:0};
// Keep tool follow-ups in this browser tab, including navigation/reloads.
// Only an opaque identifier is stored; conversation contents remain in memory.
export const conversationId = (()=>{
  const fresh=()=>globalThis.crypto?.randomUUID?.()||Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('');
  try{const saved=sessionStorage.getItem('robbi-conversation-id');if(saved&&/^[A-Za-z0-9_-]{1,128}$/.test(saved))return saved;const value=fresh();sessionStorage.setItem('robbi-conversation-id',value);return value}catch{return fresh()}
})();
export const $ = (selector, root=document) => root.querySelector(selector);
export const $$ = (selector, root=document) => [...root.querySelectorAll(selector)];
export const esc = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const title = value => String(value).replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase());
export const pretty = value => JSON.stringify(value,null,2);
export const badge = value => `<span class="badge ${['ready','failed','degraded','disabled','starting','offline','running','queued','succeeded'].includes(value)?value:''}">${esc(value==='degraded'?'Needs attention':title(value || 'unknown'))}</span>`;
export const empty = text => `<div class="empty">${esc(text)}</div>`;
export const raw = (label,data) => `<details class="section-gap"><summary>${esc(label)}</summary><div class="details-body"><pre>${esc(pretty(data))}</pre></div></details>`;
export const heading = (overline,name,description,actions='') => `<div class="page-head"><div><div class="eyebrow">${esc(overline)}</div><h1>${esc(name)}</h1><p>${esc(description)}</p></div>${actions}</div>`;
export const formatTime = value => value ? new Date(typeof value==='number'?value*1000:value).toLocaleString(): '—';
export const fields = specs => specs.map(s=>`<label class="${s.full?'full':''}">${esc(s.label||title(s.name))}${s.type==='textarea'?`<textarea name="${esc(s.name)}" ${s.required?'required':''} placeholder="${esc(s.placeholder||'')}">${esc(s.value||'')}</textarea>`:s.options?`<select name="${esc(s.name)}">${s.options.map(o=>`<option value="${esc(Array.isArray(o)?o[0]:o)}">${esc(Array.isArray(o)?o[1]:title(o))}</option>`).join('')}</select>`:`<input name="${esc(s.name)}" type="${s.type||'text'}" value="${esc(s.value??'')}" ${s.required?'required':''} ${s.min!==undefined?`min="${s.min}"`:''} ${s.max!==undefined?`max="${s.max}"`:''} ${s.step?`step="${s.step}"`:''} placeholder="${esc(s.placeholder||'')}">`}${s.hint?`<small>${esc(s.hint)}</small>`:''}</label>`).join('');
export function data(form){const result=Object.fromEntries(new FormData(form));$$('input[type=number]',form).forEach(el=>{if(el.value!=='')result[el.name]=Number(el.value)});$$('input[type=checkbox][name]',form).forEach(el=>result[el.name]=el.checked);return result}
export async function api(path,{method='GET',body,timeout=12000,signal}={}){
  const abort=new AbortController();const timer=setTimeout(()=>abort.abort(),timeout);const cancel=()=>abort.abort();signal?.addEventListener('abort',cancel,{once:true});if(signal?.aborted)abort.abort();
  try{const response=await fetch(path,{method,credentials:'same-origin',cache:'no-store',headers:{'X-Robbi-Conversation':conversationId,...(body instanceof FormData?{}:body!==undefined?{'Content-Type':'application/json'}:{})},body:body===undefined?undefined:body instanceof FormData?body:JSON.stringify(body),signal:abort.signal});
    const content=await response.json().catch(()=>({error:`Unexpected server response (${response.status})`}));
    if(!response.ok||content.ok===false){const e=new Error(typeof content.error==='string'?content.error:content.message||content.text||content.speak||`Request failed (${response.status})`);e.status=response.status;throw e}if(['POST','PATCH','DELETE','PUT'].includes(method))document.dispatchEvent(new CustomEvent('robbi:tool-change'));return content;
  }catch(error){if(error.name==='AbortError')throw new Error(method==='GET'?'The request timed out. Check the connection and retry.':'No confirmation received. The action may have started; check its status before trying again.');if(error instanceof TypeError)throw new Error('Connection lost. Check Wi-Fi and the robot service. Commands will not be replayed.');throw error}
  finally{clearTimeout(timer);signal?.removeEventListener('abort',cancel)}
}
let toastTimer;
export function notify(message,error=false){const box=$('#notification');box.textContent=message;box.className=`notification${error?' error':''}`;box.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>box.hidden=true,error?12000:5500)}
export function issue(element,error){if(element)element.innerHTML=`<div class="notice error" role="alert">${esc(error.message||error)}</div>`}
export async function action(button,work,{output,success='Done.'}={}){if(button?.dataset.busy)return;const old=button?.textContent;if(button){button.dataset.busy='1';button.disabled=true;button.textContent='Working…'}try{const result=await work();if(output)output.textContent=responseText(result);if(success)notify(success);return result}catch(error){if(output)issue(output,error);notify(error.message,true);return null}finally{if(button){delete button.dataset.busy;button.disabled=false;button.textContent=old}gateControls()}}
export function responseText(result){const spoken=result?.action?.speak||result?.speak;if(spoken)return spoken+(result?.next?.speak?'\n\n'+result.next.speak:'');return result?.message||pretty(result)||'Request completed.'}
export function component(name){const components=state.health?.components;return (Array.isArray(components)?components:Object.values(components||{})).find(c=>c.name===name)}
export function ready(name){if(!state.connected||state.health?.state==='stopped')return false;const c=component(name);if(c?.state==='ready')return true;if(['display','audio'].includes(name)&&(!c||c.state==='degraded')){const output=state.status?.media?.[name];return output?.available===true&&output?.enabled!==false}if(c)return false;const d=state.status?.[name];return d?.available===true&&d?.enabled!==false}
export function gateControls(){for(const el of $$('[data-requires]')){const dependencies=el.dataset.requires.split(',');const unavailable=dependencies.filter(n=>!ready(n));el.disabled=!!el.dataset.busy||unavailable.length>0;el.title=unavailable.length?`Unavailable: ${unavailable.map(title).join(', ')}. See Overview for details.`:''}document.dispatchEvent(new CustomEvent('robbi:gates'))}
export function scope(){const controller=new AbortController();const disposers=[];return{on(el,event,fn){el?.addEventListener(event,fn,{signal:controller.signal})},add(fn){disposers.push(fn)},signal:controller.signal,dispose(){controller.abort();disposers.forEach(fn=>fn())}}}
export async function mountLoad(target,load,render,signal){try{const result=await load();if(!signal?.aborted)render(result)}catch(e){if(!signal?.aborted)issue(target,e)}}
export function bindForm(root,selector,handler,life){life.on($(selector,root),'submit',event=>{event.preventDefault();const form=event.currentTarget;action($('button[type=submit]',form),()=>handler(data(form),form),{output:$('.form-output',form),success:'Saved.'})})}
export function download(name,content,type='application/json'){const url=URL.createObjectURL(new Blob([content],{type}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
