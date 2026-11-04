import test from 'node:test';
import assert from 'node:assert/strict';
import {AttitudeTracker,normalize,relative,angles,sceneQuaternion,multiply,inverse} from './orientation.mjs';
const sample=(quaternion,count=1)=>({enabled:true,available:true,fresh:true,age_s:.01,quaternion,quaternion_order:'wxyz',sample_count:count});
const near=(a,b)=>assert.ok(Math.abs(a-b)<1e-6,`${a} != ${b}`);
test('PCB mounting cancels and each rotation axis matches Motion Studio',()=>{
  const s=Math.SQRT1_2,reference=[s,0,s,0];
  for(const [q,key] of [[[s,s,0,0],'roll'],[[s,0,s,0],'pitch'],[[s,0,0,s],'yaw']]){
    // Multiply desired world rotation by the sensor mounting reference.
    const mounted=relative(q,[reference[0],-reference[1],-reference[2],-reference[3]]);
    const delta=relative(mounted,reference);near(angles(delta)[key],90);
  }
  assert.deepEqual(sceneQuaternion([1,2,3,4]),[2,4,-3,1]);
});
test('invalid or stale data freezes the last pose',()=>{
  const tracker=new AttitudeTracker();tracker.accept(sample([1,0,0,0]),0);
  assert.equal(tracker.accept(sample([1,0,0,0]),900).label,'Stale');
  for(const q of [null,[0,0,0,0],[NaN,0,0,1],[2,0,0,0],['1',0,0,0]])assert.equal(normalize(q),null);
  assert.equal(tracker.accept({...sample([1,0,0,0],2),fresh:false},1000).ok,false);
  assert.deepEqual(tracker.current,[1,0,0,0]);
  assert.equal(tracker.accept(sample([1,0,0,0],3),1100).ok,true);
});
test('recenter is local and service restart resets the reference',()=>{
  const tracker=new AttitudeTracker(),q=[Math.SQRT1_2,0,0,Math.SQRT1_2];
  tracker.accept(sample([1,0,0,0],10),0);
  near(tracker.accept(sample(q,20),100).angles.yaw,90);
  tracker.zero();near(tracker.accept(sample(q,21),200).angles.yaw,0);
  assert.equal(tracker.accept(sample([1,0,0,0],1),300).restarted,true);
  assert.deepEqual(tracker.current,[1,0,0,0]);
});
test('q and -q describe the same rotation, including near the yaw wrap',()=>{
  const q=[.01,0,0,Math.sqrt(1-.0001)];
  near(angles(q).yaw,angles(q.map(v=>-v)).yaw);
  near(Math.abs(angles(relative(q,q.map(v=>-v))).yaw),0);
});
test('calibrated sensor frame handles combined turns without mixing axes',()=>{
  const axis=(angle,index)=>{const q=[Math.cos(angle/2),0,0,0];q[index]=Math.sin(angle/2);return q};
  const mount=multiply(axis(2.7,2),axis(.4,1)),heading=axis(.7,3);
  const initial=multiply(heading,inverse(mount));
  const tracker=new AttitudeTracker();tracker.setMount(mount,initial);
  for(const [rotation,key] of [[axis(.6,1),'roll'],[axis(.8,2),'pitch'],[axis(-1.3,3),'yaw'],[multiply(axis(.8,3),axis(.4,2)),'pitch']]){
    const raw=multiply(multiply(heading,rotation),inverse(mount));
    const result=tracker.accept(sample(raw,++qaCount),qaCount*100);
    const expected=angles(rotation);
    for(const name of ['roll','pitch','yaw'])near(result.angles[name],expected[name]);
    assert.ok(result.ok,key);
  }
  tracker.zero();const result=tracker.accept(sample(tracker.last,++qaCount),qaCount*100);near(result.angles.roll,0);near(result.angles.pitch,0);near(result.angles.yaw,0);
});
let qaCount=0;
