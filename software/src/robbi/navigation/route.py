"""Directed travel: two long legs, bounded position checks, measured recovery."""
import math
import time

from .model import angle
from .planning import NoRoute,StartBlocked,PlanningLimit
from .recovery import LocalizationUncertain,RouteWait,SensorWait


class RouteRunner:
    RETRY_S=1.
    MAX_PRIMITIVE_CM=500.

    def __init__(self,service):
        self.service=service
        self.waits=0
        self.progress={'position_checks':0,'midpoint_checked':False,'arrival_checked':False,
                       'correction_attempted':False,'waiting_reason':'','warnings':0}
        service.route_progress=self.progress

    def _plan(self,goal):
        s=self.service
        planner=s._planner()
        try:route=planner.route(s._position(),goal,snap_cm=planner.radius+2*s.grid.resolution)
        except (NoRoute,StartBlocked,PlanningLimit):
            # Missing map connectivity is not a physical wall. The live front
            # range guard still stops the wheels if this direction is blocked.
            route=[s._position(),tuple(goal)]
            s.message='Following the destination with live ToF checks; map connectivity is uncertain.'
        s.route=[list(point) for point in route]
        return route

    def _wait(self,fault):
        s=self.service;s._check();self.waits+=1
        self.progress.update(waiting_reason=fault.code,warnings=self.progress['warnings']+1)
        s.phase='waiting';s.message=str(fault)
        # No busy loop or automatic replay of a completed primitive. Replanning
        # happens after a cancellable stationary delay, from the actual pose.
        until=time.monotonic()+min(5.,self.RETRY_S*2**min(3,self.waits-1))
        while time.monotonic()<until:
            s._check();s.intent.wait(min(.05,max(0.,until-time.monotonic())))
        s._check()

    def _follow(self,route,budget):
        s=self.service;began=s.travel;tolerance=max(3.,s.grid.resolution)
        for target in route[1:]:
            while math.dist(s._position(),target)>tolerance and s.travel-began<budget-.5:
                s._check()
                for _ in range(3):
                    dx,dy=target[0]-s.pose.x_cm,target[1]-s.pose.y_cm
                    turn=angle(math.degrees(math.atan2(dy,dx))-s.pose.theta_deg)
                    if abs(turn)<=5:break
                    s.message='Adjusting the measured heading toward the destination.'
                    s._move({'mode':'turn','angle_deg':turn,'speed':s.settings.turn_speed},transit=True)
                dx,dy=target[0]-s.pose.x_cm,target[1]-s.pose.y_cm
                if abs(angle(math.degrees(math.atan2(dy,dx))-s.pose.theta_deg))>5:
                    raise RouteWait('heading_alignment','Adjusting the measured heading before continuing the route.')
                length=min(math.hypot(dx,dy),self.MAX_PRIMITIVE_CM,budget-(s.travel-began))
                if length<1:return
                sample=s.hardware.wait_for_range(s.intent,s._check,allow_unknown=True)
                margin=s.settings.robot_radius_cm+s.settings.clearance_cm-s.settings.sensor_forward_cm
                distance=sample.get('distance_cm')
                if distance is not None:
                    length=min(length,max(0.,distance-margin-3.))
                    if length<1:
                        raise RouteWait('obstacle_close','Waiting for the obstacle ahead to clear; the route remains active.')
                s.message='Driving toward the selected point with live ToF checks.'
                moved=s._move({'mode':'distance','distance_cm':length,'speed':s.settings.drive_speed},transit=True)
                if moved<.5:raise RouteWait('no_progress','Waiting before retrying from the measured position.')
                self.waits=0;self.progress['waiting_reason']=''

    def _travel(self,goal,budget,*,planned=None):
        s=self.service;began=s.travel
        while math.dist(s._position(),goal)>max(6,s.grid.resolution*1.5) and s.travel-began<budget-.5:
            s._check();before=s.travel
            try:
                route=planned if planned is not None else self._plan(goal)
                planned=None
                self._follow(route,budget-(s.travel-began))
            except SensorWait as fault:self._wait(fault);continue
            if s.travel-before<.5:
                self._wait(RouteWait('route_alignment','Adjusting the route from the current encoder/IMU position.'))

    def _position_check(self,kind):
        s=self.service;s._check()
        self.progress['position_checks']+=1;self.progress[kind+'_checked']=True
        try:accepted=s._scan(best_effort=True)
        except (SensorWait,LocalizationUncertain):
            s._check()
            s.match={**(s.match or {}),'accepted':False,'mode':'odometry_only'}
            self.progress['warnings']+=1
            s.message='Position check was inconclusive; continuing with measured encoder/IMU progress.'
            return False
        if accepted is False:self.progress['warnings']+=1
        return accepted is not False

    def run(self,goal):
        s=self.service
        route=self._plan(goal);destination=tuple(route[-1])
        if math.dist(goal,destination)>s.grid.resolution:
            s.goal_adjustment={'requested':list(goal),'destination':list(destination)}
        self.progress.update(destination=list(destination))
        length=sum(math.dist(a,b) for a,b in zip(route,route[1:]))
        if length>s.settings.leg_cm:
            self._travel(destination,length/2,planned=route)
            if math.dist(s._position(),destination)>max(6,s.grid.resolution*1.5):
                self._position_check('midpoint')
            route=None
        self._travel(destination,float('inf'),planned=route)
        accepted=self._position_check('arrival')
        remaining=math.dist(s._position(),destination)
        if accepted and remaining>max(10,s.grid.resolution*2):
            self.progress['correction_attempted']=True
            # One modest correction, with no repeated scan/correction cycle.
            self._travel(destination,min(100.,remaining+10.))
        remaining=math.dist(s._position(),destination)
        self.progress.update(waiting_reason='',remaining_cm=round(remaining,1))
        if remaining>max(10,s.grid.resolution*2):
            s.message=f'Travel finished; the current position estimate is {remaining:.0f} cm from the destination.'
        elif s.goal_adjustment:s.message='Arrived at the clear stopping point near the selected destination.'
        elif not accepted:s.message='Arrived by encoder/IMU estimate; the arrival scan was inconclusive.'
        else:s.message='Arrived near the selected point; position checked.'
        s.route=[];s.preview=None
