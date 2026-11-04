import {api} from '../core.js';
export async function mountRuntimeExtension(root,life){
  try {
    const info=await api('/api/runtime-extension',{signal:life.signal});
    if(life.signal.aborted||!info.module)return;
    if(!/^\/api\/[a-z0-9_-]+\/panel\.js$/.test(info.module))throw new Error('Invalid extension module');
    const extension=await import(info.module);
    if(!life.signal.aborted)extension.mount(root,life);
  } catch(error) {
    if(!life.signal.aborted)console.warn('Optional dashboard extension unavailable');
  }
}
