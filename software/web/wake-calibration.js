// Same normalized cosine DTW as robbi.audio.wake_math, with bounded two-row memory.
export function dtw(a,b){
  const n=a.length/13,m=b.length/13;
  if(!Number.isInteger(n)||!Number.isInteger(m)||!n||!m||n>400||m>1000)throw Error('Invalid wake feature shape');
  let previous=new Float32Array(m+1).fill(Infinity),current=new Float32Array(m+1);previous[0]=0;
  for(let i=1;i<=n;i++){
    current[0]=Infinity;
    for(let j=1;j<=m;j++){
      let dot=0;for(let k=0;k<13;k++)dot+=a[(i-1)*13+k]*b[(j-1)*13+k];
      const cost=Math.fround(1-Math.max(-1,Math.min(1,dot)));
      current[j]=Math.fround(cost+Math.min(previous[j],current[j-1],previous[j-1]));
    }
    [previous,current]=[current,previous];
  }
  return previous[m]/(n+m);
}

export function chooseThreshold(scores){
  const positive=scores.filter(s=>s.category==='positive'),negative=scores.filter(s=>s.category!=='positive');
  if(positive.length<4||negative.filter(s=>s.category==='negative').length<3||negative.filter(s=>s.category==='noise').length<2)throw Error('More positive, negative and noise examples are required');
  const ceiling=Math.min(.5,...negative.map(s=>s.score))-.002;
  // Keep a margin below every recorded negative. Never silently train a detector
  // whose positives are indistinguishable from background or ordinary speech.
  const threshold=Math.max(.01,Math.min(ceiling,Math.max(...positive.map(s=>s.score))+.002));
  const hits=positive.filter(s=>s.score<threshold).length, falseHits=negative.filter(s=>s.score<threshold).length;
  const valid=ceiling>=.01&&hits/positive.length>=.75&&falseHits===0;
  return {valid,threshold,hits,positives:positive.length,falseHits,negatives:negative.length,
    message:valid?'Calibration ready. Apply it, then test fresh recordings.':'Examples overlap or too many wake phrases were missed. Re-record unclear positives and add similar-sounding negative speech; keep the current detector.'};
}

export function decode(features){
  if(!features||!Number.isInteger(features.rows)||features.rows<1||features.rows>1000)throw Error('Invalid wake features');
  const raw=atob(features.data);if(raw.length!==features.rows*13*4)throw Error('Invalid wake feature data');
  const bytes=Uint8Array.from(raw,c=>c.charCodeAt(0)),view=new DataView(bytes.buffer),array=new Float32Array(features.rows*13);
  for(let i=0;i<array.length;i++){array[i]=view.getFloat32(i*4,true);if(!Number.isFinite(array[i]))throw Error('Non-finite wake features')}
  return array;
}

export function calibrate(data,progress=()=>{}){
  if(data.version!==1||data.references.length>8||data.samples.length>72)throw Error('Wake training exceeds browser limits');
  const references=data.references.map(r=>({...r,features:decode(r.features)})),scores=[];
  for(const [index,sample] of data.samples.entries()){
    if(sample.windows.length>12)throw Error('Too many wake windows');
    let score=1;
    for(const encoded of sample.windows){const features=decode(encoded);for(const ref of references){
      if(sample.category==='positive'&&sample.name===ref.name)continue;
      score=Math.min(score,dtw(features,ref.features));
    }}
    scores.push({category:sample.category,score});progress(index+1,data.samples.length);
  }
  return {...chooseThreshold(scores),fingerprint:data.fingerprint,scores};
}
