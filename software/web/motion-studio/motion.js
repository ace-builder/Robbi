// Coordinates are centimetres: +x is forward at 0°, positive heading turns left.
export const FORMAT = 'robbi.motion/v2';
export const CUE_DEFAULTS = {screen:'face',media:'',sound:'',speech:'',duration:0,wait:true};
export function validateCue(value={}) {
  if(!value||typeof value!=='object'||Array.isArray(value))throw Error('Media cue must be an object.');
  const cue={...CUE_DEFAULTS,...value};
  if(!['face','image','video'].includes(cue.screen))throw Error('Choose a face, image, or video for the screen.');
  for(const key of ['media','sound'])if(typeof cue[key]!=='string'||cue[key].length>80||/[^a-z0-9_-]/.test(cue[key]))throw Error('Select media from the library.');
  if(cue.screen!=='face'&&!cue.media)throw Error('Choose an image or video for the screen.');
  if(typeof cue.speech!=='string'||[...cue.speech].length>1000)throw Error('Speech is limited to 1,000 characters.');
  if(typeof cue.wait!=='boolean')throw Error('Wait for media must be true or false.');
  finite(cue.duration,'Cue duration',0,110);
  return Object.fromEntries(Object.keys(CUE_DEFAULTS).map(k=>[k,k==='speech'?cue[k].trim():cue[k]]));
}
export const cueDuration = cue => Math.max(cue.duration,(cue.sound?cue.duration||1:0)+[...cue.speech].length/14,cue.screen==='video'?1:0);
export const FACES = ['happy','neutral','cute','wink','surprised','thinking','sleepy','joyful','mischievous','annoyed','aware','grumpy'];
export const DEFAULT_SETTINGS = {wheelBase:10.2,wheelDiameter:6.2,maxWheelSpeed:22,acceleration:18,angularSpeed:90,minRadius:8,motorPower:100,gridSize:5,stageSize:140};
export const rad = degrees => degrees*Math.PI/180;
export const deg = radians => radians*180/Math.PI;
export const wrap = angle => ((angle+Math.PI)%(2*Math.PI)+2*Math.PI)%(2*Math.PI)-Math.PI;
export const footprintRadius = g => Math.max(9,Math.hypot((g.wheelBase+.9)/2,Math.max(6.4,g.wheelDiameter/2)));
const lerp = (a,b,t) => a+(b-a)*t;
const finite = (value,label,min,max) => {if(typeof value!=='number'||!Number.isFinite(value)||value<min||value>max)throw Error(`${label} must be between ${min} and ${max}.`);return value;};

export function waypoint(x=0,y=0,heading=0,extra={}) {
  return {x,y,heading,direction:'forward',path:'curve',speed:12,hold:0,tension:.38,face:'happy',cue:{...CUE_DEFAULTS},...extra};
}
export function example(kind='hello') {
  const base={format:FORMAT,name:'Little hello',settings:{...DEFAULT_SETTINGS},waypoints:[]};
  if(kind==='hello')base.waypoints=[waypoint(-30,-22,0,{hold:.4}),waypoint(8,-10,35,{speed:10,hold:.3}),waypoint(30,25,90,{speed:9,hold:.7,face:'wink'}),waypoint(30,3,90,{direction:'reverse',speed:7,hold:.4}),waypoint(30,3,0,{hold:.5,face:'happy'})];
  if(kind==='wiggle'){base.name='Happy wiggle';base.waypoints=[waypoint(0,0,0,{hold:.2}),...[-15,15,-15,15,0].map(h=>waypoint(0,0,h,{hold:.12,face:'joyful'}))];}
  if(kind==='reverse'){base.name='A little shy';base.waypoints=[waypoint(-20,0,0,{hold:.4,face:'cute'}),waypoint(15,0,0,{speed:9,hold:.5}),waypoint(-20,0,0,{direction:'reverse',speed:6,hold:.8,face:'surprised'})];}
  if(kind==='square'){base.name='Around the block';base.waypoints=[waypoint(-25,-25,0),waypoint(25,-25,90,{path:'line'}),waypoint(25,25,180,{path:'line'}),waypoint(-25,25,-90,{path:'line'}),waypoint(-25,-25,0,{path:'line',hold:.5})];}
  return base;
}

