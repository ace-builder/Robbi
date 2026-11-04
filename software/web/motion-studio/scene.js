import * as THREE from './vendor/three.module.min.js';
import {OrbitControls} from './vendor/OrbitControls.js';
import {STLLoader} from './vendor/STLLoader.js';
import {rad,deg,wrap,footprintRadius} from './motion.js';

const GREEN=0xb9efb3,CORAL=0xecb38c;
const floorPoint=p=>new THREE.Vector3(p.x,.28,-p.y);
const mat=(color,roughness=.65)=>new THREE.MeshStandardMaterial({color,roughness});
function disposeGroup(group){
  for(const child of [...group.children]){
    child.traverse(node=>{node.geometry?.dispose();if(node.material){for(const m of [node.material].flat()){m.map?.dispose();m.dispose();}}});
    group.remove(child);
  }
}
function numberSprite(value,color){
  const canvas=document.createElement('canvas');canvas.width=96;canvas.height=96;
  const ctx=canvas.getContext('2d');ctx.fillStyle=color;ctx.beginPath();ctx.arc(48,48,40,0,Math.PI*2);ctx.fill();
  ctx.fillStyle='#1c3029';ctx.font='600 33px monospace';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(value,48,49);
  const texture=new THREE.CanvasTexture(canvas);
  const sprite=new THREE.Sprite(new THREE.SpriteMaterial({map:texture,depthTest:false,transparent:true}));sprite.scale.set(3.8,3.8,1);sprite.renderOrder=10;return sprite;
}

