import test from 'node:test';
import assert from 'node:assert/strict';
import {compile,example,waypoint,FORMAT,DEFAULT_SETTINGS,sampleAt,rad,wrap,validateProject,validateCue,CUE_DEFAULTS} from './motion.js';

const clip=points=>({format:FORMAT,name:'Test clip',settings:{...DEFAULT_SETTINGS},waypoints:points});
const near=(a,b,tolerance=1e-6)=>assert.ok(Math.abs(a-b)<tolerance,`${a} != ${b}`);
test('templates produce finite, achievable plans',()=>{
  for(const name of ['hello','wiggle','reverse','square']){
    const plan=compile(example(name));assert.deepEqual(plan.warnings,[]);assert.ok(plan.duration>0&&plan.duration<110);
    for(const segment of plan.segments)for(const p of segment.samples)for(const key of ['x','y','heading','left','right','v','t'])assert.ok(Number.isFinite(p[key]));
    const end=sampleAt(plan,plan.duration),last=plan.project.waypoints.at(-1);near(end.x,last.x);near(end.y,last.y);near(wrap(end.heading-rad(last.heading)),0);assert.equal(end.face,last.face);
  }
});
test('reverse travel preserves body heading and negative wheel displacement',()=>{
  const plan=compile(clip([waypoint(0,0,0),waypoint(-30,0,0,{direction:'reverse'})]));
  const sample=sampleAt(plan,plan.duration/2);assert.ok(sample.x<0);near(sample.heading,0);assert.ok(sample.left<0&&sample.right<0);assert.equal(sample.direction,-1);near(plan.distance,30);
});
test('wheel-distance integration follows the authored curve, rather than a straight chord',()=>{
  const plan=compile(clip([waypoint(0,0,0),waypoint(30,30,90)]));let x=0,y=0,h=0;
  const samples=plan.segments[0].samples;
  for(let i=1;i<samples.length;i++){
    const dl=samples[i].left-samples[i-1].left,dr=samples[i].right-samples[i-1].right,angle=(dr-dl)/plan.project.settings.wheelBase,distance=(dl+dr)/2;
    if(Math.abs(angle)<1e-9){x+=distance*Math.cos(h);y+=distance*Math.sin(h);}else{x+=distance/angle*(Math.sin(h+angle)-Math.sin(h));y-=distance/angle*(Math.cos(h+angle)-Math.cos(h));}h+=angle;
  }
  near(x,30,.01);near(y,30,.01);near(h,Math.PI/2);assert.ok(plan.distance>Math.hypot(30,30));
});
test('turn in place takes the shortest heading transition across ±180°',()=>{
  const plan=compile(clip([waypoint(0,0,170),waypoint(0,0,-170)]));const s=plan.segments[0];assert.equal(s.kind,'turn');near(s.samples.at(-1).heading-s.samples[0].heading,rad(20));near(plan.distance,0);near(plan.left,-rad(20)*DEFAULT_SETTINGS.wheelBase/2);
});
test('profiles obey wheel speed, acceleration and zero-speed endpoints',()=>{
  const plan=compile(example());const g=plan.project.settings;
  for(const segment of plan.segments.filter(s=>s.kind==='move')){
    near(segment.samples[0].v,0);near(segment.samples.at(-1).v,0);
    for(let i=1;i<segment.samples.length;i++){
      const a=segment.samples[i-1],b=segment.samples[i];assert.ok(b.v*(1+Math.abs(b.curvature)*g.wheelBase/2)<=g.maxWheelSpeed+1e-7);
      assert.ok(Math.abs(b.v*b.v-a.v*a.v)<=2*g.acceleration*(b.s-a.s)+1e-6);assert.ok(b.t>a.t);
    }
  }
});
test('tight curves, swept stage edges and excessive duration block robot playback',()=>{
  assert.ok(compile(clip([waypoint(0,0,0),waypoint(2,2,180)])).warnings.length);
  const edge=clip([waypoint(0,0,0),waypoint(65,0,0)]);assert.match(compile(edge).warnings[0].message,/stage edge/);
  const long=clip(Array.from({length:5},()=>waypoint(0,0,0,{hold:30})));assert.match(compile(long).warnings[0].message,/110 seconds/);
  const wide=clip([waypoint(55,0,0)]);wide.settings.wheelBase=30;assert.match(compile(wide).warnings[0].message,/stage edge/);
});
test('invalid files are rejected and unknown action fields cannot enter the format',()=>{
  for(const mutate of [p=>p.format='unknown',p=>p.settings=null,p=>p.waypoints[0].x=NaN,p=>p.waypoints[0].hold=-1,p=>p.waypoints[0].heading=181,p=>p.waypoints[0].face='../unsafe']){
    const p=example();mutate(p);assert.throws(()=>validateProject(p));
  }
  const p=example();p.drive={ignore_obstacles:true};p.waypoints[0].ignore_obstacles=true;const clean=validateProject(p);assert.equal(clean.drive,undefined);assert.equal(clean.waypoints[0].ignore_obstacles,undefined);
});
test('empty motion still has a finite, editable starting pose',()=>{
  const plan=compile(clip([waypoint(12,15,-45)]));assert.equal(plan.duration,0);const sample=sampleAt(plan,0);near(sample.x,12);near(sample.heading,rad(-45));
});

