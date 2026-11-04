"""Footprint-aware grid planning and reachable exploration frontiers."""
from collections import deque
from bisect import bisect_left
import heapq
import math
import time

from .grid import line
from .model import Halt

NEIGHBORS=((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1))


def _runs(values):
    """Contiguous coordinates; unknown cells outside each run stay obstacles."""
    run=[]
    for value in sorted(values):
        if run and value!=run[-1]+1:
            yield run;run=[]
        run.append(value)
    if run:yield run


def _distance_transform(values):
    """Exact 1D squared Euclidean transform, using a parabola lower envelope."""
    sites=[0];edges=[-math.inf,math.inf]
    for q in range(1,len(values)):
        p=sites[-1]
        crossing=((values[q]+q*q)-(values[p]+p*p))/(2*(q-p))
        while crossing<=edges[-2]:
            sites.pop();edges.pop();p=sites[-1]
            crossing=((values[q]+q*q)-(values[p]+p*p))/(2*(q-p))
        sites.append(q);edges[-1]=crossing;edges.append(math.inf)
    result=[];k=0
    for q in range(len(values)):
        while edges[k+1]<q:k+=1
        result.append((q-sites[k])**2+values[sites[k]])
    return result


def _clearance(free,check):
    # Two separable transforms give the exact distance to a non-free cell
    # center. Work on contiguous sparse runs rather than a potentially huge
    # bounding rectangle; unknown cells at each run's ends have distance zero.
    columns={};rows={};vertical={};visits=0
    for x,y in free:
        columns.setdefault(x,[]).append(y);rows.setdefault(y,[]).append(x)
    for x,ys in columns.items():
        for run in _runs(ys):
            lo,hi=run[0]-1,run[-1]+1
            for y in run:vertical[x,y]=min(y-lo,hi-y)**2
        visits+=len(ys)
        if visits>=512:check();visits=0
    result={}
    for y,xs in rows.items():
        for run in _runs(xs):
            distances=_distance_transform([0]+[vertical[x,y] for x in run]+[0])
            for x,squared in zip(run,distances[1:-1],strict=True):
                # Subtract both half-cell diagonals. This covers the occupied
                # cell's area AND any actual pose within the selected free cell,
                # including between centers on a smoothed route.
                result[x,y]=max(0.,math.sqrt(squared)-math.sqrt(2))
        visits+=len(xs)
        if visits>=512:check();visits=0
    check()
    return result


def _obstacle_clearance(grid,free,radius,check):
    """Distance to measured obstacles; unobserved side margins are uncertainty."""
    rows={}
    for (x,y),value in grid.cells.items():
        if value>=2:rows.setdefault(y,[]).append(x)
    for xs in rows.values():xs.sort()
    reach=math.ceil(radius/grid.resolution)+8
    result={}
    for index,(x,y) in enumerate(free):
        squared=reach*reach
        for dy in range(-reach,reach+1):
            if dy*dy>=squared:continue
            xs=rows.get(y+dy)
            if not xs:continue
            at=bisect_left(xs,x)
            for i in (at-1,at):
                if 0<=i<len(xs):squared=min(squared,(xs[i]-x)**2+dy*dy)
        result[x,y]=max(0.,math.sqrt(squared)-math.sqrt(2))
        if index%512==0:check()
    return result


class StartBlocked(Halt):
    pass


class NoRoute(Halt):
    preserve_pose=True


class PlanningLimit(Halt):
    """A bounded map search may fall back to live guarded directed travel."""


