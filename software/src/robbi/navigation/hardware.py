"""Adapt cached control/range samples to deskewed scans. No direct GPIO/I2C."""
from collections import deque
import logging
import math
import threading
import time
import uuid
from dataclasses import replace

from ..orchestration import intent_scope
from .model import Halt, Pose, Ray, angle
from .recovery import RecoveryMotion, SensorWait,RouteWait
from .evidence import prefer_ray

LOG = logging.getLogger(__name__)


def usable_scan_turn(spec, receipt):
    """Scan rays use measured angles; a settled endpoint need not be exact.

    Only a bounded final accuracy miss qualifies. Never wrap a full rotation
    modulo 360: no movement is not a successful 360-degree scan.
    """
    if (spec.get('mode') != 'turn' or receipt.get('reason') != 'turn_accuracy_not_reached'
            or receipt.get('control') != 'settled_turn'):
        return False
    heading = receipt.get('heading', {})
    actual, requested = heading.get('heading_deg'), spec.get('angle_deg')
    if heading.get('source') != 'imu_encoder' or any(
            type(v) not in (int, float) or not math.isfinite(v) for v in (actual, requested)):
        return False
    # Gap-filling rays also use the measured bearing, not the requested bearing.
    # The final coverage check decides whether another sweep is necessary.
    return abs(actual-requested) <= 12.


def interpolate(history, timestamp):
    """Interpolate only bracketed samples; never attach old range to new yaw."""
    # Range timestamps normally belong to the newest few control samples.
    # Walk backwards without copying the complete history for every ToF return.
    samples = iter(reversed(history))
    newer = next(samples, None)
    for older in samples:
        ta, a = older; tb, b = newer
        if ta <= timestamp <= tb and tb-ta <= .2:
            f = (timestamp-ta)/max(1e-9, tb-ta)
            return Pose(a.x_cm+(b.x_cm-a.x_cm)*f, a.y_cm+(b.y_cm-a.y_cm)*f,
                        angle(a.theta_deg+angle(b.theta_deg-a.theta_deg)*f))
        newer = older
    return None


