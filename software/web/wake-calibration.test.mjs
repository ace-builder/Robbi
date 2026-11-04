import test from 'node:test';
import assert from 'node:assert/strict';
import {dtw,chooseThreshold,calibrate} from './wake-calibration.js';

test('identical and orthogonal normalized features match known cosine-DTW distances',()=>{
  const a=new Float32Array(26),b=new Float32Array(26);a[0]=a[13]=1;b[1]=b[14]=1;
  assert.equal(dtw(a,a),0);assert.equal(dtw(a,b),.5);
});
test('overlapping speech cannot be offered for activation',()=>{
  const scores=[...Array.from({length:4},()=>({category:'positive',score:.2})),...Array.from({length:3},()=>({category:'negative',score:.1})),...Array.from({length:2},()=>({category:'noise',score:1}))];
  assert.equal(chooseThreshold(scores).valid,false);
  scores.filter(s=>s.category==='negative').forEach(s=>s.score=.4);
  const result=chooseThreshold(scores);assert.equal(result.valid,true);assert.equal(result.hits,4);assert.equal(result.falseHits,0);
});
test('training rejects unbounded input',()=>{
  assert.throws(()=>calibrate({version:1,references:Array(9),samples:[]}),/limits/);
});