export function validateProject(raw) {
  if(!raw||typeof raw!=='object'||Array.isArray(raw)||![FORMAT,'robbi.motion/v1'].includes(raw.format))throw Error('Choose a Robbi Motion Studio .robbi.json file (v1 or v2).');
  if(typeof raw.name!=='string'||!raw.name.trim()||[...raw.name].length>80)throw Error('Animation name must be 1–80 characters.');
  if(!Array.isArray(raw.waypoints)||raw.waypoints.length<1||raw.waypoints.length>64)throw Error('An animation needs 1–64 waypoints.');
  if(raw.settings!==undefined&&(!raw.settings||typeof raw.settings!=='object'||Array.isArray(raw.settings)))throw Error('Settings must be an object.');
  const settings={...DEFAULT_SETTINGS,...raw.settings};
  for(const [key,min,max] of [['wheelBase',4,30],['wheelDiameter',2,15],['maxWheelSpeed',2,60],['acceleration',1,100],['angularSpeed',10,180],['minRadius',3,100],['motorPower',10,100],['gridSize',1,20],['stageSize',40,600]])finite(settings[key],key,min,max);
  const points=raw.waypoints.map((p,i)=>{
    if(!p||typeof p!=='object'||Array.isArray(p))throw Error(`Waypoint ${i+1} must be an object.`);
    for(const key of ['x','y','heading'])if(p[key]===undefined)throw Error(`Waypoint ${i+1} needs ${key}.`);
    const w=waypoint(p.x,p.y,p.heading,p);
    for(const [key,min,max] of [['x',-250,250],['y',-250,250],['heading',-180,180],['speed',2,40],['hold',0,30],['tension',.1,.65]])finite(w[key],`Waypoint ${i+1}: ${key}`,min,max);
    if(!['forward','reverse'].includes(w.direction)||!['curve','line'].includes(w.path)||!FACES.includes(w.face))throw Error(`Waypoint ${i+1} has an unknown motion or face.`);
    return {...Object.fromEntries(['x','y','heading','direction','path','speed','hold','tension','face'].map(k=>[k,w[k]])),cue:validateCue(w.cue)};
  });
  return {format:FORMAT,name:raw.name.trim(),settings:Object.fromEntries(Object.keys(DEFAULT_SETTINGS).map(k=>[k,settings[k]])),waypoints:points};
}

export function cubicSamples(a,b) {
  const direction=b.direction==='reverse'?-1:1;
  const distance=Math.hypot(b.x-a.x,b.y-a.y), handle=distance*b.tension;
  const h0=rad(a.heading),h1=rad(b.heading);
  const c1={x:a.x+Math.cos(h0)*handle*direction,y:a.y+Math.sin(h0)*handle*direction};
  const c2={x:b.x-Math.cos(h1)*handle*direction,y:b.y-Math.sin(h1)*handle*direction};
  const count=Math.max(80,Math.ceil(distance*3));
  let s=0,previousHeading=h0,previous=null;
  return Array.from({length:count+1},(_,i)=>{
    const t=i/count,u=1-t;
    const x=u*u*u*a.x+3*u*u*t*c1.x+3*u*t*t*c2.x+t*t*t*b.x;
    const y=u*u*u*a.y+3*u*u*t*c1.y+3*u*t*t*c2.y+t*t*t*b.y;
    const dx=3*u*u*(c1.x-a.x)+6*u*t*(c2.x-c1.x)+3*t*t*(b.x-c2.x);
    const dy=3*u*u*(c1.y-a.y)+6*u*t*(c2.y-c1.y)+3*t*t*(b.y-c2.y);
    const ddx=6*u*(c2.x-2*c1.x+a.x)+6*t*(b.x-2*c2.x+c1.x);
    const ddy=6*u*(c2.y-2*c1.y+a.y)+6*t*(b.y-2*c2.y+c1.y);
    const length=Math.hypot(dx,dy);
    const heading=previousHeading+wrap(Math.atan2(dy,dx)+(direction<0?Math.PI:0)-previousHeading);
    if(previous)s+=Math.hypot(x-previous.x,y-previous.y);
    const p={x,y,s,heading,curvature:length<1e-7?Infinity:(dx*ddy-dy*ddx)/length**3,cusp:length<1e-5};
    previous=p;previousHeading=heading;return p;
  });
}