class Hardware(RecoveryMotion):
    @staticmethod
    def _recovery_now():return time.monotonic()

    def __init__(self, controller):
        self.controller = controller
        self.last_scan_metrics = {}
        self.last_motion_metrics = {}
        self._range_lock=threading.Lock()
        self._range_context=None
        setter=getattr(controller.motion,'set_navigation_distance_getter',None)
        if callable(setter):setter(self._motion_range_cm)
        setter=getattr(controller.motion,'set_navigation_unknown_getter',None)
        if callable(setter):setter(self._fresh_unknown_range)

    def _fresh_unknown_range(self):
        try:
            sample=self.range_sample(allow_no_return=True,quiet=True)
            return sample.get('range_state')=='no_return' and sample.get('distance_cm') is None
        except Halt:return False

    def _motion_range_cm(self):
        # The motor worker and navigation must apply the same missing-sample
        # policy. This callback only reads cached sensor/encoder evidence.
        try:return self.range_sample(allow_no_return=True,allow_recent=True,quiet=True)['distance_cm']
        except Halt:return None

    def _recent_range(self,sample):
        """Bound a short missing-echo run by time, samples AND measured travel."""
        now=time.monotonic()
        with self._range_lock:
            context=self._range_context
            if not context:return None
            valid=type(sample.get('distance_cm')) in (int,float) and math.isfinite(sample['distance_cm']) and sample['distance_cm']>0
            if valid:
                previous=context.get('valid')
                if previous and previous[0]['last_read_ts']>=sample['last_read_ts']:return None
                context.pop('valid',None)
                # Anchor at/before the exposure, so coasting since measurement
                # is subtracted rather than treating the old distance as new.
                exposure=sample.get('sample_time',sample['last_read_ts']-sample.get('timing_budget_ms',0)/1000)
                exposure-=sample.get('timing_budget_ms',0)/2000+sample.get('timing_uncertainty_ms',0)/1000
                anchor=None
                if exposure<=context['started']:
                    anchor={'distance_cm':0.,'heading_deg':0.}
                else:
                    reader=getattr(self.controller.motion,'navigation_samples',None)
                    if callable(reader):
                        rows=reader(max(context['started'],exposure-.15)-1e-6)
                        anchor=next((row for row in reversed(rows) if row['intent_id']==context['intent_id'] and row['timestamp']<=exposure),None)
                if anchor is not None:
                    context['valid']=(dict(sample),dict(anchor),exposure)
                return None
            previous=context.get('valid')
            if not previous:return None
            feedback=self.controller.motion.navigation_feedback()
            if not previous or sample.get('range_state')!='no_return' or not feedback:return None
            measured,anchor,exposure=previous
            gap=sample.get('sample_count',0)-measured.get('sample_count',0)
            if (feedback.get('intent_id')!=context['intent_id'] or not 0<=now-feedback['timestamp']<=.06
                    or not 0<=now-exposure<=.15 or not 1<=gap<=3):return None
            traveled=feedback['distance_cm']-anchor['distance_cm']
            if not -.1<=traveled<=5 or abs(angle(feedback['heading_deg']-anchor['heading_deg']))>3:return None
            # This is collision-check evidence only; it never enters the map.
            return {**sample,'distance_cm':max(0.,measured['distance_cm']-max(0,traveled)-2),
                    'held_range':True,'held_age_s':now-exposure,'skipped_returns':gap}

    def ready(self, *, allow_no_return=False,allow_encoder_fallback=False):
        c = self.controller
        allowed, reason = c._can_run_slam_motion()
        if not allowed:
            if allow_encoder_fallback and reason!='filming_mode':
                raise RouteWait(str(reason),'Waiting for the current robot activity to finish; the route remains active.')
            raise Halt('Leave filming mode before navigating.' if reason == 'filming_mode' else 'Navigation is waiting: '+reason)
        status = c.motion.status()
        if not status.get('available') or not status.get('encoder_enabled'):
            if allow_encoder_fallback:
                raise RouteWait('motors_unavailable','Waiting for motors and encoder feedback; the route remains active.')
            raise Halt('Navigation needs enabled motors and calibrated wheel encoders.')
        if not allow_encoder_fallback and (not status.get('heading', {}).get('available') or not status.get('heading', {}).get('enabled')):
            raise SensorWait('imu_heading_unavailable','Waiting for calibrated IMU heading feedback.')
        if getattr(c.motion, 'handling_interlock', lambda: False)():
            raise Halt('Put Robbi upright on a level floor before navigating.')
        return self.range_sample(allow_no_return=allow_no_return)

    def range_sample(self, *, allow_no_return=False, allow_recent=False,quiet=False):
        sample = self.controller.tof.status()
        distance = sample.get('distance_cm')
        stamp = sample.get('last_read_ts')
        valid_range = type(distance) in (int,float) and math.isfinite(distance) and distance>0
        state=sample.get('range_state')
        age=time.monotonic()-stamp if type(stamp) in (int,float) and math.isfinite(stamp) else None
        if sample.get('enabled') is False or state=='disabled':
            reason='Front ToF is disabled. Enable it before navigating.'
        elif not sample.get('available') or state=='unavailable':
            reason='Front ToF is unavailable. '+str(sample.get('last_error') or 'Check the sensor connection.')[:160]
        elif state in ('waiting','stale') or age is None or not 0<=age<=.35:
            detail=f'{age*1000:.0f} ms old; limit 350 ms' if age is not None and age>=0 else 'no valid timestamp'
            reason=f'Front ToF readings are stale ({detail}). Motion stopped.'
        elif distance is None and state=='no_return':
            if allow_no_return:return sample
            if allow_recent:
                held=self._recent_range(sample)
                if held is not None:return held
            reason='Front ToF is updating but has no usable distance return. Motion stopped; check the surface or sensor range.'
        elif not valid_range:
            reason='Front ToF returned an invalid distance. Motion stopped.'
        else:
            if allow_recent:self._recent_range(sample)
            return sample
        if not quiet:LOG.debug('%s [state=%s age_s=%s sample=%s distance=%s driver=%s]',
                    reason,state,age,sample.get('sample_count'),distance,sample.get('last_error',''))
        if sample.get('enabled') is not False and state!='disabled':
            raise SensorWait('front_range_unavailable',reason)
        raise Halt(reason)

    def wait_for_range(self, intent, check, *, allow_unknown=False):
        """Reacquire a missing echo while stationary; never reuse an old range."""
        if self.controller.motion.status().get('running'):
            raise Halt('Stop movement before waiting for a new front distance reading.')
        deadline=time.monotonic()+.6
        while True:
            check()
            sample=self.range_sample(allow_no_return=True)
            if allow_unknown and sample.get('range_state')=='no_return':return sample
            if sample['distance_cm'] is not None:return sample
            if time.monotonic()>=deadline:
                return self.range_sample() # Report the actual missing-return fault.
            intent.wait(.01)
            check()

    def _move_once(self, spec, pose, intent, settings, check, publish, *, collect=False, scan_turn=False, route_turn=False, transit=False):
        """Integrate actual wheel-mean travel + the controller's fused yaw."""
        # A no-return exposure is unknown space, not a stale/disconnected sensor.
        # Rotation stays inside the mapped footprint; forward travel requires a
        # real distance and may wait briefly while the motors are still stopped.
        turning=spec.get('mode')=='turn'
        initial = self.ready(allow_no_return=True,allow_encoder_fallback=transit); check()
        if not (collect or turning):initial=self.wait_for_range(intent,check,allow_unknown=True)
        if spec.get('mode')=='distance' and initial['distance_cm'] is None:
            self.last_motion_metrics['unknown_range']=True
            if not transit:
                spec=dict(spec,distance_cm=math.copysign(min(abs(spec['distance_cm']),settings.unknown_step_cm),spec['distance_cm']))
        clearance = settings.robot_radius_cm + settings.clearance_cm - settings.sensor_forward_cm
        if initial['distance_cm'] is not None and initial['distance_cm'] < clearance:
            if transit:raise RouteWait('obstacle_close','Waiting for the obstacle ahead to clear; the route remains active.')
            raise Halt('An obstacle is too close. Motion stopped; clear space around Robbi.')
        origin = pose
        began = time.monotonic()
        history = deque([(began, pose)], maxlen=256)
        last_distance = last_heading = 0.
        stamp = began
        last_range = None
        unknown_start=None
        partial=False
        sequence = self.controller.tof.status().get('sample_count',0)
        pending = deque()
        metrics = {'received_samples':0,'invalid_returns':0,'unmatched_timestamps':0,'buffer_dropped':0}
        rays = []
        motion = self.controller.motion
        with self._range_lock:
            self._range_context={'intent_id':intent.id,'started':began} if not (turning or collect) else None
        if not (turning or collect):
            self._recent_range(initial)
        try:
            with intent_scope(intent):
                options={'allow_encoder_fallback':True} if transit else {}
                future = motion.drive({**spec, 'require_encoders': True}, cancel_event=intent,**options)
        except Exception as error:
            with self._range_lock:self._range_context=None
            if transit and isinstance(error,(RuntimeError,OSError)):
                raise RouteWait('motion_unavailable','Waiting for the motor driver to become available; the route remains active.') from error
            raise
        if future is None:
            with self._range_lock:self._range_context=None
            if transit:raise RouteWait('motion_unavailable','Waiting for the motor driver to start; the route remains active.')
            raise Halt('Motion could not start.')

        def integrate(sample):
            nonlocal pose, stamp, last_distance, last_heading
            if not sample or sample.get('intent_id') != intent.id or sample['timestamp'] <= stamp:
                return
            pose = pose.moved(sample['distance_cm']-last_distance, sample['heading_deg']-last_heading)
            last_distance, last_heading, stamp = sample['distance_cm'], sample['heading_deg'], sample['timestamp']
            history.append((stamp, pose))
            publish(pose)

        def read_odometry():
            batch = getattr(motion,'navigation_samples',None)
            for sample in batch(stamp) if callable(batch) else [motion.navigation_feedback()]:
                integrate(sample)

        def finish_odometry(result):
            segment=(result.get('segments') or [result])[-1] if isinstance(result,dict) else {}
            final=segment.get('final',{});heading=segment.get('heading',{})
            settled_heading=heading.get('heading_deg',last_heading)
            if (all(type(final.get(k)) in (int,float) and math.isfinite(final[k]) for k in ('left','right'))
                    and type(settled_heading) in (int,float) and math.isfinite(settled_heading)):
                integrate({'intent_id':intent.id,'timestamp':time.monotonic(),
                    'distance_cm':(final['left']+final['right'])*motion.cm_per_count()/2,
                    'heading_deg':settled_heading})
            return segment

        def collect_ranges(latest):
            nonlocal sequence,last_range
            reader = getattr(self.controller.tof,'samples_since',None)
            if callable(reader):
                batch = reader(sequence)
                metrics['buffer_dropped'] += batch['dropped']
                new = batch['samples']
                if new:sequence = new[-1]['sample_count']
            else:
                new = [latest] if latest['last_read_ts'] != last_range else []
                last_range = latest['last_read_ts']
            for sample in new:
                metrics['received_samples'] += 1
                delay = sample.get('timing_budget_ms',latest.get('timing_budget_ms',0))/2000 + .5/max(1,latest.get('poll_hz',10))
                timestamp = sample.get('sample_time',sample['last_read_ts']-min(.25,delay))
                if len(pending)>=512:
                    pending.popleft();metrics['buffer_dropped']+=1
                pending.append((timestamp,sample))
            while pending and pending[0][0] <= history[-1][0]:
                timestamp,sample = pending.popleft()
                measured = interpolate(history,timestamp)
                if measured is None:
                    metrics['unmatched_timestamps'] += 1
                    continue
                distance = sample['distance_cm']
                empty=(distance is None and sample.get('available') is not False and sample.get('empty_candidate') is True)
                if not empty and (type(distance) not in (int,float) or not math.isfinite(distance) or distance < settings.min_range_cm):
                    metrics['invalid_returns'] += 1
                    continue
                r = math.radians(measured.theta_deg)
                x = measured.x_cm+math.cos(r)*settings.sensor_forward_cm-math.sin(r)*settings.sensor_left_cm
                y = measured.y_cm+math.sin(r)*settings.sensor_forward_cm+math.cos(r)*settings.sensor_left_cm
                a = math.radians(-origin.theta_deg);dx=x-origin.x_cm;dy=y-origin.y_cm
                rays.append(Ray(math.cos(a)*dx-math.sin(a)*dy,math.sin(a)*dx+math.cos(a)*dy,
                    angle(measured.theta_deg-origin.theta_deg+settings.sensor_yaw_deg),
                    0. if empty else min(distance,settings.max_range_cm),False if empty else distance<settings.max_range_cm,
                    'empty' if empty else 'measured'))
                if len(rays)>5000:raise Halt('Scan exceeded its sample budget.')

        try:
            while not future.done():
                check()
                read_odometry()
                if time.monotonic()-began>.5 and time.monotonic()-stamp>.25:
                    if transit:raise RouteWait('motion_feedback_stale','Waiting for fresh movement feedback; the route remains active.')
                    raise Halt('Motion feedback stopped updating. Position must be checked before continuing.')
                sample = self.range_sample(allow_no_return=True,allow_recent=not (collect or turning))
                if not (collect or turning) and sample['distance_cm'] is None:
                    self.last_motion_metrics['unknown_range']=True
                    if unknown_start is None:unknown_start=last_distance
                    if not transit and abs(last_distance-unknown_start)>=settings.unknown_step_cm:
                        partial=True;motion.stop(owner=intent)
                        while not future.done():time.sleep(.01)
                        read_odometry();break
                else:unknown_start=None
                # Also guard turns and runtime configurations with obstacle checking disabled.
                if sample['distance_cm'] is not None and sample['distance_cm'] < clearance:
                    if transit:raise RouteWait('obstacle_close','Waiting for the obstacle ahead to clear; the route remains active.')
                    raise Halt('An obstacle is too close. Motion stopped; clear space around Robbi.')
                if collect:collect_ranges(sample)
                if time.monotonic()-began > 90:
                    raise Halt('Movement exceeded its time budget.')
                intent.wait(.01)
            read_odometry()
            try:result = future.result()
            except (RuntimeError,OSError) as error:
                if transit:raise RouteWait('motion_unavailable','Waiting for the motor driver to recover; the route remains active.') from error
                raise
            # Successful receipts contain the final settled physical counts.
            segment = finish_odometry(result)
            final = segment.get('final', {})
            heading = segment.get('heading', {})
            if collect:
                collect_ranges(self.range_sample(allow_no_return=True))
                self.last_scan_metrics = {**metrics,'raw_points':len(rays),
                    'elapsed_s':round(time.monotonic()-began,3),
                    'measured_range_hz':self.controller.tof.status().get('measured_hz'),
                    'control_hz':segment.get('control_hz')}
            # A settled accuracy miss can be corrected from the measured pose.
            # Route following still requires <=5 degrees before forward travel.
            usable = (collect or scan_turn or route_turn) and usable_scan_turn(spec, segment)
            if usable:
                LOG.info('Using measured turn %.2f degrees for requested %.2f; checking coverage or route alignment',
                         heading['heading_deg'], spec['angle_deg'])
            reason=segment.get('reason') or (result or {}).get('reason') if isinstance(result,dict) else ''
            partial=partial and isinstance(result,dict) and (reason=='stopped' or not reason and result.get('ok'))
            if not partial and reason in {'range_unavailable','imu_heading_unavailable','imu_heading_stale','imu_heading_reset','imu_encoder_disagreement'}:
                raise SensorWait(str(reason),'Waiting for fresh motion sensor readings.')
            if not partial and not usable and (not isinstance(result, dict) or not result.get('ok')):
                if transit and reason in {'turn_accuracy_not_reached','turn_not_settled','turn_excessive_error'}:
                    self.last_motion_metrics.update(accuracy_reached=False,warning=str(reason))
                    check();return pose,rays
                if transit and reason not in {'stopped','cancelled','canceled'}:
                    self.last_motion_metrics.update(partial=True,last_issue=str(reason or 'motion_unavailable'))
                    raise RouteWait(str(reason or 'motion_unavailable'),'Waiting before resuming from the measured position ('+str(reason or 'motion unavailable')+').')
                raise Halt('Movement stopped: '+str(segment.get('reason') or (result or {}).get('reason') or 'no verified completion'))
            self.last_motion_metrics['partial']=partial
            check()
            return pose, rays
        except SensorWait as fault:
            if not future.done():
                motion.stop(owner=intent)
                while not future.done():time.sleep(.01)
            read_odometry()
            try:settled=future.result()
            except (RuntimeError,OSError):
                if not transit:raise
                settled={}
            finish_odometry(settled)
            fault.pose,fault.distance,fault.rotation,fault.rays=pose,last_distance,last_heading,rays
            raise
        finally:
            with self._range_lock:self._range_context=None
            if not future.done():
                motion.stop(owner=intent)
                # Hold navigation ownership until the motor worker has cleaned up.
                while not future.done():
                    time.sleep(.025)

    def _stationary_scan_ray(self, pose, origin, intent, settings, check):
        """Wait for a real exposure entirely after the motor worker settled.

        A newer read timestamp alone can still describe a range integrated while
        turning. Never attach that reading to a stationary heading.
        """
        began = time.monotonic()
        sample = self.range_sample(allow_no_return=True)
        previous = (sample.get('sample_count'), sample['last_read_ts'])
        budget = sample.get('timing_budget_ms', 0)/1000
        period = max(budget, sample.get('intermeasurement_ms', 0)/1000,
                     1/max(1, sample.get('measured_hz') or sample.get('poll_hz', 10)))
        deadline = began + min(2., max(.6, 3*period+budget))
        while time.monotonic() < deadline:
            check()
            intent.wait(.01)
            check()
            sample = self.range_sample(allow_no_return=True)
            identity = (sample.get('sample_count'), sample['last_read_ts'])
            if identity == previous:
                continue
            previous = identity
            self.last_scan_metrics['recovery_received_samples'] += 1
            budget = sample.get('timing_budget_ms', 0)/1000
            stamp = sample.get('sample_time', sample['last_read_ts']-budget/2-.5/max(1,sample.get('poll_hz',10)))
            uncertainty = sample.get('timing_uncertainty_ms', 0)/1000
            if type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp-budget/2-uncertainty < began:
                continue
            distance = sample['distance_cm']
            if type(distance) not in (int,float) or not math.isfinite(distance) or distance < settings.min_range_cm:
                self.last_scan_metrics['recovery_invalid_returns'] += 1
                continue
            r=math.radians(pose.theta_deg);a=math.radians(-origin.theta_deg)
            dx=pose.x_cm-origin.x_cm+math.cos(r)*settings.sensor_forward_cm-math.sin(r)*settings.sensor_left_cm
            dy=pose.y_cm-origin.y_cm+math.sin(r)*settings.sensor_forward_cm+math.cos(r)*settings.sensor_left_cm
            return Ray(math.cos(a)*dx-math.sin(a)*dy,math.sin(a)*dx+math.cos(a)*dy,
                       angle(pose.theta_deg-origin.theta_deg+settings.sensor_yaw_deg),
                       min(distance,settings.max_range_cm),distance<settings.max_range_cm)
        check()
        return None

    def scan(self, pose, intent, settings, check, publish, *, allow_sparse=False,transit=False):
        self.last_scan_metrics = {}
        started = time.monotonic()
        sample = self.controller.tof.status()
        rate = settings.scan_rate_deg_s
        hz = sample.get('measured_hz')
        valid = sample.get('valid_return_percent',100)
        if (type(hz) in (int,float) and math.isfinite(hz) and hz>0
                and type(valid) in (int,float) and math.isfinite(valid)):
            # Aim for <=4 degrees between usable returns; configured speed is
            # always a ceiling. Missing returns still remain unknown space.
            rate = min(rate,max(10.,hz*max(0,min(100,valid))/100*4))
        end, rays = self.move({'mode':'turn', 'angle_deg':360, 'speed':settings.scan_speed,
                              'turn_rate_deg_s':rate},
                              pose, intent, settings, check, publish, collect=True,transit=transit)
        sweep=uuid.uuid4().hex
        rays=[replace(r,sweep_id=sweep) for r in rays]
        # Deduplicate angular bins: repeated returns while settling add no coverage.
        bins = {}
        def insert(ray):
            key = round((ray.angle_deg % 360)/settings.scan_bin_deg) % round(360/settings.scan_bin_deg)
            if key not in bins or prefer_ray(ray,bins[key]):
                bins[key] = ray
        for ray in rays:insert(ray)

        def gaps():
            angles = sorted(ray.angle_deg % 360 for ray in bins.values())
            return sorted(((b-a,a) for a,b in zip(angles,angles[1:]+[angles[0]+360],strict=True)),
                          reverse=True) if angles else [(360.,0.)]

        def covered():return len(bins)>=30 and gaps()[0][0]<=20
        # Mapping/localization can use a partial sweep. Empty exposures do not
        # count as bearings and missing sectors remain unknown in the grid.
        def usable():return len(bins)>=24 and 360-gaps()[0][0]>=180

        self.last_scan_metrics.update(initial_points=len(bins),initial_largest_gap_deg=round(gaps()[0][0],2),
            fill_turns=0,recovery_points=0,recovery_received_samples=0,recovery_invalid_returns=0,
            target_rate_deg_s=round(rate,1),configured_rate_deg_s=settings.scan_rate_deg_s)
        def metrics(phase):
            self.last_scan_metrics.update(points=len(bins),largest_gap_deg=round(gaps()[0][0],2),
                observed_span_deg=round(360-gaps()[0][0],2),partial_coverage=not covered(),
                angular_support_percent=round(sum(min(10,size) for size,_ in gaps())/3.6,1),
                bin_deg=settings.scan_bin_deg,phase=phase,total_elapsed_s=round(time.monotonic()-started,3))
        metrics('coverage_check')
        # Full PWM is needed for breakaway torque. If a fast sweep undersamples
        # a sector, stop there and take a fresh range instead of reducing power.
        # Recompute the largest *measured* gap after each repair; a static list
        # can waste turns on already covered angles and miss the remaining hole.
        attempted = []
        for _ in range(0 if allow_sparse else settings.scan_max_fill_turns):
            if usable():break
            check()
            candidates = ((begin+size*fraction)%360 for size,begin in gaps()
                          if size>settings.scan_bin_deg for fraction in (.5,.33,.67))
            target = next((candidate for candidate in candidates if all(
                abs(angle(candidate-old))>max(2,settings.scan_bin_deg/2) for old in attempted)),None)
            if target is None:break
            attempted.append(target)
            self.last_scan_metrics['fill_turns'] = len(attempted)
            metrics('repairing_coverage')
            turn = angle(pose.theta_deg+target-settings.sensor_yaw_deg-end.theta_deg)
            if abs(turn)>2:
                end,_ = self.move({'mode':'turn','angle_deg':turn,'speed':min(settings.scan_speed,settings.turn_speed)},
                                  end,intent,settings,check,publish,scan_turn=True)
            ray = self._stationary_scan_ray(end,pose,intent,settings,check)
            if ray is not None:
                insert(ray);self.last_scan_metrics['recovery_points'] += 1
            metrics('coverage_check')
        if not usable() and not allow_sparse:
            metrics('insufficient')
            raise Halt(f'Scan has only {len(bins)} usable bearings across {360-gaps()[0][0]:.0f}°. '
                       'Not enough measured geometry to map reliably; missing returns were skipped.')
        metrics('complete' if covered() else 'partial')
        return end, list(bins.values())

    def heading_status(self):
        getter=getattr(getattr(self.controller,'imu',None),'heading_sample',None)
        sample=getter() if callable(getter) else None
        age=time.monotonic()-sample['sample_time'] if sample and type(sample.get('sample_time')) in (int,float) else None
        return {'source':'imu_encoder','fresh':bool(sample and sample.get('ready') and age is not None and 0<=age<=.15),
                'age_ms':round(age*1000,1) if age is not None else None}
