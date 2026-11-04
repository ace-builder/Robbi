// Same wxyz body-to-world convention as robot.orientation / motion_capture.
export function normalize(q){
  if(!Array.isArray(q)||q.length!==4||!q.every(Number.isFinite))return null;
  const n=Math.hypot(...q);
  return n>.5&&n<1.5?q.map(v=>v/n):null;
}
export function relative(q,reference){
  const [a,b,c,d]=q,[w,x,y,z]=[reference[0],-reference[1],-reference[2],-reference[3]];
  return [a*w-b*x-c*y-d*z,a*x+b*w+c*z-d*y,a*y-b*z+c*w+d*x,a*z+b*y-c*x+d*w];
}
export const inverse=([w,x,y,z])=>[w,-x,-y,-z];
export const multiply=(a,b)=>relative(a,inverse(b));
export function angles([w,x,y,z]){
  const degrees=180/Math.PI;
  return {roll:degrees*Math.atan2(2*(w*x+y*z),1-2*(x*x+y*y)),
    pitch:degrees*Math.asin(Math.max(-1,Math.min(1,2*(w*y-z*x)))),
    yaw:degrees*Math.atan2(2*(w*z+x*y),1-2*(y*y+z*z))};
}
// Z-up sensor/world -> Three.js Y-up, xyzw order; +X is robot forward.
export const sceneQuaternion=([w,x,y,z])=>[x,z,-y,w];

export class AttitudeTracker{
  constructor(){this.reference=null;this.mount=null;this.last=null;this.count=null;this.changedAt=0;this.current=null}
  setMount(q,reference=null){this.mount=normalize(q);this.reference=this.mount&&reference?multiply(reference,this.mount):null}
  accept(sample,now){
    if(sample.enabled===false)return {ok:false,label:'Disabled',message:'The IMU is disabled in Settings.'};
    if(!sample.available)return {ok:false,label:'Unavailable',message:sample.last_error||'Waiting for the IMU connection.'};
    const q=normalize(sample.quaternion);
    if(!q||sample.fresh!==true||!Number.isFinite(sample.age_s)||sample.age_s>=.5||
      (sample.quaternion_order&&sample.quaternion_order!=='wxyz')){
      return {ok:false,label:'Stale',message:'No fresh orientation. Holding the last pose.'};
    }
    const restarted=Number.isFinite(this.count)&&Number.isFinite(sample.sample_count)&&sample.sample_count<this.count;
    const changed=sample.sample_count!==this.count||!this.last||q.some((v,i)=>Math.abs(v-this.last[i])>1e-9);
    if(changed){this.changedAt=now;this.count=sample.sample_count}
    if(!changed&&now-this.changedAt>800)return {ok:false,label:'Stale',message:'Samples stopped updating. Holding the last pose.'};
    this.last=q;
    if(this.mount){
      const body=multiply(q,this.mount);
      if(!this.reference||restarted){const yaw=angles(body).yaw*Math.PI/180;this.reference=[Math.cos(yaw/2),0,0,Math.sin(yaw/2)]}
      this.current=multiply(inverse(this.reference),body);
    }else{
      if(!this.reference||restarted)this.reference=q;
      this.current=relative(q,this.reference);
    }
    return {ok:true,label:'Live',rotation:this.current,angles:angles(this.current),restarted};
  }
  zero(){if(!this.last)return null;this.reference=this.mount?multiply(this.last,this.mount):[...this.last];this.current=[1,0,0,0];return this.current}
}
