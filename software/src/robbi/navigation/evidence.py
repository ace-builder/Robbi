"""Independent-sweep empty-space confirmation, using measured origins/bearings."""
from dataclasses import replace
import math


def prefer_ray(new,old):
    priority={'measured':2,'inferred_empty':1,'empty':0}
    a,b=priority.get(new.source,0),priority.get(old.source,0)
    return a>b or a==b and new.distance_cm<old.distance_cm


def confirmed_empty(rays,clearance_cm=200.):
    """Raw empty observations stay non-geometric until another sweep agrees."""
    measured=[r for r in rays if r.source!='empty']
    if clearance_cm<=0:return measured
    candidates={}
    for r in rays:
        if r.source=='empty' and r.sweep_id:
            key=round(r.angle_deg%360/10)%36
            candidates.setdefault(key,{}).setdefault(r.sweep_id,[]).append(r)
    result=list(measured)
    for key,sweeps in candidates.items():
        # A real nearby return in this capture always defeats the empty inference.
        if any(r.hit and r.distance_cm<=clearance_cm and
               abs((r.angle_deg-key*10+180)%360-180)<=10 for r in measured):continue
        if len(sweeps)<2:continue
        rows=[r for group in sweeps.values() for r in group]
        for r in rows:
            if any(other.sweep_id!=r.sweep_id and math.hypot(other.x_cm-r.x_cm,other.y_cm-r.y_cm)<=10
                   and abs((other.angle_deg-r.angle_deg+180)%360-180)<=10 for other in rows):
                result.append(replace(r,distance_cm=clearance_cm,hit=False,source='inferred_empty'))
    return result
