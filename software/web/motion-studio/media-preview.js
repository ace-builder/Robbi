import {compile,FORMAT,DEFAULT_SETTINGS,validateCue,waypoint} from './motion.js';

export const mediaUrl = id => '/api/motion-studio/media/'+encodeURIComponent(id)+'/file';
export const hasMedia = point => point.cue.screen!=='face'||!!point.cue.sound||!!point.cue.speech;
export function cueLabel(point){return [point.cue.screen==='face'?point.face:point.cue.screen,point.cue.sound?'sound':'',point.cue.speech?'speech':''].filter(Boolean).join(' · ');}

export class MediaPreview {
  constructor(stage,notify,caption=()=>{}){this.stage=stage;this.notify=notify;this.caption=caption;this.catalog=new Map();this.key='';this.lastTime=0;this.wasPlaying=false;this.speechTriggered=false;this.auditionState=null;this.lastUpdate=performance.now();this.playedSeconds=0;}
  setCatalog(entries){this.catalog=new Map(entries.map(e=>[e.id,e]));}
  reset(){
    for(const media of [this.audio,this.video])if(media){media.pause();media.removeAttribute('src');media.load();}
    this.image?.removeAttribute('src');
    if(this.utterance&&window.speechSynthesis)window.speechSynthesis.cancel();
    this.utterance=null;this.audio=null;this.video=null;this.image=null;this.speechTriggered=false;this.speechPending=false;this.key='';this.failed=false;this.errorKey='';this.playedSeconds=0;this.audioSeek=null;this.audioDone=false;
    this.stage()?.setScreen(null);
    this.caption('');
  }
  stopAudition(){this.auditionState=null;this.reset();}
  audition(point){
    this.stopAudition();const p={...waypoint(),...point,cue:validateCue({...point.cue,wait:true})};
    const plan=compile({format:FORMAT,name:'Cue preview',settings:{...DEFAULT_SETTINGS},waypoints:[p]});
    this.auditionState={plan,start:performance.now(),duration:Math.max(1,plan.duration)};
  }
  _error(message){this.failed=true;if(this.errorKey!==this.key){this.errorKey=this.key;this.notify(message,true);}}
  pending(cue){
    if(this.failed)return false;
    if(this.playedSeconds>=110){this._error('Media preview timed out. Check the file or browser voice.');return false;}
    return !!((this.audio&&!this.audioDone&&!this.audio.ended&&!this.audio.error)||(cue.speech&&(!this.speechTriggered||this.speechPending)));
  }
  limitTime(plan,time,nextTime){
    const cue=plan.cues.findLast(c=>c.start<=time+1e-7);
    if(!cue||(!cue.wait&&cue.index!==plan.cues.at(-1).index)||!this.pending(cue))return nextTime;
    const hold=plan.segments.find(s=>s.kind==='hold'&&s.index===cue.index);
    const boundary=hold?hold.start+hold.duration:cue.start;
    return nextTime>=boundary?Math.max(time,boundary-1e-5):nextTime;
  }
  update(plan,time,playing,rate=1){
    if(this.auditionState){const a=this.auditionState;time=(performance.now()-a.start)/1000;plan=a.plan;playing=true;rate=1;if(time>=a.duration&&!this.pending(plan.cues[0])){this.stopAudition();return;}}
    const cue=plan.cues.findLast(c=>c.start<=time+1e-7)||plan.cues[0];if(!cue)return;
    const elapsed=Math.max(0,time-cue.start),key=JSON.stringify([cue.index,cue.start,cue.screen,cue.media,cue.sound,cue.speech,cue.face,cue.mediaTime,plan.project.name]);
    const jump=!this.auditionState&&(time<this.lastTime-.05||Math.abs(time-this.lastTime)>.5);
    if(key!==this.key||jump){
      this.reset();this.key=key;
      if(cue.screen==='image'||cue.screen==='face'){
        const image=new Image();this.image=image;image.onload=()=>{if(this.image===image&&image.naturalWidth)this.stage()?.setScreen(image);};image.onerror=()=>{if(this.image===image)this._error('The screen image is missing. Import its media bundle.');};image.src=cue.screen==='face'?'/static/motion-studio/faces/'+cue.face+'.png':mediaUrl(cue.media);
      }else if(cue.screen==='video'){
        const video=document.createElement('video');this.video=video;video.muted=true;video.playsInline=true;video.preload='auto';video.src=mediaUrl(cue.media);video.onerror=()=>{if(this.video===video)this._error('This video cannot be decoded by the browser. Use an H.264 MP4.');};
      }
      if(cue.sound){const audio=new Audio(mediaUrl(cue.sound));this.audio=audio;this.audioSeek=elapsed;audio.preload='auto';audio.onerror=()=>{if(this.audio===audio)this._error('The sound could not be loaded. Import its media bundle.');};}
      this.speechTriggered=!playing&&elapsed>.25;
    }
    const now=performance.now();if(playing&&this.wasPlaying)this.playedSeconds+=Math.min(1,(now-this.lastUpdate)/1000);this.lastUpdate=now;
    if(this.image?.complete&&this.image.naturalWidth)this.stage()?.setScreen(this.image);
    if(this.video){
      const limit=cue.mediaTime||this.video.duration||110,target=Math.min(elapsed,limit,Number.isFinite(this.video.duration)?Math.max(0,this.video.duration-.02):110);
      if(this.video.readyState>=1&&Math.abs(this.video.currentTime-target)>.25)this.video.currentTime=target;
      this.video.playbackRate=rate;
      if(playing&&elapsed<limit&&this.video.paused&&!this.video.ended)this.video.play().catch(()=>{});else if(!playing||elapsed>=limit)this.video.pause();
      if(this.video.readyState>=2)this.stage()?.setScreen(this.video);
    }
    if(this.audio?.readyState>=1&&this.audioSeek!==null){
      this.audioDone=Number.isFinite(this.audio.duration)&&this.audioSeek>=this.audio.duration;
      this.audio.currentTime=Math.min(this.audioSeek,Number.isFinite(this.audio.duration)?this.audio.duration:this.audioSeek);this.audioSeek=null;
    }
    const soundFinished=!this.audio||this.audioDone||this.audio.ended;
    this.caption(soundFinished?cue.speech:'');
    if(this.audio){
      this.audio.playbackRate=rate;
      if(playing&&!soundFinished&&this.audio.paused&&!this.failed){const audio=this.audio;audio.play().catch(error=>{if(this.audio===audio&&error.name!=='AbortError')this._error('Click Preview cue to enable sound in this browser.');});}else if(!playing)this.audio.pause();
    }
    if(cue.speech&&playing&&soundFinished&&!this.speechTriggered&&!this.failed){
      this.speechTriggered=true;
      if(!('speechSynthesis' in window))this._error('Speech preview is unavailable in this browser. Robbi can still use its configured voice.');
      else{
        const utterance=new SpeechSynthesisUtterance(cue.speech);this.utterance=utterance;this.speechPending=true;utterance.rate=Math.max(.5,Math.min(2,rate));
        utterance.onend=()=>{if(this.utterance===utterance)this.speechPending=false;};
        utterance.onerror=event=>{if(this.utterance!==utterance)return;this.speechPending=false;if(!['canceled','interrupted'].includes(event.error))this._error('Browser speech is unavailable. Check the installed system voices; Robbi uses its configured voice.');};
        const voices=window.speechSynthesis.getVoices(),local=voices.find(v=>v.localService&&v.lang.toLowerCase().startsWith(navigator.language.split('-')[0]));if(local)this.utterance.voice=local;
        window.speechSynthesis.speak(this.utterance);
      }
    }
    if(this.utterance&&window.speechSynthesis){if(!playing&&this.wasPlaying)window.speechSynthesis.pause();if(playing&&!this.wasPlaying)window.speechSynthesis.resume();}
    this.wasPlaying=playing;this.lastTime=time;
  }
  dispose(){this.stopAudition();}
}
