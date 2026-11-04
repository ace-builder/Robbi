"""Bounded sensor recovery. Resume measured remaining travel, never replay it."""
import logging
import math

from .model import Halt, Ray, angle

LOG=logging.getLogger(__name__)


class SensorWait(Halt):
    def __init__(self,code,message):
        super().__init__(message)
        self.code=code
        self.pose=None
        self.distance=self.rotation=0.
        self.rays=[]


class RouteWait(SensorWait):
    """Directed travel can pause and replan from its measured position."""


class LocalizationUncertain(Halt):
    """A completed scan has no reliable map match; its odometry still exists."""


def transform_rays(rays,source,target):
    a=math.radians(-target.theta_deg)
    rotation=math.radians(angle(source.theta_deg-target.theta_deg))
    c,s=math.cos(rotation),math.sin(rotation)
    dx,dy=source.x_cm-target.x_cm,source.y_cm-target.y_cm
    ox,oy=math.cos(a)*dx-math.sin(a)*dy,math.sin(a)*dx+math.cos(a)*dy
    return [Ray(ox+c*r.x_cm-s*r.y_cm,oy+s*r.x_cm+c*r.y_cm,
                angle(r.angle_deg+math.degrees(rotation)),r.distance_cm,r.hit,r.source,r.sweep_id) for r in rays]


class RecoveryMotion:
    def _recovery_message(self,message):
        LOG.info('%s',message)
        callback=getattr(self,'on_recovery',None)
        if callback:callback(message)

    def move(self,spec,pose,intent,settings,check,publish,**options):
        original=pose;remaining=dict(spec);rays=[];began=self._recovery_now()
        def checked():
            check()
            if self._recovery_now()-began>=90:
                if options.get('transit'):raise RouteWait('motion_timeout','Waiting before resuming the measured remaining route.')
                raise Halt('Movement exceeded its time budget.')
        self.last_motion_metrics={'retries':0,'unknown_range':False,'partial':False}
        for attempt in range(settings.sensor_retry_count+1):
            checked()
            try:
                end,points=self._move_once(remaining,pose,intent,settings,checked,publish,**options)
                rays.extend(transform_rays(points,pose,original))
                if attempt:self._recovery_message('Sensor recovered; movement continued from the measured position.')
                return end,rays
            except SensorWait as fault:
                if fault.pose is not None:
                    rays.extend(transform_rays(fault.rays,pose,original));pose=fault.pose
                    checked()
                    if spec.get('mode')=='distance':remaining['distance_cm']-=fault.distance
                    elif spec.get('mode')=='turn':remaining['angle_deg']-=fault.rotation
                    else:raise Halt('Sensor interrupted this movement; its remaining path needs replanning.') from None
                    # A stop can finish the requested travel while braking.
                    if spec.get('mode')=='distance' and remaining['distance_cm']*spec['distance_cm']<=0:
                        return pose,rays
                    if spec.get('mode')=='turn' and abs(remaining['angle_deg'])<=2:
                        return pose,rays
                checked()
                if options.get('transit') and isinstance(fault,RouteWait):raise
                if attempt>=settings.sensor_retry_count or self._recovery_now()-began>=85:
                    if options.get('transit'):
                        retry=RouteWait(fault.code,'Waiting for motion sensors to recover; the route remains active.')
                        retry.pose=pose
                        raise retry from None
                    raise Halt(f'Sensor is still unavailable after {attempt} automatic retries. Movement paused; retry when the sensor reconnects.') from None
                self.last_motion_metrics.update(retries=attempt+1,last_issue=fault.code)
                self._recovery_message(f'Waiting {settings.sensor_retry_delay_s:g}s for fresh sensor readings; retry {attempt+1}/{settings.sensor_retry_count}.')
                until=self._recovery_now()+settings.sensor_retry_delay_s
                while self._recovery_now()<until:
                    checked();intent.wait(min(.02,max(0,until-self._recovery_now())))
                checked()
        raise Halt('Sensor recovery ended.')