function timed(samples,limits,acceleration) {
  const velocities=samples.map((_,i)=>limits[i]);
  velocities[0]=0;velocities[velocities.length-1]=0;
  for(let i=1;i<samples.length;i++)velocities[i]=Math.min(velocities[i],Math.sqrt(velocities[i-1]**2+2*acceleration*(samples[i].s-samples[i-1].s)));
  for(let i=samples.length-2;i>=0;i--)velocities[i]=Math.min(velocities[i],Math.sqrt(velocities[i+1]**2+2*acceleration*(samples[i+1].s-samples[i].s)));
  let time=0;
  return samples.map((p,i)=>{if(i)time+=2*(p.s-samples[i-1].s)/Math.max(.001,velocities[i]+velocities[i-1]);return {...p,t:time,v:velocities[i]};});
}

export function compile(raw) {
  const project=validateProject(raw),{settings:g,waypoints:points}=project;
  const segments=[],beats=[],cues=[],warnings=[],arrivals=[0];
  let time=0,distance=0,left=0,right=0;
  const add=(kind,samples,index,direction=1)=>{
    const first=samples[0];
    for(const p of samples){const turn=(p.heading-first.heading)*g.wheelBase/2;p.left=left+direction*p.s-turn;p.right=right+direction*p.s+turn;}
    const last=samples.at(-1);
    segments.push({kind,samples,index,direction,start:time,duration:last.t,face:points[Math.max(0,index-1)].face});
    beats.push(segments.at(-1));
    time+=last.t;left=last.left;right=last.right;
  };
  const cue=(p,index)=>{const event={kind:'cue',index,start:time,face:p.face,...p.cue,mediaTime:p.cue.duration,duration:cueDuration(p.cue)};cues.push(event);beats.push(event);};
  const hold=(p,index)=>{const duration=Math.max(p.hold,p.cue.wait||index===points.length-1?cueDuration(p.cue):0);if(duration){const sample={x:p.x,y:p.y,heading:rad(p.heading),s:0,t:0,v:0,left,right};segments.push({kind:'hold',index,start:time,duration,direction:1,face:p.face,samples:[sample,{...sample,t:duration}]});beats.push(segments.at(-1));time+=duration;}};
  const turn=(a,from,to,index)=>{
    const angle=wrap(to-from);if(Math.abs(angle)<1e-5)return;
    const count=Math.max(20,Math.ceil(Math.abs(deg(angle))));
    const samples=Array.from({length:count+1},(_,i)=>({x:a.x,y:a.y,s:Math.abs(angle)*i/count,heading:from+angle*i/count,curvature:0}));
    const profile=timed(samples,samples.map(()=>rad(g.angularSpeed)),rad(g.angularSpeed)*2);
    // Rotation uses radians for timing; its centre does not travel.
    for(const p of profile)p.s=0;
    add('turn',profile,index);
  };
  cue(points[0],0);hold(points[0],0);
  for(let i=1;i<points.length;i++){
    const a=points[i-1],b=points[i],d=Math.hypot(b.x-a.x,b.y-a.y),dir=b.direction==='reverse'?-1:1;
    if(d<.01){turn(a,rad(a.heading),rad(b.heading),i);}
    else {
      let samples;
      if(b.path==='line'){
        const h=Math.atan2(b.y-a.y,b.x-a.x)+(dir<0?Math.PI:0);
        turn(a,rad(a.heading),h,i);
        samples=Array.from({length:Math.max(40,Math.ceil(d*3))+1},(_,j)=>j);
        const count=samples.length-1;
        samples=samples.map((_,j)=>({x:lerp(a.x,b.x,j/count),y:lerp(a.y,b.y,j/count),s:d*j/count,heading:h,curvature:0}));
      } else samples=cubicSamples(a,b);
      let tight=false,cusp=false;
      const limits=samples.map(p=>{
        const k=Math.abs(p.curvature);tight ||= k>1/g.minRadius||k*g.wheelBase/2>=.95;cusp ||= p.cusp;
        return Math.max(.3,Math.min(b.speed,g.maxWheelSpeed/(1+k*g.wheelBase/2),k>1e-8?rad(g.angularSpeed)/k:1000));
      });
      if(tight||cusp)warnings.push({index:i,severity:'error',message:`Waypoint ${i+1}: ${cusp?'path folds back on itself':'turn is too tight'}. Adjust its heading or use Turn & drive.`});
      add('move',timed(samples,limits,g.acceleration),i,dir);
      distance+=samples.at(-1).s;
      if(b.path==='line')turn(b,samples.at(-1).heading,rad(b.heading),i);
    }
    arrivals.push(time);cue(b,i);hold(b,i);
  }
  const radius=footprintRadius(g);
  const unsafe=new Set(segments.filter(s=>s.samples.some(p=>Math.abs(p.x)+radius>g.stageSize/2||Math.abs(p.y)+radius>g.stageSize/2)).map(s=>s.index));
  points.forEach((p,i)=>{if(Math.abs(p.x)+radius>g.stageSize/2||Math.abs(p.y)+radius>g.stageSize/2)unsafe.add(i);});
  [...unsafe].sort((a,b)=>a-b).forEach(index=>warnings.push({index,severity:'error',message:`Waypoint ${index+1}: Robbi's footprint crosses the stage edge.`}));
  if(time>110)warnings.push({index:0,severity:'error',message:'Robot playback is limited to 110 seconds. Shorten the animation or split it into clips.'});
  if(segments.some(s=>s.kind==='move'&&s.samples.at(-1).s>500))warnings.push({index:0,severity:'error',message:'A single path is limited to 500 cm.'});
  return {project,segments,beats,cues,arrivals,duration:time,distance,left,right,warnings};
}

export function sampleAt(plan,time) {
  const points=plan.project.waypoints;
  if(!plan.segments.length)return {x:points[0].x,y:points[0].y,heading:rad(points[0].heading),left:0,right:0,v:0,angular:0,face:points[0].face,index:0,kind:'hold',direction:1};
  const t=Math.max(0,Math.min(time,plan.duration));
  const segment=plan.segments.find(s=>t<s.start+s.duration)||plan.segments.at(-1),local=t-segment.start;
  const list=segment.samples;
  let lo=0,hi=list.length-1;
  while(lo+1<hi){const mid=(lo+hi)>>1;if(list[mid].t<local)lo=mid;else hi=mid;}
  const a=list[lo],b=list[hi],f=Math.max(0,Math.min(1,(local-a.t)/Math.max(1e-9,b.t-a.t)));
  const value=Object.fromEntries(['x','y','heading','left','right','v'].map(k=>[k,lerp(a[k],b[k],f)]));
  value.angular=(b.heading-a.heading)/Math.max(1e-9,b.t-a.t);
  return {...value,face:t>=plan.duration?points.at(-1).face:segment.face,index:segment.index,kind:segment.kind,direction:segment.direction};
}
