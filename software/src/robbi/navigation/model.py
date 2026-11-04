"""Geometry and validated settings. Distances are cm, angles are degrees."""
from dataclasses import asdict, dataclass
import math

from ..errors import ValidationError


def angle(value):
    return (value+180)%360-180


def finite(value, name, low, high):
    if type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=high:
        raise ValidationError(f'{name} must be between {low:g} and {high:g}')
    return float(value)


@dataclass
class Pose:
    x_cm: float=0.
    y_cm: float=0.
    theta_deg: float=0.
    sigma_cm: float=2.
    sigma_deg: float=2.

    def to_dict(self):return asdict(self)

    def moved(self,distance,rotation):
        # Exact integration of a constant-curvature increment, not commanded travel.
        a=math.radians(self.theta_deg);d=math.radians(rotation)
        if abs(d)<1e-8:dx,dy=distance*math.cos(a),distance*math.sin(a)
        else:dx,dy=distance/d*(math.sin(a+d)-math.sin(a)),-distance/d*(math.cos(a+d)-math.cos(a))
        return Pose(self.x_cm+dx,self.y_cm+dy,angle(self.theta_deg+rotation),
                    self.sigma_cm+abs(distance)*.035+abs(rotation)*.006,
                    self.sigma_deg+abs(rotation)*.025+abs(distance)*.008)


@dataclass(frozen=True)
class Ray:
    # Sensor origin and bearing relative to the start of a scan (deskewed).
    x_cm: float
    y_cm: float
    angle_deg: float
    distance_cm: float
    hit: bool=True
    source: str='measured'
    sweep_id: str=''

    def endpoint(self):
        a=math.radians(self.angle_deg)
        return self.x_cm+math.cos(a)*self.distance_cm,self.y_cm+math.sin(a)*self.distance_cm


@dataclass
class Settings:
    resolution_cm: float=5.
    robot_radius_cm: float=12.
    clearance_cm: float=4.
    sensor_forward_cm: float=4.
    sensor_left_cm: float=0.
    sensor_yaw_deg: float=0.
    max_range_cm: float=300.
    min_range_cm: float=5.
    leg_cm: float=200.
    segment_cm: float=30.
    drive_speed: float=100.
    turn_speed: float=100.
    scan_speed: float=100.
    scan_rate_deg_s: float=90.
    scan_bin_deg: float=1.
    scan_max_fill_turns: int=8
    rescan_sigma_cm: float=12.
    max_match_s: float=12.
    sensor_retry_count: int=2
    sensor_retry_delay_s: float=1.
    unknown_step_cm: float=10.
    empty_clearance_cm: float=200.

    @classmethod
    def parse(cls,data):
        if not isinstance(data,dict) or set(data)-set(cls.__dataclass_fields__):
            raise ValidationError('Unknown navigation settings')
        limits={'resolution_cm':(3,10),'robot_radius_cm':(8,25),'clearance_cm':(2,15),
            'sensor_forward_cm':(-20,20),'sensor_left_cm':(-20,20),'sensor_yaw_deg':(-180,180),
            'max_range_cm':(50,400),'min_range_cm':(2,20),'leg_cm':(20,200),'segment_cm':(5,50),
            'drive_speed':(25,100),'turn_speed':(25,100),'scan_speed':(25,100),
            'scan_rate_deg_s':(10,120),'scan_bin_deg':(.5,10),'scan_max_fill_turns':(0,12),
            'rescan_sigma_cm':(5,20),'max_match_s':(2,30),
            'sensor_retry_count':(0,3),'sensor_retry_delay_s':(.2,3),'unknown_step_cm':(2,30),
            'empty_clearance_cm':(0,200)}
        values=asdict(cls());values.update(data)
        values={k:finite(v,k,*limits[k]) for k,v in values.items()}
        if int(values['scan_max_fill_turns']) != values['scan_max_fill_turns']:
            raise ValidationError('Gap-fill turn count must be a whole number')
        values['scan_max_fill_turns']=int(values['scan_max_fill_turns'])
        if int(values['sensor_retry_count'])!=values['sensor_retry_count']:raise ValidationError('Retry count must be a whole number')
        values['sensor_retry_count']=int(values['sensor_retry_count'])
        if values['segment_cm']>values['leg_cm']:raise ValidationError('Segment must fit inside a scan leg')
        return cls(**values)

    def to_dict(self):return asdict(self)


class Halt(RuntimeError):
    """Expected interruption; retains a human-readable reason and map state."""
