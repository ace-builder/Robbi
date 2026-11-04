import test from 'node:test';
import assert from 'node:assert/strict';
import {MediaPreview} from './media-preview.js';
import {compile,waypoint,FORMAT,DEFAULT_SETTINGS} from './motion.js';

function harness(t, cue={speech:'Hello Robbi.'}){
  let now=0,cancels=0;
  const spoken=[],messages=[];
  t.mock.method(performance,'now',()=>now);
  globalThis.window={speechSynthesis:{speak:u=>spoken.push(u),cancel:()=>cancels++,pause(){},resume(){},getVoices:()=>[]}};
  globalThis.SpeechSynthesisUtterance=class{constructor(text){this.text=text;}};
  globalThis.Image=class{removeAttribute(){}};
  globalThis.Audio=class{
    constructor(){this.readyState=4;this.duration=4;this.currentTime=0;this.paused=true;this.ended=false;}
    play(){this.paused=false;return Promise.resolve();} pause(){this.paused=true;} load(){} removeAttribute(){}
  };
  const point=waypoint(0,0,0,{cue});
  const plan=compile({format:FORMAT,name:'Preview test',settings:DEFAULT_SETTINGS,waypoints:[point]});
  const preview=new MediaPreview(()=>({setScreen(){}}),m=>messages.push(m));
  return {preview,plan,point,spoken,messages,tick:ms=>now=ms,cancels:()=>cancels};
}

test('cue audition waits for speech to finish instead of cutting it off at the estimate',t=>{
  const h=harness(t);h.preview.audition(h.point);h.preview.update(h.plan,0,false);
  assert.equal(h.spoken.length,1);
  h.tick(1400);h.preview.update(h.plan,0,false);
  assert.ok(h.preview.auditionState);assert.equal(h.cancels(),0);
  h.spoken[0].onend();h.tick(1500);h.preview.update(h.plan,0,false);
  assert.equal(h.preview.auditionState,null);
});

test('waiting cues hold the timeline until audio and then speech really finish',t=>{
  const h=harness(t,{sound:'media-sound',speech:'Hi',duration:.1,wait:true});
  h.preview.update(h.plan,0,true);
  const boundary=h.preview.limitTime(h.plan,0,h.plan.duration+.1);
  assert.ok(boundary<h.plan.duration);assert.equal(h.spoken.length,0);
  h.preview.update(h.plan,boundary,true);
  assert.equal(h.preview.audio.paused,false);
  h.preview.audio.ended=true;h.preview.update(h.plan,boundary,true);
  assert.equal(h.spoken.length,1);
  assert.ok(h.preview.limitTime(h.plan,boundary,h.plan.duration+.1)<h.plan.duration);
  h.spoken[0].onend();
  assert.ok(h.preview.limitTime(h.plan,boundary,h.plan.duration+.1)>h.plan.duration);
});

test('background speech does not hold motion, but final speech finishes',t=>{
  const h=harness(t,{speech:'Hello Robbi.',wait:false});
  const moving=compile({...h.plan.project,waypoints:[h.plan.project.waypoints[0],waypoint(30,0,0)]});
  h.preview.update(moving,0,true);
  assert.equal(h.preview.limitTime(moving,0,1),1);
  h.preview.reset();h.preview.update(h.plan,0,true);
  assert.ok(h.preview.limitTime(h.plan,0,h.plan.duration+.1)<h.plan.duration);
});

test('scrubbing stays silent and rewinding permits speech to replay',t=>{
  const h=harness(t);h.preview.update(h.plan,.5,false);h.preview.update(h.plan,.51,true);
  assert.equal(h.spoken.length,0);
  h.preview.reset();h.preview.update(h.plan,0,true);assert.equal(h.spoken.length,1);
  h.preview.reset();h.preview.update(h.plan,0,true);assert.equal(h.spoken.length,2);
  h.spoken[0].onend();assert.equal(h.preview.speechPending,true);
});

test('failed speech releases the timeline and reports its reason once',t=>{
  const h=harness(t);h.preview.update(h.plan,0,true);
  h.spoken[0].onerror({error:'voice-unavailable'});
  assert.equal(h.messages.length,1);
  assert.ok(h.preview.limitTime(h.plan,0,h.plan.duration+.1)>h.plan.duration);
});

test('resuming after scrubbing past a sound does not restart it',t=>{
  const h=harness(t,{sound:'media-sound',wait:false});
  const moving=compile({...h.plan.project,waypoints:[h.plan.project.waypoints[0],waypoint(30,0,0,{speed:2})]});
  h.preview.update(moving,5,false);h.preview.update(moving,5.1,true);
  assert.equal(h.preview.audio.paused,true);
});

test('a cancelled audio load cannot report an error against the following cue',async t=>{
  const h=harness(t,{sound:'media-sound'});let reject;
  h.preview.update(h.plan,0,false);
  h.preview.audio.play=()=>new Promise((_,fail)=>reject=fail);
  h.preview.update(h.plan,0,true);h.preview.reset();
  const next=compile({...h.plan.project,waypoints:[waypoint()]});h.preview.update(next,0,false);
  reject(new Error('The previous audio was replaced'));await Promise.resolve();
  assert.deepEqual(h.messages,[]);assert.equal(h.preview.failed,false);
});
