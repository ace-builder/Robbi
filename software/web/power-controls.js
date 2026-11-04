import {api,notify,state} from './core.js';

export async function requestHostOperation(operation,button){
  if(button?.dataset.pending)return;
  const shutdown=operation==='shutdown';
  if(!['shutdown','restart'].includes(operation))return;
  if(!confirm(shutdown?'Shut down the Raspberry Pi? Robbi will go offline until you power it on again.':'Restart Robbi’s service and interrupt active work?'))return;
  const label=button?.textContent;
  if(button){button.dataset.pending='1';button.disabled=true;button.textContent=shutdown?'Requesting shutdown…':'Requesting restart…'}
  try{
    const result=await api(shutdown?'/shutdown':'/restart_service',{method:'POST',body:{},timeout:7000});
    state.hostOperation=operation;state.hostDisconnected=false;
    document.dispatchEvent(new Event('robbi:stop'));
    notify(result.message);
  }catch(error){notify(error.message,true)}
  finally{if(button){delete button.dataset.pending;button.disabled=false;button.textContent=label}}
}
