// Browser-only rendering. Robot sends bounded occupancy cells, never images.
export function mapCanvas(canvas,life,onSelect){
  const ctx=canvas.getContext('2d');let grid=null,status={},trail=[],selected=null,scale=1,cx=0,cy=0,auto=true,drag=null;
  let raster=null,bounds=null,resolution=5,frame=null;
  const width=()=>canvas.clientWidth,height=()=>canvas.clientHeight;
  const project=(x,y)=>[width()/2+(x-cx)*scale,height()/2-(y-cy)*scale];
  function fit(){if(!bounds||width()<=70||height()<=70)return;cx=(bounds.min_x+bounds.max_x)*resolution/2;cy=(bounds.min_y+bounds.max_y)*resolution/2;scale=Math.min((width()-70)/((bounds.max_x-bounds.min_x+8)*resolution),(height()-70)/((bounds.max_y-bounds.min_y+8)*resolution));auto=true}
  function draw(){if(frame===null&&!life.signal.aborted)frame=requestAnimationFrame(()=>{frame=null;paint()})}
  function paint(){
    const dpr=Math.min(2,devicePixelRatio||1),w=width(),h=height();if(!w||!h)return;
    if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)){canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);if(auto)fit()}
    ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);ctx.fillStyle='#f2f2f2';ctx.fillRect(0,0,w,h);
    if(!raster){ctx.fillStyle='#676767';ctx.font='14px system-ui';ctx.textAlign='center';ctx.fillText('Your map will appear after the first scan.',w/2,h/2);return}
    const [x,y]=project((bounds.min_x-.5)*resolution,(bounds.max_y+.5)*resolution);
    ctx.imageSmoothingEnabled=false;ctx.drawImage(raster,x,y,raster.width*resolution*scale,raster.height*resolution*scale);
    function path(points,color,lineWidth){if(!points?.length)return;ctx.beginPath();points.forEach((p,i)=>{const [xx,yy]=project(...p);i?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy)});ctx.strokeStyle=color;ctx.lineWidth=lineWidth;ctx.stroke()}
    path(status.trail?.length?status.trail:trail,'#b4b4b4',1.5);path(status.route,'#217c68',3);
    const p=status.pose||status.estimated_pose;
    const tof=status.tof,settings=status.settings||{};
    if(p&&status.localized&&tof?.fresh&&Number.isFinite(tof.distance_cm)&&tof.distance_cm>0){const a=p.theta_deg*Math.PI/180,b=(p.theta_deg+(settings.sensor_yaw_deg||0))*Math.PI/180,f=settings.sensor_forward_cm||0,l=settings.sensor_left_cm||0,x=p.x_cm+Math.cos(a)*f-Math.sin(a)*l,y=p.y_cm+Math.sin(a)*f+Math.cos(a)*l;path([[x,y],[x+Math.cos(b)*tof.distance_cm,y+Math.sin(b)*tof.distance_cm]],'#b16c20',2)}
    if(p){const [xx,yy]=project(p.x_cm,p.y_cm);ctx.save();ctx.translate(xx,yy);ctx.rotate(-p.theta_deg*Math.PI/180);ctx.strokeStyle=status.localized?'#14634f':'#777';ctx.fillStyle=status.localized?'#14634f':'#fff';ctx.lineWidth=2;ctx.beginPath();ctx.arc(0,0,Math.max(7,(status.settings?.robot_radius_cm||12)*scale),0,2*Math.PI);ctx.stroke();ctx.beginPath();ctx.moveTo(12,0);ctx.lineTo(-6,-6);ctx.lineTo(-6,6);ctx.closePath();ctx.fill();ctx.stroke();ctx.restore()}
    if(selected){const [xx,yy]=project(selected.x_cm,selected.y_cm);ctx.strokeStyle='#b16c20';ctx.lineWidth=2;ctx.beginPath();ctx.arc(xx,yy,8,0,Math.PI*2);ctx.moveTo(xx-13,yy);ctx.lineTo(xx+13,yy);ctx.moveTo(xx,yy-13);ctx.lineTo(xx,yy+13);ctx.stroke()}
    const cm=scale>1?50:100;ctx.fillStyle='#333';ctx.fillRect(18,h-24,cm*scale,2);ctx.textAlign='left';ctx.font='12px system-ui';ctx.fillText(cm+' cm',18,h-32);
  }
  function zoom(factor){scale=Math.max(.005,Math.min(12,scale*factor));auto=false;draw()}
  life.on(canvas,'wheel',e=>{e.preventDefault();zoom(e.deltaY<0?1.15:1/1.15)});
  life.on(canvas,'pointerdown',e=>{if(e.button!==0||drag)return;drag={id:e.pointerId,x:e.clientX,y:e.clientY,cx,cy,moved:false};canvas.setPointerCapture(e.pointerId)});
  life.on(canvas,'pointermove',e=>{if(!drag||drag.id!==e.pointerId)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.hypot(dx,dy)>6)drag.moved=true;if(drag.moved){cx=drag.cx-dx/scale;cy=drag.cy+dy/scale;auto=false;draw()}});
  life.on(canvas,'pointerup',e=>{if(!drag||drag.id!==e.pointerId)return;if(!drag.moved&&grid){const r=canvas.getBoundingClientRect();selected={x_cm:Math.round(cx+(e.clientX-r.left-canvas.clientLeft-width()/2)/scale),y_cm:Math.round(cy-(e.clientY-r.top-canvas.clientTop-height()/2)/scale)};onSelect(selected);draw()}drag=null});
  life.on(canvas,'pointercancel',()=>drag=null);
  const observer=new ResizeObserver(draw);observer.observe(canvas);life.add(()=>{observer.disconnect();if(frame!==null)cancelAnimationFrame(frame)});
  return {zoom,fit(){fit();draw()},select(value){selected=value;draw()},update(data,info){status=info||status;if(data?.grid){grid=data.grid;bounds=grid.bounds;resolution=grid.resolution_cm;trail=data.trail||[];raster=document.createElement('canvas');raster.width=bounds.max_x-bounds.min_x+1;raster.height=bounds.max_y-bounds.min_y+1;const c=raster.getContext('2d');c.fillStyle='#f2f2f2';c.fillRect(0,0,raster.width,raster.height);for(const [xx,yy,v] of grid.cells){c.fillStyle=v>=2?'#303030':v<0?'#fff':'#e1e1e1';c.fillRect(xx-bounds.min_x,bounds.max_y-yy,1,1)}if(auto)fit()}draw()}};
}
