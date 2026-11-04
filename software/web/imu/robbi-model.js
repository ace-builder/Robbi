import * as THREE from '../motion-studio/vendor/three.module.min.js';
import {STLLoader} from '../motion-studio/vendor/STLLoader.js';

const BASE='/static/motion-studio/';
export function disposeModel(group){
  const geometries=new Set(),materials=new Set(),textures=new Set();
  group.traverse(node=>{if(node.geometry)geometries.add(node.geometry);for(const m of [node.material].flat())if(m)materials.add(m)});
  for(const m of materials){if(m.map)textures.add(m.map);m.dispose()}
  for(const t of textures){t.image?.close?.();t.dispose()}
  for(const g of geometries)g.dispose();
  group.clear();
}

// Reuse the existing CAD-derived shell/head and their assembly metadata.
// Only one face is loaded; no animation catalogs, robot commands or server work.
export async function loadRobbiModel(signal){
  const abort=new AbortController(),cancel=()=>abort.abort();signal.addEventListener('abort',cancel,{once:true});
  const timeout=setTimeout(cancel,15000);if(signal.aborted)cancel();
  const get=async path=>{const r=await fetch(path.startsWith('/')?path:BASE+path,{signal:abort.signal});if(!r.ok)throw Error('Robbi model file could not be loaded.');return r};
  const group=new THREE.Group(),loader=new STLLoader();
  const material=color=>new THREE.MeshStandardMaterial({color,roughness:.65});
  function cylinder(radius,depth,color,position,axle=false){
    const mesh=new THREE.Mesh(new THREE.CylinderGeometry(radius,radius,depth,32),material(color));
    mesh.rotation.set(axle?0:Math.PI/2,0,axle?Math.PI/2:0);mesh.position.fromArray(position);return mesh;
  }
  try{
    const metadata=await (await get('models/robbi.json')).json();
    const parts=await Promise.allSettled(metadata.meshes.map(async part=>{
      // Flask serves these precompressed files with Content-Encoding: gzip;
      // the browser transparently decodes them. The Pi does no compression.
      const geometry=loader.parse(await (await get('/static/imu/models/'+part.file+'.gz')).arrayBuffer());
      const mesh=new THREE.Mesh(geometry,material(part.color||0xe7e1d6));mesh.name=part.name;group.add(mesh);
    }));
    const failed=parts.find(p=>p.status==='rejected');if(failed)throw failed.reason;
    signal.throwIfAborted();
    const wheels=metadata.wheels;
    for(const center of wheels.centers){
      const wheel=new THREE.Group();wheel.position.fromArray(center);group.add(wheel);
      wheel.add(cylinder(wheels.radius,wheels.width,0x454947,[0,0,0],true));
      wheel.add(cylinder(wheels.hubRadius,1.8,0xe2dfd5,[Math.sign(center[0])*(wheels.width/2+1),0,0],true));
      const treadGeometry=new THREE.BoxGeometry(wheels.width+.3,1.3,2.2),treadMaterial=material(0x60645f);
      for(let i=0;i<40;i++){
        const a=i/40*Math.PI*2,tread=new THREE.Mesh(treadGeometry,treadMaterial);
        tread.position.set(0,(wheels.radius-.4)*Math.cos(a),(wheels.radius-.4)*Math.sin(a));tread.rotation.x=a;wheel.add(tread);
      }
    }
    const panel=metadata.screen;
    const back=new THREE.Mesh(new THREE.PlaneGeometry(panel.width+3,panel.height+3),material(0x111b1e));
    back.position.fromArray(panel.center);back.position.z-=.25;group.add(back);
    const image=await createImageBitmap(await (await get('faces/happy.png')).blob(),{imageOrientation:'flipY'});
    const texture=new THREE.Texture(image);texture.colorSpace=THREE.SRGBColorSpace;texture.needsUpdate=true;
    const face=new THREE.Mesh(new THREE.PlaneGeometry(panel.width,panel.height),new THREE.MeshBasicMaterial({map:texture,toneMapped:false}));
    face.position.fromArray(panel.center);group.add(face);
    const inside=new THREE.Mesh(new THREE.BoxGeometry(59,48,1),material(0x28302e));inside.position.set(0,60,40.5);group.add(inside);
    const lens=metadata.camera.center;
    group.add(cylinder(6.3,2,0x151d20,lens),cylinder(4.5,2.2,0x09151d,[lens[0],lens[1],lens[2]+1]),cylinder(2.4,.3,0x37647a,[lens[0],lens[1],lens[2]+2.3]));
    signal.throwIfAborted();
    return group;
  }catch(error){disposeModel(group);throw error}
  finally{clearTimeout(timeout);signal.removeEventListener('abort',cancel)}
}