class Planner:
    def __init__(self,grid,radius,check=lambda:None,*,allow_unknown_margin=False,escape_radius=None):
        self.grid=grid;self.radius=radius;self.clearance={};self.check=check
        check()
        free={c for c,v in grid.cells.items() if v<0}
        self.mapped_clearance=_clearance(free,check)
        self.clearance=(_obstacle_clearance(grid,free,radius,check)
                        if allow_unknown_margin else self.mapped_clearance)
        self.safe={c for c,d in self.clearance.items() if d*grid.resolution>=radius}
        self.uncertain={c for c in self.safe if self.mapped_clearance[c]*grid.resolution<radius or c in grid.inferred_free}
        self.escape_radius=radius if escape_radius is None else escape_radius
        self.escape=[]

    def _prepare_start(self,a):
        """Exit excess safety inflation, without entering a physical obstacle."""
        self.escape=[]
        if a in self.safe:return True
        minimum=self.escape_radius/self.grid.resolution
        if self.clearance.get(a,-1)<minimum:return False
        queue=deque([a]);parent={a:None};limit=min(30.,self.radius+2*self.grid.resolution)
        while queue:
            p=queue.popleft();self.check()
            if p in self.safe:
                path=[p]
                while parent[path[-1]] is not None:path.append(parent[path[-1]])
                self.escape=list(reversed(path));return True
            for dx,dy in NEIGHBORS:
                q=p[0]+dx,p[1]+dy
                if q in parent or math.dist(a,q)*self.grid.resolution>limit:continue
                # Clearance must not decrease while escaping. No wall crossing,
                # unmapped center cells or diagonal corner cutting is allowed.
                if self.clearance.get(q,-1)<max(minimum,self.clearance[p]-.01):continue
                if dx and dy and min(self.clearance.get((p[0]+dx,p[1]),-1),self.clearance.get((p[0],p[1]+dy),-1))<minimum:continue
                parent[q]=p;queue.append(q)
        return False

    def neighbors(self,p):
        if p in self.escape and p not in self.safe:
            i=self.escape.index(p)
            if i+1<len(self.escape):yield self.escape[i+1],2*math.dist(p,self.escape[i+1])
            return
        for dx,dy in NEIGHBORS:
            q=p[0]+dx,p[1]+dy
            if q not in self.safe:continue
            if dx and dy and ((p[0]+dx,p[1]) not in self.safe or (p[0],p[1]+dy) not in self.safe):continue
            yield q,math.hypot(dx,dy)*(1+1/max(1,self.clearance[q])+(1 if q in self.uncertain else 0))

    def route(self,start,goal,*,snap_cm=0):
        a=self.grid.cell(*start);b=self.grid.cell(*goal)
        if not self._prepare_start(a):
            raise StartBlocked('Refreshing the local map around Robbi’s position is needed.')
        targets={b} if b in self.safe else set()
        if not targets and snap_cm:
            n=math.ceil(snap_cm/self.grid.resolution)
            targets={(b[0]+dx,b[1]+dy) for dx in range(-n,n+1) for dy in range(-n,n+1)
                     if (b[0]+dx,b[1]+dy) in self.safe and math.dist(self.grid.world((b[0]+dx,b[1]+dy)),goal)<=snap_cm}
        if not targets:raise NoRoute('The connection to this destination is not mapped yet.')
        exact_goal=targets=={b}
        frontier=[(0,0,a)];cost={a:0};parent={};visited=0;deadline=time.monotonic()+3
        while frontier:
            _,g,p=heapq.heappop(frontier)
            if g!=cost[p]:continue
            visited+=1
            if visited%256==0:
                self.check()
                if visited>60000 or time.monotonic()>deadline:raise PlanningLimit('Route search reached its compute limit. Choose a nearer point.')
            if p in targets:
                path=[p]
                while path[-1]!=a:path.append(parent[path[-1]])
                return self.simplify(list(reversed(path)))
            for q,step in self.neighbors(p):
                new=g+step
                if new<cost.get(q,float('inf')):
                    cost[q]=new;parent[q]=p;dx=abs(q[0]-b[0]);dy=abs(q[1]-b[1])
                    heuristic=max(dx,dy)+(math.sqrt(2)-1)*min(dx,dy) if exact_goal else 0
                    heapq.heappush(frontier,(new+heuristic,new,q))
        raise NoRoute('The current map has no connected path to this destination.')

    def visible(self,a,b):
        previous=None
        escaping=a in self.escape
        permitted=self.safe|set(self.escape) if escaping else self.safe
        for p in line(a,b):
            if p not in permitted:return False
            if previous and p[0]!=previous[0] and p[1]!=previous[1]:
                if escaping:
                    minimum=self.escape_radius/self.grid.resolution
                    if min(self.clearance.get((p[0],previous[1]),-1),self.clearance.get((previous[0],p[1]),-1))<minimum:return False
                elif (p[0],previous[1]) not in self.safe or (previous[0],p[1]) not in self.safe:return False
            if previous and previous not in self.safe and self.clearance[p]<self.clearance[previous]-.01:return False
            previous=p
        return True

    def simplify(self,path):
        result=[path[0]];i=0
        while i<len(path)-1:
            j=i+1
            while j+1<len(path) and self.visible(path[i],path[j+1]):j+=1
            result.append(path[j]);i=j
        return [self.grid.world(p) for p in result]

    def frontiers(self,start,visited=(),blocked=()):
        origin=self.grid.cell(*start)
        if not self._prepare_start(origin):return []
        queue=deque([origin]);distance={origin:0};candidates=[]
        edge=math.ceil(self.radius/self.grid.resolution)+2
        while queue:
            p=queue.popleft()
            if len(distance)%512==0:self.check()
            if min(self.clearance[p],self.mapped_clearance[p])<=edge and distance[p]*self.grid.resolution>=20:
                def opening(dx,dy):
                    target=p[0]+dx*edge,p[1]+dy*edge
                    if target in self.grid.cells:return False
                    # Unknown behind a known wall is not an exploration opening.
                    # Check physical clearance along the sightline as well as
                    # endpoints, including gaps between sparse wall samples.
                    for q in line(p,target):
                        if self.grid.cells.get(q,0)>=2:return False
                        if q in self.clearance and self.clearance[q]*self.grid.resolution<self.escape_radius:return False
                    return True
                unknown=sum(opening(dx,dy) for dx,dy in NEIGHBORS)
                if unknown:
                    x,y=self.grid.world(p)
                    repeats=sum(math.hypot(x-v[0],y-v[1])<30 for v in visited[-100:])
                    if not any(math.hypot(x-v[0],y-v[1])<30 for v in blocked):
                        score=unknown*12+min(distance[p]*self.grid.resolution,200)*.15-repeats*60
                        candidates.append((score,(x,y)))
            for q,_ in self.neighbors(p):
                if q not in distance:distance[q]=distance[p]+1;queue.append(q)
        ranked=[]
        for score,p in sorted(candidates,reverse=True):
            if not any(math.dist(p,q)<30 for _,q in ranked):ranked.append((score,p))
            if len(ranked)>=24:break
        return ranked
