"""Bounded correlative matching with explicit ambiguity rejection."""
from collections import deque
import heapq
import math
import time

from .model import Halt,Pose,angle


class Matcher:
    def __init__(self,grid,check=lambda:None):
        self.grid=grid;self.check=check
        x0,y0,x1,y1=grid.bounds();pad=12
        self.x0=x0-pad;self.y0=y0-pad;self.width=x1-x0+1+2*pad;self.height=y1-y0+1+2*pad
        if self.width*self.height>1600000:raise Halt('Map extent exceeds localization limits.')
        distances=bytearray([255])*(self.width*self.height);queue=deque()
        for (x,y),v in grid.cells.items():
            if v>=2:
                index=(y-self.y0)*self.width+x-self.x0;distances[index]=0;queue.append(index)
        visited=0
        while queue:
            index=queue.popleft();d=distances[index]+1;visited+=1
            if visited%1024==0:check()
            if d>12:continue
            x=index%self.width;y=index//self.width
            for dx,dy in ((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)):
                xx=x+dx;yy=y+dy
                if 0<=xx<self.width and 0<=yy<self.height:
                    q=yy*self.width+xx
                    if distances[q]>d:distances[q]=d;queue.append(q)
        self.fields={}
        for sigma in (12.,30.):
            lookup=[round(255*math.exp(-.5*(d*grid.resolution/sigma)**2)) if d<=12 else 0 for d in range(256)]
            self.fields[sigma]=bytes(lookup[d] for d in distances)

    def search(self,rays,prior=None,*,hint=None,budget_s=12):
        hits=[r for r in rays if r.hit]
        if len(hits)<18:raise Halt('Too few obstacle returns to locate Robbi. Scan near visible walls or furniture.')
        points=[r.endpoint() for r in hits]
        points=points[::max(1,math.ceil(len(points)/90))]
        coarse=points[::max(1,math.ceil(len(points)/30))]
        deadline=time.monotonic()+budget_s;evaluated=0
        try:
            import numpy as np
        except ImportError:np=None

        def scores(candidates,sigma,ends):
            nonlocal evaluated
            field=self.fields[sigma];ranked=[];distinct={}
            for offset in range(0,len(candidates),256):
                self.check()
                if time.monotonic()>deadline:raise Halt('Localization search needs a position hint. Tap near Robbi on the map.')
                batch=candidates[offset:offset+256];evaluated+=len(batch)
                if np is not None:
                    poses=np.asarray(batch,dtype=np.float64);p=np.asarray(ends,dtype=np.float64)
                    theta=np.deg2rad(poses[:,2,None]);c=np.cos(theta);s=np.sin(theta)
                    xx=np.rint((poses[:,0,None]+c*p[None,:,0]-s*p[None,:,1])/self.grid.resolution).astype(np.int32)-self.x0
                    yy=np.rint((poses[:,1,None]+s*p[None,:,0]+c*p[None,:,1])/self.grid.resolution).astype(np.int32)-self.y0
                    valid=(xx>=0)&(xx<self.width)&(yy>=0)&(yy<self.height)
                    indices=np.clip(yy,0,self.height-1)*self.width+np.clip(xx,0,self.width-1)
                    values=np.frombuffer(field,dtype=np.uint8)[indices]*valid
                    values=values.mean(axis=1)/255
                else:
                    values=[]
                    for x,y,t in batch:
                        c=math.cos(math.radians(t));s=math.sin(math.radians(t));total=0
                        for px,py in ends:
                            ix=round((x+c*px-s*py)/self.grid.resolution)-self.x0
                            iy=round((y+s*px+c*py)/self.grid.resolution)-self.y0
                            if 0<=ix<self.width and 0<=iy<self.height:total+=field[iy*self.width+ix]
                        values.append(total/(255*len(ends)))
                batch_scores=[(float(score),pose) for score,pose in zip(values,batch, strict=True)]
                ranked=heapq.nlargest(128,ranked+batch_scores)
                # Retain distinct alternatives even when the best neighborhood
                # has hundreds of nearly identical fine-grid candidates.
                for score,p in batch_scores:
                    key=(round(p[0]/20),round(p[1]/20),round(angle(p[2])/12))
                    if score>distinct.get(key,(-1,None))[0]:distinct[key]=(score,p)
                distinct=dict(heapq.nlargest(128,distinct.items(),key=lambda pair:pair[1][0]))
                time.sleep(0)
            return sorted(set(ranked+list(distinct.values())),reverse=True)

        global_search=prior is None
        reference=prior or (Pose(hint['x_cm'],hint['y_cm'],hint.get('theta_deg') or 0) if hint else None)
        if reference:
            radius=50 if global_search else min(30,max(15,prior.sigma_cm*2))
            step=10 if global_search else 5
            headings=range(-180,180,12) if global_search and hint.get('theta_deg') is None else range(-18,19,3)
            candidates=[(reference.x_cm+dx,reference.y_cm+dy,angle(reference.theta_deg+dt))
                for dx in range(-round(radius),round(radius)+1,step)
                for dy in range(-round(radius),round(radius)+1,step) for dt in headings
                if self.grid.cells.get(self.grid.cell(reference.x_cm+dx,reference.y_cm+dy),0)<0]
        else:
            # Bound candidates spatially without silently accepting a partial search.
            positions={((x//4)*4,(y//4)*4) for (x,y),v in self.grid.cells.items() if v<0}
            if len(positions)>4000:raise Halt('This map needs an approximate position hint for global localization.')
            candidates=[(*self.grid.world(p),t) for p in positions if self.grid.cells.get(p,0)<0 for t in range(-180,180,12)]
        if not candidates:raise Halt('No candidate positions in known free space.')
        coarse_rank=scores(candidates,30.,coarse)
        seeds=[]
        for score,p in coarse_rank:
            if all(math.hypot(p[0]-q[0],p[1]-q[1])>15 or abs(angle(p[2]-q[2]))>12 for q in seeds):seeds.append(p)
            if len(seeds)==12:break
        refined=set()
        for x,y,t in seeds:
            for dx in range(-15,16,5):
                for dy in range(-15,16,5):
                    if self.grid.cells.get(self.grid.cell(x+dx,y+dy),0)>=0:continue
                    for dt in range(-9,10,3):refined.add((x+dx,y+dy,angle(t+dt)))
        ranked=scores(list(refined),12.,points)
        if not ranked:raise Halt('No valid localization hypotheses.')
        best,p=ranked[0]
        fine=[(p[0]+dx,p[1]+dy,angle(p[2]+dt)) for dx in (-4,-2,0,2,4) for dy in (-4,-2,0,2,4) for dt in (-2,-1,0,1,2)
              if self.grid.cells.get(self.grid.cell(p[0]+dx,p[1]+dy),0)<0]
        ranked=sorted(ranked+scores(fine,12.,points),reverse=True);best,p=ranked[0]
        alternatives=[(s,q) for s,q in ranked if math.dist(p[:2],q[:2])>25 or abs(angle(p[2]-q[2]))>18]
        second=alternatives[0][0] if alternatives else 0.
        # Free-space contradictions catch wall-only fits crossing occupied rooms.
        contradictions=0;checked=0;c=math.cos(math.radians(p[2]));s=math.sin(math.radians(p[2]))
        for ray in hits:
            for fraction in (.4,.75):
                a=math.radians(ray.angle_deg);x=ray.x_cm+math.cos(a)*ray.distance_cm*fraction;y=ray.y_cm+math.sin(a)*ray.distance_cm*fraction
                checked+=1
                contradictions+=self.grid.cells.get(self.grid.cell(p[0]+c*x-s*y,p[1]+s*x+c*y),0)>=2
        spread=[q for score,q in ranked if score>=best-.02]
        sigma=max(3.,min(30.,max((math.dist(p[:2],q[:2]) for q in spread),default=0)/2))
        accepted=best>=(.60 if global_search else .43) and best-second>=.035 and contradictions/max(1,checked)<.08
        return {'accepted':accepted,'pose':Pose(*p,sigma,3.),'score':round(best,3),
                'margin':round(best-second,3),'candidates':evaluated,'global':global_search,
                'alternatives':[{'score':round(s,3),'x_cm':q[0],'y_cm':q[1],'theta_deg':q[2]} for s,q in alternatives[:4]],
                'reason':'' if accepted else 'The scan has no distinct reliable match. Add a position/heading hint or scan from a more distinctive place.'}
