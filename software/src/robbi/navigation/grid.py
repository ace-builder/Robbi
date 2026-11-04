"""Bounded occupancy evidence. One vote per cell per scan; gaps stay unknown."""
import math

from .model import Halt, Ray

MAX_CELLS=60000
MAX_CELL_COORD=600


def line(a,b):
    x,y=a;xx,yy=b;dx=abs(xx-x);dy=-abs(yy-y);sx=1 if x<xx else -1;sy=1 if y<yy else -1;e=dx+dy
    while True:
        yield x,y
        if (x,y)==(xx,yy):return
        twice=2*e
        if twice>=dy:e+=dy;x+=sx
        if twice<=dx:e+=dx;y+=sy


class Grid:
    def __init__(self,resolution=5.,cells=None,*,inferred_free=(),confirmed_hits=()):
        self.resolution=float(resolution);self.cells=dict(cells or {});self.revision=0
        self.inferred_free=set(inferred_free);self.confirmed_hits=set(confirmed_hits)
        self._bounds=None

    def cell(self,x,y):return round(x/self.resolution),round(y/self.resolution)
    def world(self,c):return c[0]*self.resolution,c[1]*self.resolution

    def bounds(self):
        if self._bounds is None:
            xs=[c[0] for c in self.cells] or [-10,10];ys=[c[1] for c in self.cells] or [-10,10]
            self._bounds=(min(xs),min(ys),max(xs),max(ys))
        return self._bounds

    def _apply(self,free,hits,weak=(),tentative=()):
        weak,tentative=set(weak),set(tentative)
        keys=free|hits|weak|tentative
        if len(self.cells.keys()|keys)>MAX_CELLS or any(max(abs(x),abs(y))>MAX_CELL_COORD for x,y in keys):
            raise Halt('Map size limit reached. Save this map before starting another area.')
        for cell in free-hits:self.cells[cell]=max(-20,self.cells.get(cell,0)-4)
        inferred=set(self.inferred_free)
        for cell in weak-free-hits:
            if self.cells.get(cell,0)>=0:inferred.add(cell)
            self.cells[cell]=min(-1,self.cells.get(cell,0))
        for cell in tentative-free-hits-weak:
            self.cells[cell]=1
        # A newly observed obstacle must block a previously well-travelled cell
        # immediately; old free evidence must not conceal a moved chair/person.
        for cell in hits:self.cells[cell]=min(12,max(0,self.cells.get(cell,0))+6)
        self.inferred_free=inferred-free-hits-(tentative-weak)
        self.confirmed_hits={p for p in self.confirmed_hits|hits if self.cells.get(p,0)>=2}
        self.revision+=1;self._bounds=None

    def insert(self,pose,rays,body_radius=0):
        free,hits,weak=set(),set(),set();c=math.cos(math.radians(pose.theta_deg));s=math.sin(math.radians(pose.theta_deg))
        old_hits={p for p,v in self.cells.items() if v>=2}
        # Sparse fast sweeps can space points on a distant wall 20–30 cm apart.
        n=max(1,math.ceil(30/self.resolution))
        offsets=[(x,y) for x in range(-n,n+1) for y in range(-n,n+1) if 0<x*x+y*y<=n*n]
        def supported(p,points):return any((p[0]+x,p[1]+y) in points for x,y in offsets)
        protected=self.confirmed_hits|{p for p in old_hits if supported(p,old_hits)}
        votes={}
        def cast(ray,endpoint=True):
            x=pose.x_cm+c*ray.x_cm-s*ray.y_cm;y=pose.y_cm+s*ray.x_cm+c*ray.y_cm
            a=math.radians(pose.theta_deg+ray.angle_deg)
            end=self.cell(x+math.cos(a)*ray.distance_cm,y+math.sin(a)*ray.distance_cm)
            cells=list(line(self.cell(x,y),end))
            inferred=ray.source=='inferred_empty'
            for cell in cells[:-2] if ray.hit else cells:
                if inferred and cell in protected:break
                (weak if inferred else free).add(cell)
            if endpoint and ray.hit:
                hits.add(end);votes.setdefault(end,set()).add(ray.sweep_id or 'one-scan')
        valid=sorted((r for r in rays if r.source!='empty'),key=lambda r:r.angle_deg%360)
        for ray in valid:cast(ray)
        # Fill only small supported gaps, and only to the shorter measurement.
        # These inferred free rays never fabricate occupied endpoints for matching.
        for a,b in zip(valid,valid[1:]+valid[:1], strict=True):
            gap=(b.angle_deg-a.angle_deg)%360
            if not 0<gap<=10 or math.hypot(a.x_cm-b.x_cm,a.y_cm-b.y_cm)>3:continue
            distance=max(0,min(a.distance_cm,b.distance_cm)-self.resolution*1.5)
            n=min(16,max(1,math.ceil(math.radians(gap)*distance/self.resolution*2)))
            for i in range(1,n):
                f=i/n
                source='inferred_empty' if 'inferred_empty' in (a.source,b.source) else 'measured'
                cast(Ray(a.x_cm*(1-f)+b.x_cm*f,a.y_cm*(1-f)+b.y_cm*f,a.angle_deg+gap*f,distance,False,source),False)
        # The occupied footprint of a robot already resting on the floor is known.
        center=self.cell(pose.x_cm,pose.y_cm);n=math.ceil(body_radius/self.resolution)
        for dx in range(-n,n+1):
            for dy in range(-n,n+1):
                p=(center[0]+dx,center[1]+dy)
                if math.hypot(dx,dy)*self.resolution<=body_radius and self.cells.get(p,0)<2:free.add(p)
        stable={p for p in hits if p in old_hits or self.cells.get(p)==1 or supported(p,hits) or len(votes[p])>=2}
        tentative=hits-stable
        self._apply(free,stable,weak,tentative)

    def snapshot(self):
        x0,y0,x1,y1=self.bounds()
        return {'resolution_cm':self.resolution,'bounds':{'min_x':x0,'min_y':y0,'max_x':x1,'max_y':y1},
                'cells':[[x,y,v] for (x,y),v in self.cells.items()],'revision':self.revision,
                'inferred_free':[list(p) for p in self.inferred_free],
                'confirmed_hits':[list(p) for p in self.confirmed_hits]}