export async function createStage(host,events){
  const renderer=new THREE.WebGLRenderer({antialias:true,alpha:false});
  renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;
  renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.3;
  renderer.domElement.setAttribute('aria-label','Robbi and draggable waypoints on a centimetre grid');
  host.append(renderer.domElement);
  const scene=new THREE.Scene();scene.background=new THREE.Color(0x18212c);scene.fog=new THREE.Fog(0x18212c,180,450);
  const camera=new THREE.PerspectiveCamera(40,1,.1,1200);camera.position.set(100,105,110);
  const controls=new OrbitControls(camera,renderer.domElement);controls.target.set(0,0,0);controls.enableDamping=true;controls.dampingFactor=.12;
  controls.minDistance=35;controls.maxDistance=420;controls.maxPolarAngle=Math.PI*.485;controls.minPolarAngle=.001;
  controls.mouseButtons={LEFT:null,MIDDLE:THREE.MOUSE.PAN,RIGHT:THREE.MOUSE.ROTATE};controls.screenSpacePanning=false;
  scene.add(new THREE.HemisphereLight(0xd9e9ff,0x4d5c53,2.5));
  const key=new THREE.DirectionalLight(0xfff3dc,3.4);key.position.set(40,100,30);key.castShadow=true;key.shadow.mapSize.set(2048,2048);
  Object.assign(key.shadow.camera,{left:-100,right:100,top:100,bottom:-100,near:1,far:250});key.shadow.normalBias=.12;key.shadow.bias=-.0002;scene.add(key);
  const fill=new THREE.DirectionalLight(0x80b3ee,1);fill.position.set(-70,45,-60);scene.add(fill);
  const ground=new THREE.Mesh(new THREE.PlaneGeometry(1500,1500),mat(0x1c2733,.95));ground.rotation.x=-Math.PI/2;ground.position.y=-.09;ground.receiveShadow=true;scene.add(ground);
  const boardGroup=new THREE.Group(),pathGroup=new THREE.Group(),markerGroup=new THREE.Group(),footprints=new THREE.Group();
  const captureGroup=new THREE.Group(),forwardRotation=new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0,1,0),Math.PI/2);
  scene.add(boardGroup,pathGroup,markerGroup,footprints,captureGroup);
  const robot=new THREE.Group();scene.add(robot);
  const robotModel=new THREE.Group();robotModel.scale.setScalar(.1);robot.add(robotModel);
  const wheelGroups={},textures=new Map();let screen,modelWheelRadius=25,mediaActive=false,currentFace='happy',lastScreenSource=null;
  const mediaCanvas=document.createElement('canvas');mediaCanvas.width=280;mediaCanvas.height=240;
  const mediaContext=mediaCanvas.getContext('2d'),mediaTexture=new THREE.CanvasTexture(mediaCanvas);mediaTexture.colorSpace=THREE.SRGBColorSpace;
  let plan=null,selected=0,tool='select',snap=true,showFootprints=false,view='3d',drag=null,pickables=[],boardKey='',disposed=false;
  const raycaster=new THREE.Raycaster(),pointer=new THREE.Vector2(),plane=new THREE.Plane(new THREE.Vector3(0,1,0),0);

  function cylinder(radius,depth,material,position,alongX=false){
    const mesh=new THREE.Mesh(new THREE.CylinderGeometry(radius,radius,depth,40),material);
    mesh.rotation.set(alongX?0:Math.PI/2,0,alongX?Math.PI/2:0);mesh.position.fromArray(position);mesh.castShadow=true;return mesh;
  }
  async function loadRobot(){
    const response=await fetch('/static/motion-studio/models/robbi.json');if(!response.ok)throw Error('Robbi model metadata could not be loaded.');
    const metadata=await response.json(),loader=new STLLoader();
    await Promise.all(metadata.meshes.map(async part=>{
      const geometry=await loader.loadAsync(`/static/motion-studio/models/${part.file}`);
      const mesh=new THREE.Mesh(geometry,mat(0xe9e5d9,.49));mesh.name=part.name;mesh.castShadow=true;robotModel.add(mesh);
    }));
    const spec=metadata.wheels;
    modelWheelRadius=spec.radius;
    for(const center of spec.centers){
      const wheel=new THREE.Group();wheel.position.fromArray(center);robotModel.add(wheel);wheelGroups[center[0]>0?'left':'right']=wheel;
      wheel.add(cylinder(spec.radius,spec.width,mat(0x474d4b,.95),[0,0,0],true));
      const side=Math.sign(center[0]);
      wheel.add(cylinder(spec.hubRadius,1.8,mat(0xd9dbce,.5),[side*(spec.width/2+1),0,0],true));
      wheel.add(cylinder(2,2,mat(0x78a18d),[side*(spec.width/2+2),spec.radius*.55,0],true));
      const treadGeometry=new THREE.BoxGeometry(spec.width+.3,1.3,2.2),treadMaterial=mat(0x59615b,.9);
      for(let i=0;i<48;i++){const angle=i/48*Math.PI*2,m=new THREE.Mesh(treadGeometry,treadMaterial);m.position.set(0,(spec.radius-.4)*Math.cos(angle),(spec.radius-.4)*Math.sin(angle));m.rotation.x=angle;wheel.add(m);}
    }
    const textureLoader=new THREE.TextureLoader();
    await Promise.all(['happy','neutral','cute','wink','surprised','thinking','sleepy','joyful','mischievous','annoyed','aware','grumpy'].map(async face=>{const t=await textureLoader.loadAsync(`/static/motion-studio/faces/${face}.png`);t.colorSpace=THREE.SRGBColorSpace;textures.set(face,t);}));
    const panel=metadata.screen;
    const back=new THREE.Mesh(new THREE.PlaneGeometry(panel.width+3,panel.height+3),mat(0x121d20));back.position.fromArray(panel.center);back.position.z-=.25;robotModel.add(back);
    screen=new THREE.Mesh(new THREE.PlaneGeometry(panel.width,panel.height),new THREE.MeshBasicMaterial({map:textures.get('happy'),toneMapped:false}));screen.position.fromArray(panel.center);robotModel.add(screen);
    const interior=new THREE.Mesh(new THREE.BoxGeometry(59,48,1),mat(0x27312e));interior.position.set(0,60,40.5);robotModel.add(interior);
    const lens=metadata.camera.center;robotModel.add(cylinder(6.3,2,mat(0x1b2426,.35),lens));robotModel.add(cylinder(4.5,2.2,mat(0x09151d,.12),[lens[0],lens[1],lens[2]+1]));robotModel.add(cylinder(2.4,.3,mat(0x37647a,.1),[lens[0],lens[1],lens[2]+2.3]));
    return metadata;
  }
  const robotReady=loadRobot();

  function board(g){
    const id=`${g.stageSize}/${g.gridSize}`;if(id===boardKey)return;boardKey=id;disposeGroup(boardGroup);
    const size=g.stageSize;
    const surface=new THREE.Mesh(new THREE.PlaneGeometry(size,size),mat(0x25313e,.92));surface.rotation.x=-Math.PI/2;surface.position.y=-.04;surface.receiveShadow=true;boardGroup.add(surface);
    const grid=new THREE.GridHelper(size,Math.round(size/g.gridSize),0xa1bacd,0x8095ab);grid.position.y=.025;grid.material.transparent=true;grid.material.opacity=.32;boardGroup.add(grid);
    const border=new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(-size/2,.02,-size/2),new THREE.Vector3(size/2,.02,-size/2),new THREE.Vector3(size/2,.02,size/2),new THREE.Vector3(-size/2,.02,size/2)]),new THREE.LineBasicMaterial({color:0x526373,transparent:true,opacity:.65}));boardGroup.add(border);
    const axes=new THREE.Group();
    axes.add(new THREE.ArrowHelper(new THREE.Vector3(1,0,0),new THREE.Vector3(-size/2+7,.2,size/2-7),7,0xc88f80,1.5,.7));
    axes.add(new THREE.ArrowHelper(new THREE.Vector3(0,0,-1),new THREE.Vector3(-size/2+7,.2,size/2-7),7,0x83bba8,1.5,.7));boardGroup.add(axes);
  }
  function marker(p,index){
    const color=index===selected?GREEN:(p.direction==='reverse'?CORAL:0x86b99c);
    const base=floorPoint(p),group=new THREE.Group();group.position.copy(base);markerGroup.add(group);
    const ring=new THREE.Mesh(new THREE.RingGeometry(index===selected?2.4:1.9,index===selected?2.8:2.2,40),new THREE.MeshBasicMaterial({color,side:THREE.DoubleSide,depthTest:false}));ring.rotation.x=-Math.PI/2;ring.renderOrder=4;group.add(ring);
    const disc=new THREE.Mesh(new THREE.CylinderGeometry(1.7,1.7,.18,30),new THREE.MeshBasicMaterial({color:0x223a30}));group.add(disc);
    const hit=new THREE.Mesh(new THREE.SphereGeometry(2.5,12,8),new THREE.MeshBasicMaterial({visible:false}));hit.userData={kind:'point',index};group.add(hit);pickables.push(hit);
    const label=numberSprite(String(index+1).padStart(2,'0'),index===selected?'#c4efba':p.direction==='reverse'?'#dcb491':'#9dc7ad');label.position.set(0,1.2,0);group.add(label);
    const h=rad(p.heading),direction=new THREE.Vector3(Math.cos(h),0,-Math.sin(h)),length=index===selected?15:11;
    const arrow=new THREE.ArrowHelper(direction,new THREE.Vector3(0,.25,0),length,color,2.6,1.5);group.add(arrow);
    arrow.cone.userData={kind:'heading',index};pickables.push(arrow.cone);
    if(index===selected){
      const handle=new THREE.Mesh(new THREE.SphereGeometry(1.5,12,8),new THREE.MeshBasicMaterial({color,transparent:true,opacity:.3}));handle.position.copy(direction.clone().multiplyScalar(length-1));handle.position.y=.35;handle.userData={kind:'heading',index};group.add(handle);pickables.push(handle);
      const guidePoints=Array.from({length:81},(_,i)=>new THREE.Vector3(Math.cos(i/80*Math.PI*2)*length,.1,Math.sin(i/80*Math.PI*2)*length));
      const guide=new THREE.Line(new THREE.BufferGeometry().setFromPoints(guidePoints),new THREE.LineDashedMaterial({color:GREEN,dashSize:.7,gapSize:1.2,transparent:true,opacity:.18}));guide.computeLineDistances();group.add(guide);
    }
  }
  function rebuild(){
    if(!plan)return;board(plan.project.settings);disposeGroup(pathGroup);disposeGroup(markerGroup);disposeGroup(footprints);pickables=[];
    for(const segment of plan.segments){
      if(segment.kind!=='move')continue;
      const points=segment.samples.map(floorPoint),color=plan.warnings.some(w=>w.index===segment.index)?0xd3a18c:segment.direction<0?CORAL:GREEN;
      const curve=new THREE.CatmullRomCurve3(points);
      const tube=new THREE.Mesh(new THREE.TubeGeometry(curve,Math.min(400,points.length),.16,5,false),new THREE.MeshBasicMaterial({color,transparent:true,opacity:.76}));pathGroup.add(tube);
      for(let i=15;i<points.length-6;i+=40){const a=points[i],b=points[i+1],direction=b.clone().sub(a).normalize();const arrow=new THREE.ArrowHelper(direction,a,2.8,color,1.6,.8);pathGroup.add(arrow);}
      if(showFootprints)for(let i=0;i<segment.samples.length;i+=25){const p=segment.samples[i],r=footprintRadius(plan.project.settings);
        const outline=new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(Array.from({length:28},(_,j)=>new THREE.Vector3(p.x+Math.cos(j/28*Math.PI*2)*r,.12,-p.y+Math.sin(j/28*Math.PI*2)*r))),new THREE.LineBasicMaterial({color,transparent:true,opacity:.11}));footprints.add(outline);
      }
    }
    plan.project.waypoints.forEach(marker);
  }
  function fit(){
    if(!plan)return;const all=plan.segments.flatMap(s=>s.samples).concat(plan.project.waypoints);
    const xs=all.map(p=>p.x),ys=all.map(p=>p.y),minX=Math.min(...xs)-17,maxX=Math.max(...xs)+17,minY=Math.min(...ys)-17,maxY=Math.max(...ys)+17;
    controls.target.set((minX+maxX)/2,0,-(minY+maxY)/2);
    const span=Math.max(maxY-minY,(maxX-minX)/camera.aspect,55),distance=Math.min(500,Math.max(80,span/(2*Math.tan(rad(camera.fov/2)))*1.22));
    const offset=view==='top'?new THREE.Vector3(.001,1,.001):new THREE.Vector3(.72,1,.93).normalize();
    camera.position.copy(controls.target).addScaledVector(offset,distance);controls.update();
  }
  function resize(){const {width,height}=host.getBoundingClientRect();if(width<1||height<1)return;camera.aspect=width/height;camera.updateProjectionMatrix();renderer.setSize(width,height);fit();}
  const observer=new ResizeObserver(resize);observer.observe(host);resize();
  function pointerRay(event){const box=host.getBoundingClientRect();pointer.set((event.clientX-box.left)/box.width*2-1,-(event.clientY-box.top)/box.height*2+1);raycaster.setFromCamera(pointer,camera);}
  function onFloor(event){pointerRay(event);const out=new THREE.Vector3();return raycaster.ray.intersectPlane(plane,out)?{x:out.x,y:-out.z}:null;}
  function snapped(value,event){const size=snap&&!event.shiftKey?plan.project.settings.gridSize:1;return Math.round(value/size)*size;}
  function down(event){
    if(event.button!==0||!plan)return;
    pointerRay(event);const hit=raycaster.intersectObjects(pickables,false)[0]?.object.userData,point=onFloor(event);
    if(!point)return;
    if(hit){
      event.stopImmediatePropagation();events.select(hit.index);const p=plan.project.waypoints[hit.index];
      drag={index:hit.index,kind:hit.kind,offsetX:p.x-point.x,offsetY:p.y-point.y,originalX:p.x,originalY:p.y,startX:event.clientX,startY:event.clientY,started:false};
      if(tool==='rotate')drag.kind='heading';controls.enabled=false;renderer.domElement.setPointerCapture(event.pointerId);return;
    }
    if(tool==='add'){event.stopImmediatePropagation();events.add(snapped(point.x,event),snapped(point.y,event));return;}
    if(tool==='rotate'){
      event.stopImmediatePropagation();const p=plan.project.waypoints[selected];events.begin();events.rotate(selected,deg(wrap(Math.atan2(point.y-p.y,point.x-p.x))));events.end();return;
    }
  }
  function move(event){
    if(!plan)return;
    if(drag){
      const point=onFloor(event);if(!point)return;
      if(!drag.started&&Math.hypot(event.clientX-drag.startX,event.clientY-drag.startY)<3)return;
      if(!drag.started){drag.started=true;events.begin();}
      if(drag.kind==='heading'){
        const p=plan.project.waypoints[drag.index];let heading=deg(Math.atan2(point.y-p.y,point.x-p.x));heading=Math.round(heading/(event.shiftKey?1:5))*(event.shiftKey?1:5);events.rotate(drag.index,heading);
      }else events.move(drag.index,snapped(point.x+drag.offsetX,event),snapped(point.y+drag.offsetY,event));
      return;
    }
    pointerRay(event);const hit=raycaster.intersectObjects(pickables,false)[0];renderer.domElement.style.cursor=hit?'grab':tool==='add'?'crosshair':tool==='rotate'?'crosshair':'default';
  }
  function up(){if(drag?.started)events.end();drag=null;controls.enabled=true;}
  renderer.domElement.addEventListener('pointerdown',down,true);renderer.domElement.addEventListener('pointermove',move);renderer.domElement.addEventListener('pointerup',up);renderer.domElement.addEventListener('pointercancel',up);renderer.domElement.addEventListener('lostpointercapture',up);
  renderer.domElement.addEventListener('dblclick',event=>{if(tool!=='select'||!plan)return;const p=onFloor(event);if(p)events.add(snapped(p.x,event),snapped(p.y,event));});
  let frame;
  function render(){if(disposed)return;controls.update();renderer.render(scene,camera);events.frame?.();frame=requestAnimationFrame(render);}
  render();

  const stage={
    setPlan(value,index=selected){plan=value;selected=index;const g=plan.project.settings,scale=g.wheelDiameter*5/modelWheelRadius;robotModel.position.y=g.wheelDiameter/2-modelWheelRadius/10;for(const wheel of Object.values(wheelGroups)){wheel.scale.set(1,scale,scale);wheel.position.x=Math.sign(wheel.position.x)*g.wheelBase*5;}rebuild();},
    select(index){selected=index;rebuild();},
    setTool(value){tool=value;renderer.domElement.style.cursor=value==='select'?'default':'crosshair';},
    setSnap(value){snap=value;},
    setFootprints(value){showFootprints=value;rebuild();},
    setPose(pose){robot.position.set(pose.x,0,-pose.y);if(pose.orientation){const [w,x,y,z]=pose.orientation;robot.quaternion.set(x,z,-y,w).multiply(forwardRotation);}else robot.rotation.set(0,Math.PI/2+pose.heading,0);const radius=plan?.project.settings.wheelDiameter/2||2.5;if(wheelGroups.left)wheelGroups.left.rotation.x=pose.left/radius;if(wheelGroups.right)wheelGroups.right.rotation.x=pose.right/radius;currentFace=pose.face;if(screen&&!mediaActive&&textures.has(pose.face))screen.material.map=textures.get(pose.face);},
    setCapture(samples){disposeGroup(captureGroup);pathGroup.visible=markerGroup.visible=footprints.visible=!samples;if(samples?.length>1){const geometry=new THREE.BufferGeometry().setFromPoints(samples.map(floorPoint));captureGroup.add(new THREE.Line(geometry,new THREE.LineBasicMaterial({color:CORAL})));}},
    setScreen(source){
      if(!source){mediaActive=false;lastScreenSource=null;if(screen&&textures.has(currentFace))screen.material.map=textures.get(currentFace);return;}
      if(!screen)return;
      const video=source instanceof HTMLVideoElement,width=source.videoWidth||source.naturalWidth||source.width,height=source.videoHeight||source.naturalHeight||source.height;
      if(!width||!height||(!video&&lastScreenSource===source))return;
      const scale=Math.min(280/width,240/height);mediaContext.fillStyle='#000';mediaContext.fillRect(0,0,280,240);mediaContext.drawImage(source,(280-width*scale)/2,(240-height*scale)/2,width*scale,height*scale);mediaTexture.needsUpdate=true;screen.material.map=mediaTexture;mediaActive=true;lastScreenSource=source;
    },
    setView(value){view=value;stage.fit();},
    fit,
    screenPoint(x,y,height=.4){const v=new THREE.Vector3(x,height,-y).project(camera),box=host.getBoundingClientRect();return{x:box.left+(v.x+1)/2*box.width,y:box.top+(1-v.y)/2*box.height};},
    dispose(){disposed=true;cancelAnimationFrame(frame);observer.disconnect();controls.dispose();disposeGroup(pathGroup);disposeGroup(markerGroup);disposeGroup(boardGroup);disposeGroup(footprints);disposeGroup(captureGroup);mediaTexture.dispose();for(const texture of textures.values())texture.dispose();renderer.dispose();},
  };
  try{await robotReady;events.modelLoaded?.();}catch(error){events.modelError?.(error);}
  host.querySelector('#scene-loading')?.remove();
  return stage;
}
