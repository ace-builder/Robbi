import {api} from '../core.js';

// Opaque-origin iframe: only the transferred port can make app-scoped requests.
export function mountAppBridge(frame,appId,life,{onJob,onConfig}={}){
  const base=`/api/apps/${encodeURIComponent(appId)}`;
  let port,active=0,count=0,interval=Date.now();
  const jobs=new Set();
  function connect(event){
    if(event.source!==frame.contentWindow||event.data?.type!=='robbi-app:ready')return;
    port?.close();const channel=new MessageChannel();port=channel.port1;const connectedPort=port;
    port.onmessage=async message=>{
      const request=message.data;
      if(!request||typeof request.id!=='string'||request.id.length>80)return;
      const answer=(result,error)=>{try{connectedPort.postMessage({id:request.id,result,error})}catch{/* frame closed */}};
      if(Date.now()-interval>60000){count=0;interval=Date.now()}
      if(active>=4||++count>120){answer(null,'Dashboard request limit reached. Try again shortly.');return}
      let size;try{size=JSON.stringify(request).length}catch{return}
      if(size>65536){answer(null,'Dashboard request is too large.');return}
      active++;
      try{
        let result;
        const params=request.params||{};
        switch(request.method){
          case 'app':result=await api(base,{signal:life.signal});break;
          case 'config':result=await api(`${base}/config`,{method:'PATCH',body:{config:params.config},signal:life.signal});onConfig?.(result.config||params.config);break;
          case 'artifacts':result=await api(`${base}/artifacts`,{signal:life.signal});break;
          case 'tool':
            if(typeof params.tool!=='string'||!params.tool||params.tool.length>100)throw new Error('Invalid tool name.');
            result=await api(`${base}/tools/${encodeURIComponent(params.tool)}`,{method:'POST',body:{arguments:params.arguments||{}},signal:life.signal});
            if(result.job?.id){jobs.add(result.job.id);if(jobs.size>64)jobs.delete(jobs.values().next().value);onJob?.(result.job)}
            break;
          case 'job':case 'cancel':
            if(!jobs.has(params.id))throw new Error('This dashboard can only access jobs it started.');
            result=await api(`/api/app-jobs/${encodeURIComponent(params.id)}${request.method==='cancel'?'/cancel':''}`,{method:request.method==='cancel'?'POST':'GET',...(request.method==='cancel'?{body:{}}:{}),signal:life.signal});break;
          default:throw new Error('Unsupported dashboard operation.');
        }
        if(!life.signal.aborted)answer(result);
      }catch(error){if(!life.signal.aborted)answer(null,error.message)}finally{active--}
    };
    port.start();frame.contentWindow.postMessage({type:'robbi-app:connect',app:appId},'*',[channel.port2]);
  }
  life.on(window,'message',connect);life.add(()=>port?.close());
}
