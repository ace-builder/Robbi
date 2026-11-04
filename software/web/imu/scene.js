import * as THREE from '../motion-studio/vendor/three.module.min.js';
import {OrbitControls} from '../motion-studio/vendor/OrbitControls.js';
import {loadRobbiModel,disposeModel} from './robbi-model.js';
import {sceneQuaternion} from './orientation.mjs';

export function createIMUScene(host,signal,onState){
  const renderer=new THREE.WebGLRenderer({antialias:true});
  renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));
  renderer.outputColorSpace=THREE.SRGBColorSpace;
  const canvas=renderer.domElement;canvas.setAttribute('role','img');canvas.setAttribute('aria-label','Live 3D Robbi: tilt and rotation from the IMU');host.prepend(canvas);
  const scene=new THREE.Scene();scene.background=new THREE.Color(0xfafbf9);
  const camera=new THREE.PerspectiveCamera(35,1,1,1600);
  const controls=new OrbitControls(camera,canvas);controls.enablePan=false;controls.enableDamping=false;controls.minDistance=170;controls.maxDistance=650;
  scene.add(new THREE.HemisphereLight(0xffffff,0xa5a79f,2.5));
  const key=new THREE.DirectionalLight(0xffffff,2.8);key.position.set(180,300,220);scene.add(key);
  const grid=new THREE.GridHelper(340,17,0xbfc6be,0xe2e6df);scene.add(grid);
  const pivot=new THREE.Group();pivot.position.y=95;scene.add(pivot);
  const assembly=new THREE.Group();assembly.rotation.y=Math.PI/2;pivot.add(assembly);
  const target=new THREE.Quaternion();
  let disposed=false,visible=!document.hidden,frame=0,lastFrame=0,loaded=false;
  const reduceMotion=matchMedia('(prefers-reduced-motion: reduce)').matches;
  function requestRender(){if(!disposed&&visible&&!frame)frame=requestAnimationFrame(render)}
  function render(now){
    frame=0;if(disposed||!visible)return;
    const dt=lastFrame?now-lastFrame:33;
    if(dt<30){requestRender();return}
    lastFrame=now;
    if(reduceMotion)pivot.quaternion.copy(target);
    else pivot.quaternion.slerp(target,1-Math.exp(-Math.min(dt,100)/55));
    renderer.render(scene,camera);
    if(pivot.quaternion.angleTo(target)>.0005)requestRender();
  }
  function resetCamera(){camera.position.set(265,175,220);controls.target.set(0,95,0);controls.update();requestRender()}
  function resize(){if(disposed)return;const {width,height}=host.getBoundingClientRect();if(width<1||height<1)return;camera.aspect=width/height;camera.updateProjectionMatrix();renderer.setSize(width,height);requestRender()}
  function dispose(){
    if(disposed)return;disposed=true;cancelAnimationFrame(frame);observer.disconnect();controls.dispose();
    canvas.removeEventListener('webglcontextlost',lost);signal.removeEventListener('abort',dispose);
    disposeModel(scene);renderer.dispose();renderer.forceContextLoss();canvas.remove();
  }
  function lost(event){event.preventDefault();onState('3D view lost its graphics connection. Reopen this tab to retry.',true);dispose()}
  canvas.addEventListener('webglcontextlost',lost);
  controls.addEventListener('change',requestRender);
  const observer=new ResizeObserver(resize);observer.observe(host);resetCamera();resize();
  signal.addEventListener('abort',dispose,{once:true});
  loadRobbiModel(signal).then(model=>{
    if(disposed||signal.aborted){disposeModel(model);return}
    const center=new THREE.Box3().setFromObject(model).getCenter(new THREE.Vector3());
    model.position.copy(center).negate();assembly.add(model);loaded=true;onState('',false);requestRender();
  }).catch(error=>{if(!disposed&&!signal.aborted)onState('3D model unavailable. '+error.message+' Reopen this tab to retry.',true)});
  return {
    setOrientation(q){if(disposed)return;target.fromArray(sceneQuaternion(q)).normalize();requestRender()},
    freeze(){target.copy(pivot.quaternion)},
    setVisible(value){visible=value;if(!visible){cancelAnimationFrame(frame);frame=0}else{lastFrame=0;requestRender()}},
    resetCamera,dispose,
    // A DOM-visible readiness flag is used by functional/browser diagnostics.
    get ready(){return loaded&&!disposed},
  };
}
