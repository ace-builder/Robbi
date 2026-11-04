import {calibrate} from './wake-calibration.js';
self.onmessage=event=>{
  const started=performance.now();
  try{const result=calibrate(event.data,(done,total)=>self.postMessage({type:'progress',done,total}));self.postMessage({type:'done',result:{...result,browser_seconds:(performance.now()-started)/1000}})}
  catch(error){self.postMessage({type:'error',error:error.message})}
};