test('v1 clips upgrade without losing motion or faces',()=>{
  const old=example();old.format='robbi.motion/v1';old.waypoints.forEach(p=>delete p.cue);
  const upgraded=validateProject(old);assert.equal(upgraded.format,FORMAT);assert.equal(upgraded.waypoints[2].face,'wink');assert.deepEqual(upgraded.waypoints[0].cue,CUE_DEFAULTS);
});
test('media cues are anchored at arrivals and can wait or accompany movement',()=>{
  const source=clip([waypoint(0,0,0,{cue:{...CUE_DEFAULTS,speech:'Hello, friend!',duration:2,wait:true}}),waypoint(30,0,0)]);
  const waiting=compile(source);assert.equal(waiting.cues[0].start,0);assert.ok(waiting.segments[0].duration>=2);assert.equal(waiting.beats[0].kind,'cue');
  source.waypoints[0].cue.wait=false;const moving=compile(source);assert.equal(moving.segments[0].kind,'move');assert.equal(moving.segments[0].start,0);assert.ok(waiting.duration>moving.duration);
  source.waypoints[1].cue={...CUE_DEFAULTS,speech:'Bye!',duration:2,wait:false};const final=compile(source);assert.equal(final.segments.at(-1).kind,'hold');near(final.duration-final.arrivals[1],2);
});
test('screen and speech files retain every cue through a JSON round trip',()=>{
  const p=clip([waypoint(0,0,0,{cue:{screen:'video',media:'media-demo',sound:'media-sound',speech:'A little movie.',duration:4,wait:false}})]);
  const roundtrip=validateProject(JSON.parse(JSON.stringify(p)));assert.deepEqual(roundtrip.waypoints[0].cue,p.waypoints[0].cue);assert.equal(compile(roundtrip).cues[0].mediaTime,4);
});
test('media validation rejects paths, missing screens and malformed speech',()=>{
  for(const cue of [null,[],{screen:'video'},{screen:'image',media:'../file'},{sound:'https://example.com/a.wav'},{sound:'media-file\n'},{speech:42},{speech:'a'.repeat(1001)},{wait:'yes'},{duration:Infinity}])assert.throws(()=>validateCue(cue));
});

test('browser requires explicit coordinates and counts Unicode names like the robot',()=>{
  for(const key of ['x','y','heading']){const p=example();delete p.waypoints[0][key];assert.throws(()=>validateProject(p));}
  const p=example();p.name='🤖'.repeat(80);assert.equal(validateProject(p).name,p.name);
  p.name+='🤖';assert.throws(()=>validateProject(p));
});
