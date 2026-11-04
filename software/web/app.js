import {requestHostOperation} from './power-controls.js';
import {audioControls} from './audio-controls.js';
import {liveIndicator} from './views/live-awareness.js';
liveIndicator();
import {wake} from './views/wake.js';
import {$,$$,state,api,notify,badge,gateControls,scope} from './core.js';
import {overview,interact} from './views/home.js';
import {daily,memory} from './views/daily.js';
import {media} from './views/hardware.js';
import {motion} from './views/motion.js';
import {settings} from './views/settings.js';
import {apps} from './views/apps.js';
import {appHub} from './views/app-hub.js';
import {audio} from './views/audio.js';

// Load the 3D viewer only when requested; other tabs never download Three.js.
const imu=async(root,life)=>{const view=await import('./views/imu.js');if(!life.signal.aborted)return view.imu(root,life)};
const map=async(root,life)=>{const view=await import('./views/navigation.js');if(!life.signal.aborted)return view.navigation(root,life)};
const video=async(root,life)=>{const view=await import('./views/video.js');if(!life.signal.aborted)return view.video(root,life)};
const conversationLab=async(root,life)=>{const view=await import('./views/conversation-experiment.js');if(!life.signal.aborted)return view.conversationExperiment(root,life)};
const routes={'conversation-lab':conversationLab,video,wake,map,overview,interact,apps,'app-hub':appHub,'app-store':appHub,daily,memory,motion,imu,media,audio,settings};
const aliases={'/app':'interact','/onboard':'settings','/live':'interact','/mic':'media','/wake':'wake','/settings':'settings','/memory':'memory','/today':'daily','/map':'map','/imu':'imu'};
let life,epoch=0;
async function navigate(){const key=(location.hash.slice(1)||aliases[location.pathname]||'overview').split('/')[0];state.view=routes[key]?key:'overview';life?.dispose();life=scope();const current=++epoch;for(const a of $$('nav a'))a.setAttribute('aria-current',a.dataset.view===state.view?'page':'false');const selected=$('nav a[aria-current=page]'),nav=$('nav');if(nav.scrollWidth>nav.clientWidth&&selected){const left=selected.offsetLeft-nav.offsetLeft;nav.scrollLeft=Math.max(0,left-(nav.clientWidth-selected.offsetWidth)/2)}const root=$('#content');root.innerHTML='';window.scrollTo(0,0);try{await routes[state.view](root,life);if(current===epoch)gateControls()}catch(e){if(current===epoch){root.textContent=`Unable to open this view: ${e.message}`;notify(e.message,true)}}document.title=`Robbi · ${$('nav a[aria-current=page]')?.textContent.trim().replace(/^\d+\s*/,'')||'Overview'}`}
window.addEventListener('hashchange',navigate);
$('#stop').addEventListener('click',async()=>{const button=$('#stop');if(button.dataset.pending)return;button.dataset.pending='1';button.innerHTML='■ <span>Stopping…</span>';document.dispatchEvent(new Event('robbi:stop'));try{const result=await api('/api/stop',{method:'POST',body:{},timeout:4000});notify(result.message||'Stop requested. Check the motion status for confirmation.')}catch(e){notify(`Stop could not be confirmed. ${e.message} Use the physical motor cutoff if needed.`,true)}finally{delete button.dataset.pending;button.innerHTML='■ <span>Stop actions</span>'}});

$('#power-off').addEventListener('click',()=>requestHostOperation('shutdown',$('#power-off')));

let polling=false,timer=null,failures=0;
async function poll(){clearTimeout(timer);if(polling||document.hidden)return;polling=true;try{const health=await api('/api/health',{timeout:5000});state.health=health;state.connected=true;if(state.hostDisconnected){state.hostOperation=null;state.hostDisconnected=false}failures=0;state.updated=Date.now();$('#connection-label').textContent='Service connected';$('#connection-dot').className='status-dot ready';$('#last-updated').textContent=`Updated ${new Date(state.updated).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`;$('#system-state').outerHTML=badge(health.state).replace('<span ','<span id="system-state" ');$('#connection-notice').hidden=true;
    try{state.status=await api('/status',{timeout:5000})}catch(e){state.status=null;if(e.status!==503)console.debug('Status unavailable:',e.message)}
  }catch(e){state.connected=false;state.status=null;failures++;$('#connection-label').textContent='Connection unavailable';$('#connection-dot').className='status-dot failed';$('#system-state').outerHTML=badge('offline').replace('<span ','<span id="system-state" ');const notice=$('#connection-notice');notice.hidden=false;if(state.hostOperation)state.hostDisconnected=true;notice.textContent=state.hostOperation==='shutdown'?'Shutdown was requested and the connection closed. Wait for the Pi to halt before disconnecting power. The dashboard reconnects when Robbi starts again.':`Robbi is unreachable. Check power, Wi-Fi, and the robot service. ${state.updated?'Last connected '+new Date(state.updated).toLocaleTimeString()+'. ':''}Reconnecting automatically; commands are never replayed.`}
  finally{polling=false;gateControls();document.dispatchEvent(new CustomEvent('robbi:status'));if(!document.hidden)timer=setTimeout(poll,Math.min(30000,5000*2**Math.min(failures,3)))}
}
document.addEventListener('visibilitychange',()=>{clearTimeout(timer);document.dispatchEvent(new CustomEvent('robbi:visibility'));if(!document.hidden)poll()});
window.addEventListener('online',poll);
window.addEventListener('pagehide',()=>{clearTimeout(timer);life?.dispose()});
audioControls();navigate();poll();
