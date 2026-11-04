/* Include with <script src="/static/app-sdk.js"></script> in a custom app page.
 * No framework, parent access or direct network is required. POSTs are never retried.
 */
(()=>{
  let port,sequence=0;
  const pending=new Map();
  let resolveReady;const ready=new Promise(resolve=>resolveReady=resolve);
  window.addEventListener('message',event=>{
    if(event.source!==parent||event.data?.type!=='robbi-app:connect'||!event.ports[0]||port)return;
    port=event.ports[0];port.onmessage=event=>{
      const response=event.data,entry=pending.get(response?.id);if(!entry)return;
      clearTimeout(entry.timer);pending.delete(response.id);
      if(response.error)entry.reject(new Error(response.error));else entry.resolve(response.result);
    };port.start();resolveReady({id:event.data.app});
  });
  async function request(method,params={}){
    const connected=await Promise.race([ready,new Promise((_,reject)=>{const timer=setTimeout(()=>reject(new Error('Open this dashboard through Robbi’s app library.')),5000);ready.finally(()=>clearTimeout(timer))})]);
    if(!connected||!port)throw new Error('Dashboard is not connected.');
    if(pending.size>=4)throw new Error('Wait for the current dashboard requests to finish.');
    const id=String(++sequence);
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{pending.delete(id);reject(new Error('No confirmation received. Check the job status before trying again.'))},15000);
      pending.set(id,{resolve,reject,timer});port.postMessage({id,method,params});
    });
  }
  window.RobbiApp=Object.freeze({ready,info:()=>request('app'),saveConfig:config=>request('config',{config}),artifacts:()=>request('artifacts'),call:(tool,args={})=>request('tool',{tool,arguments:args}),job:id=>request('job',{id}),cancel:id=>request('cancel',{id})});
  parent.postMessage({type:'robbi-app:ready'},'*');
})();
