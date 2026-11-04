"""Single-worker mapping and navigation, with explicit localization state."""
import json
import logging
import math
from pathlib import Path
import threading
import time
import uuid

from ..errors import BusyError, ValidationError
from ..orchestration import Intent, MANUAL, intent_scope
from ..storage import write_json
from .grid import Grid, line
from .hardware import Hardware
from .recovery import SensorWait,LocalizationUncertain
from .route import RouteRunner
from .localization import Matcher
from .model import Halt, Pose, Ray, Settings, angle, finite
from .planning import Planner, StartBlocked, NoRoute
from .storage import MapStore
from .evidence import confirmed_empty,prefer_ray

LOG = logging.getLogger(__name__)


class NavigationService:
    def __init__(self, controller, directory, *, hardware=None):
        self.controller = controller
        self.hardware = hardware or Hardware(controller)
        self.hardware.on_recovery=lambda message:setattr(self,'message',message)
        self.directory = Path(directory)
        self.store = MapStore(self.directory / 'maps')
        self.settings = Settings()
        self.lock = threading.RLock()
        self.thread = None
        self.intent = self.lease = None
        self.grid = Grid()
        self.pose = Pose()
        self.localized = False
        self.trail = []
        self.route = []
        self.preview = None
        self.scans = self.steps = self.revision = 0
        self.travel = 0.
        self.phase = 'idle'
        self.message = 'Start a new map or load a saved map.'
        self.error = ''
        self.match = None
        self.name = 'Untitled map'
        self.job = None
        self.library = []
        self._map = self.grid.snapshot()
        self._deadline = 0.
        self._renewed = 0.
        self._planner_cache = None
        self._motion_started = False
        self.goal_adjustment = None
        self._physical_job=False
        self._route_partial=False
        self._connection_visited=[]
        self._connection_target=None
        self.route_progress={}
        # Disk recovery belongs to the same bounded worker, not controller startup.
        self._launch('restore', self._restore, physical=False)

    def _restore(self):
        settings_path = self.directory / 'settings.json'
        if settings_path.exists():
            if settings_path.stat().st_size>4096: raise ValidationError('Navigation settings file is too large')
            self.settings = Settings.parse(json.loads(settings_path.read_text(encoding='utf-8')))
        checkpoint = self.store.path('checkpoint')
        if checkpoint.exists():
            self._load('checkpoint')
            self.message = 'Map recovered. Press Scan room to find Robbi’s position.'
        else:
            self.grid = Grid(self.settings.resolution_cm)
            self._snapshot()
        self.library = self.store.listing()

    def _snapshot(self):
        value = self.grid.snapshot()
        with self.lock:
            self._map = value
            self.revision += 1

    def _checkpoint(self):
        self.store.save(self.grid, self.name, self.trail, identifier='checkpoint', settings=self.settings)

    def _check(self):
        if self.intent is None or self.intent.is_set():
            raise Halt(self.intent.reason if self.intent else 'Stopped')
        if time.monotonic() >= self._deadline:
            raise Halt('The time limit was reached. Map saved; you can continue exploring.')
        if self.lease is not None:
            if self.lease.is_set():
                raise Halt('Navigation was stopped by another activity.')
            if time.monotonic()-self._renewed >= 40:
                # Renew before expiry, retaining the same motion claim throughout.
                replacement = self.controller.orchestrator.acquire('motion', intent=self.intent, ttl_s=120, wait_s=0)
                previous, self.lease = self.lease, replacement
                self._renewed = time.monotonic()
                previous.release()

    def _launch(self, job, work, *, physical=False, timeout=1800):
        cancel = self.controller._action_token()[1] if hasattr(self.controller, '_action_token') else None
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise BusyError('Navigation is busy. Stop it or wait for completion.')
            if physical:
                try:self.hardware.ready(allow_no_return=True,allow_encoder_fallback=job=='navigate')
                except SensorWait:pass # Worker performs a bounded cancellable recovery.
            self.intent = Intent('navigation', MANUAL, cancelled=cancel)
            self._deadline = time.monotonic()+timeout
            self.error = ''
            self.goal_adjustment = None
            self._motion_started = False
            self.job, self.phase = job, job
            self._physical_job=physical
            self.preview = None if job != 'navigate' else self.preview
            if job != 'navigate':
                self.route = []

            def run():
                try:
                    with intent_scope(self.intent):
                        if physical or job == 'preview':
                            self.lease = self.controller.orchestrator.acquire('motion', intent=self.intent, ttl_s=120, wait_s=0)
                            self._renewed = time.monotonic()
                        self._check()
                        work()
                        self._check()
                except Exception as exc:
                    self.error = str(exc)[:400]
                    self.message = self.error
                    if physical and self._motion_started and not getattr(exc,'preserve_pose',False):
                        self.localized = False
                    self.preview = None
                    self.route = []
                    if not isinstance(exc, (Halt, BusyError, ValidationError)):
                        LOG.exception('Navigation job failed')
                finally:
                    # Hardware.move has already waited for motor cleanup.
                    if self.lease:
                        self.lease.release()
                        self.lease = None
                    with self.lock:
                        self.phase = 'needs_attention' if self.error else 'idle'
                        self.job = None

            self.thread = threading.Thread(target=run, name='navigation', daemon=True)
            self.thread.start()
        return {'ok':True, 'accepted':job}

    def is_running(self):
        with self.lock:
            return bool(self.thread and self.thread.is_alive())

    def status(self):
        with self.lock:
            return {'active':self.is_running(), 'job':self.job, 'phase':self.phase,
                    'filming_mode':bool(getattr(self.controller,'_filming_mode',False)),
                    'localized':self.localized, 'pose':self.pose.to_dict() if self.localized else None,
                    'estimated_pose':self.pose.to_dict(), 'message':self.message, 'last_error':self.error,
                    'last_stop_reason':self.message, 'map_name':self.name, 'revision':self.revision,
                    'scan_count':self.scans, 'explore_steps':self.steps, 'travel_cm':round(self.travel,1),
                    'scan_metrics':dict(getattr(self.hardware,'last_scan_metrics',{})),
                    'motion_recovery':dict(getattr(self.hardware,'last_motion_metrics',{})),
                    'route_progress':dict(self.route_progress),
                    'heading':getattr(self.hardware,'heading_status',lambda:{})(),
                    'goal_adjustment':self.goal_adjustment,
                    'tof':self.controller.tof.status() if hasattr(self.controller,'tof') else {},
                    'cells':len(self._map['cells']), 'match':self.match, 'preview':self.preview,
                    'trail':list(self.trail[-200:]),
                    'route':list(self.route), 'settings':self.settings.to_dict()}

    def map_data(self, since=None):
        with self.lock:
            return {'status':self.status(), 'revision':self.revision,
                    'grid':self._map if since != self.revision else None,
                    'trail':list(self.trail[-1500:]) if since != self.revision else None,
                    'pose':self.pose.to_dict() if self.localized else None}

    def sessions(self):
        return {'sessions':list(self.library)}

    def stop(self):
        with self.lock:
            intent = self.intent
            active = self.is_running()
            self.preview = None
            self.route = []
        if active and intent:
            intent.cancel('Stopped by user')
            self.controller.motion.stop(owner=intent)
        return {'ok':True, 'message':'Stopping navigation.' if active else 'Navigation is idle.'}

    def invalidate_pose(self, reason='Robbi was moved. Press Scan room to update its position.'):
        with self.lock:
            self.localized = False
            self.preview = None
            self.route = []
            self.message = reason
            if self.is_running() and self.intent:
                self.intent.cancel(reason)

    def observe_motion(self, intent):
        if intent is not self.intent:
            self.invalidate_pose('Robbi moved outside navigation. Press Scan room to update its position.')

    def track_drive_action(self, spec, future):
        # All actual launches are observed; compatibility callers need no second
        # guessed odometry update or extra Future callback.
        return None

    def set_localization_enabled(self, enabled):
        if not enabled:
            self.invalidate_pose('Localization disabled. Scan to locate Robbi again.')
        return {'ok':True, 'enabled':bool(enabled)}

    def reload_runtime_settings(self):
        self.invalidate_pose('Motion calibration changed. Relocalize before navigating.')

    def reset(self):
        def work():
            self.grid = Grid(self.settings.resolution_cm)
            self.pose = Pose(); self.localized = False
            self.trail = []; self.route = []; self.preview = None
            self.match = None; self.scans = self.steps = 0; self.travel = 0.
            self.name = 'Untitled map'
            self._snapshot(); self._checkpoint()
            self.message = 'New map. Press Scan room or Explore; the first scan collects several sweeps automatically.'
        return self._launch('new_map', work)

    def save(self, name=None):
        name = name or self.name
        if not isinstance(name,str) or not name.strip() or len(name)>80:
            raise ValidationError('Use a map name up to 80 characters')
        def work():
            if not self.grid.cells: raise Halt('Scan before saving a map.')
            self.name = name.strip()
            self.store.save(self.grid, self.name, self.trail, settings=self.settings)
            self._checkpoint(); self.library = self.store.listing()
            self.message = 'Map saved as '+self.name+'.'
        return self._launch('saving', work)

    def _load(self, identifier):
        grid, value = self.store.load(identifier)
        self.grid = grid
        geometry = value.get('geometry', {})
        # Sensor placement and scale are part of a map's coordinate geometry.
        # Motor and scan preferences belong to the robot, not the capture session.
        preferences = {key:getattr(self.settings,key) for key in (
            'drive_speed','turn_speed','scan_speed','scan_rate_deg_s','scan_bin_deg','scan_max_fill_turns')}
        self.settings = Settings.parse({**self.settings.to_dict(), **geometry, **preferences})
        self.name = value['name']; self.trail = value.get('trail', [])
        self.localized = False; self.pose = Pose(); self.route = []; self.preview = None; self.match = None
        self.scans = self.steps = 0; self.travel = 0.
        self._snapshot()
        self.message = 'Map loaded. Press Scan room to find Robbi’s position.'

    def load(self, identifier):
        self.store.path(identifier)
        def work():
            self._load(identifier); self._checkpoint()
        return self._launch('loading', work)

    def delete(self, identifier):
        self.store.path(identifier)
        def work():
            self.store.delete(identifier); self.library = self.store.listing()
            self.message = 'Saved map deleted. The active map is unchanged.'
        return self._launch('deleting', work)

    def configure(self, values):
        if not isinstance(values,dict): raise ValidationError('Settings must be an object')
        settings = Settings.parse({**self.settings.to_dict(), **values})
        geometry = ('resolution_cm','sensor_forward_cm','sensor_left_cm','sensor_yaw_deg')
        if self.grid.cells and any(getattr(settings,k)!=getattr(self.settings,k) for k in geometry):
            raise ValidationError('Start a new empty map before changing map resolution or sensor placement.')
        def work():
            write_json(self.directory/'settings.json', settings.to_dict())
            self.settings = settings
            if not self.grid.cells: self.grid = Grid(settings.resolution_cm)
            self._snapshot(); self._checkpoint(); self.message = 'Navigation settings saved.'
        return self._launch('settings', work)

    def _publish_pose(self, pose):
        with self.lock: self.pose = pose

    def _move(self, spec, *, transit=False):
        self._check()
        self.phase = 'turning' if spec['mode']=='turn' else 'driving'
        start = self.pose
        self._motion_started = True
        try:
            end, _ = self.hardware.move(spec, start, self.intent, self.settings, self._check, self._publish_pose,
                                        route_turn=spec['mode']=='turn',transit=transit)
            self.pose = end
        finally:
            # Feedback may have advanced the pose before a retryable failure.
            # Include that travel instead of replaying the original distance.
            distance = math.hypot(self.pose.x_cm-start.x_cm,self.pose.y_cm-start.y_cm)
            self.travel += distance
            self.trail.append([self.pose.x_cm,self.pose.y_cm]); self.trail = self.trail[-1500:]
        return distance

    @staticmethod
    def _rays_in_frame(rays,source,target):
        a=math.radians(-target.theta_deg);rotation=math.radians(angle(source.theta_deg-target.theta_deg))
        c,s=math.cos(rotation),math.sin(rotation)
        dx,dy=source.x_cm-target.x_cm,source.y_cm-target.y_cm
        ox,oy=math.cos(a)*dx-math.sin(a)*dy,math.sin(a)*dx+math.cos(a)*dy
        return [Ray(ox+c*r.x_cm-s*r.y_cm,oy+s*r.x_cm+c*r.y_cm,
                    angle(r.angle_deg+math.degrees(rotation)),r.distance_cm,r.hit,r.source,r.sweep_id) for r in rays]

    def _capture_scan(self,origin,*,bootstrap=False,single=False):
        if single:
            end,rays=self.hardware.scan(origin,self.intent,self.settings,self._check,self._publish_pose,
                                       allow_sparse=True,transit=True)
            return end,confirmed_empty(rays,self.settings.empty_clearance_cm)
        if not bootstrap:
            end,rays=self.hardware.scan(origin,self.intent,self.settings,self._check,self._publish_pose)
            if self.settings.empty_clearance_cm and any(r.source=='empty' for r in rays):
                self.message='Confirming empty directions with a second sweep.'
                second_origin=end
                end,extra=self.hardware.scan(second_origin,self.intent,self.settings,self._check,self._publish_pose,allow_sparse=True)
                rays+=self._rays_in_frame(extra,second_origin,origin)
            confirmed=confirmed_empty(rays,self.settings.empty_clearance_cm)
            if hasattr(self.hardware,'last_scan_metrics'):
                self.hardware.last_scan_metrics.update(confirmed_empty_bearings=sum(r.source=='inferred_empty' for r in confirmed))
            return end,confirmed
        began=time.monotonic();end=origin;bins={};totals={};aligned=0;observations=[]
        for sweep in range(1,7):
            self._check();self.phase='scanning'
            self.message=f'Building the first map: sweep {sweep} of up to 6. Collecting more real ToF points.'
            capture_origin=end
            end,extra=self.hardware.scan(capture_origin,self.intent,self.settings,self._check,self._publish_pose,allow_sparse=True)
            # Align an independent new sweep against preceding sweeps, never
            # against itself. This corrects drift and bounds position spread
            # during a longer first scan without inventing confidence.
            if len(bins)>=24 and len([r for r in extra if r.hit])>=18:
                reference=Grid(self.settings.resolution_cm)
                reference.insert(origin,list(bins.values()),body_radius=self.settings.robot_radius_cm)
                alignment=Matcher(reference,self._check).search(extra,capture_origin,budget_s=self.settings.max_match_s)
                if alignment['accepted']:
                    matched=alignment['pose'];rotation=math.radians(angle(matched.theta_deg-capture_origin.theta_deg))
                    dx,dy=end.x_cm-capture_origin.x_cm,end.y_cm-capture_origin.y_cm
                    end=Pose(matched.x_cm+math.cos(rotation)*dx-math.sin(rotation)*dy,
                             matched.y_cm+math.sin(rotation)*dx+math.cos(rotation)*dy,
                             angle(end.theta_deg+math.degrees(rotation)),matched.sigma_cm+1,matched.sigma_deg+1)
                    capture_origin=matched;aligned+=1
            observations.extend(self._rays_in_frame(extra,capture_origin,origin))
            bins={}
            confirmed=confirmed_empty(observations,self.settings.empty_clearance_cm)
            for ray in confirmed:
                key=round(ray.angle_deg%360)%360
                if key not in bins or prefer_ray(ray,bins[key]):bins[key]=ray
            metrics=getattr(self.hardware,'last_scan_metrics',{})
            for key in ('raw_points','received_samples','invalid_returns','unmatched_timestamps','buffer_dropped'):
                totals[key]=totals.get(key,0)+metrics.get(key,len(extra) if key=='raw_points' else 0)
            bearings=sorted(r.angle_deg%360 for r in bins.values())
            gap=max((b-a for a,b in zip(bearings,bearings[1:]+[bearings[0]+360],strict=True)),default=360) if bearings else 360
            summary={**metrics,**totals,'bootstrap':True,'sweeps':sweep,'points':len(bins),'combined_points':len(bins),
                     'confirmed_empty_bearings':sum(r.source=='inferred_empty' for r in bins.values()),
                     'aligned_sweeps':aligned,
                     'largest_gap_deg':round(gap,2),'observed_span_deg':round(360-gap,2),
                     'angular_support_percent':round(sum(min(10,b-a) for a,b in zip(bearings,bearings[1:]+[bearings[0]+360],strict=True))/3.6,1) if bearings else 0,
                     'partial_coverage':gap>10,'phase':'building_first_map',
                     'total_elapsed_s':round(time.monotonic()-began,3)}
            self.hardware.last_scan_metrics=summary
            if sweep>=(2 if summary['confirmed_empty_bearings']>=24 else 3) and len(bins)>=90 and gap<=8:break
        if len(bins)<24 or 360-gap<180:
            raise Halt('The front sensor could not see enough of the room. Check that its view is unobstructed.')
        summary['phase']='partial' if summary['partial_coverage'] else 'complete'
        self.hardware.last_scan_metrics=summary
        return end,list(bins.values())

    def _scan(self, hint=None, *, global_search=False,best_effort=False):
        self._check(); self.phase = 'scanning'; self.message = 'Scanning a full circle with ToF, encoders and IMU.'
        origin = self.pose
        first_map=not self.grid.cells
        self._motion_started = True
        end, rays = self._capture_scan(origin,bootstrap=first_map and not best_effort,single=best_effort)
        reliable=True
        if self.grid.cells and (len([r for r in rays if r.hit])>=18 or not self.localized or global_search):
            matcher=Matcher(self.grid,self._check)
            for attempt in range(2):
                self.phase = 'localizing'
                match = matcher.search(rays, None if global_search or not self.localized else origin,
                                       hint=hint, budget_s=self.settings.max_match_s)
                matched = match.pop('pose')
                self.match = {**match, 'pose':matched.to_dict()}
                if match['accepted']:break
                if best_effort:
                    reliable=False
                    self.match.update(mode='odometry_only')
                    break
                if attempt or match['score']<.6 or match['margin']>=.035:raise LocalizationUncertain(match['reason'])
                # Ambiguous geometry gets one more independent capture. Keep
                # both scans in the first scan's frame, using measured motion;
                # no map evidence is inserted until the match is accepted.
                self._check();self.phase='scanning'
                self.message='Collecting one more partial scan to resolve the position.'
                extra_origin=end
                end,extra=self.hardware.scan(extra_origin,self.intent,self.settings,self._check,self._publish_pose)
                rays=list(rays)+self._rays_in_frame(extra,extra_origin,origin)
                if hasattr(self.hardware,'last_scan_metrics'):
                    self.hardware.last_scan_metrics.update(localization_extra_scans=1,combined_points=len(rays))
            if reliable:
                correction = math.radians(angle(matched.theta_deg-origin.theta_deg))
                dx,dy = end.x_cm-origin.x_cm,end.y_cm-origin.y_cm
                end = Pose(matched.x_cm+math.cos(correction)*dx-math.sin(correction)*dy,
                           matched.y_cm+math.sin(correction)*dx+math.cos(correction)*dy,
                           angle(end.theta_deg+math.degrees(correction)), matched.sigma_cm+1, matched.sigma_deg+1)
                origin = matched
        elif self.grid.cells:
            self.match={'accepted':False,'mode':'odometry_only','reason':'Open scan has no distinct obstacle landmarks; retaining measured odometry.'}
        self._check()
        # Drive position checks localize against the existing map. They do not
        # expand it from an uncertain pose or hit storage limits on a long trip.
        if reliable and not best_effort:self.grid.insert(origin, rays, body_radius=self.settings.robot_radius_cm)
        self.pose = end; self.localized = True; self.scans += 1
        self.trail.append([end.x_cm,end.y_cm]); self.trail = self.trail[-1500:]
        self._snapshot(); self._checkpoint()
        self.message = ('Scan saved with partial coverage. Confirmed empty sectors are white; select a destination or explore.'
                        if getattr(self.hardware,'last_scan_metrics',{}).get('partial_coverage') else
                        'Scan complete. Position localized; select a destination or explore.')
        if best_effort:return bool(self.match and self.match.get('accepted'))

    def start_scan(self, hint=None, *, relocalize=False):
        return self._launch('scan', lambda:self._scan(hint, global_search=relocalize or not self.localized or hint is not None), physical=True, timeout=240)

    def _planner(self):
        self.phase = 'planning'; self._check()
        radius = self.settings.robot_radius_cm+self.settings.clearance_cm+min(5,self.pose.sigma_cm)
        key = (self.grid, self.grid.revision, radius)
        if self._planner_cache is None or self._planner_cache[0] != key:
            self._planner_cache = (key, Planner(self.grid, radius, self._check,allow_unknown_margin=True,
                                               escape_radius=self.settings.robot_radius_cm))
        return self._planner_cache[1]

    def _position(self): return (self.pose.x_cm,self.pose.y_cm)

    def _plan(self, goal):
        planner = self._planner()
        try:
            try:route = planner.route(self._position(),goal,snap_cm=planner.radius+2*self.grid.resolution)
            except StartBlocked:
                if not self._physical_job:raise
                self.message='Updating the local obstacle map automatically before driving.'
                self._scan()
                planner=self._planner()
                route=planner.route(self._position(),goal,snap_cm=planner.radius+2*self.grid.resolution)
        except NoRoute:
            if not self._physical_job:raise
            route=None
            for attempt in range(2):
                candidates=planner.frontiers(self._position())
                candidates.sort(key=lambda row:math.dist(row[1],goal)-row[0]*.05)
                for _,point in candidates:
                    key=planner.grid.cell(*point)
                    if key in self._connection_visited or math.dist(self._position(),point)<10:continue
                    try:route=planner.route(self._position(),point)
                    except NoRoute:continue
                    self._route_partial=True;self.goal_adjustment=None
                    self._connection_target=point
                    self.route=[list(p) for p in route]
                    self.message='Finding the connection automatically: moving toward the next visible opening, then scanning again.'
                    return planner,route
                if attempt==0:
                    self.message='Refreshing the map automatically to find the connection.'
                    self._scan();planner=self._planner()
                    try:route=planner.route(self._position(),goal,snap_cm=planner.radius+2*self.grid.resolution)
                    except NoRoute:continue
                    break
            if route is None:raise NoRoute('No traversable opening was found after checking nearby routes and refreshing the map.') from None
        self._route_partial=False
        self._connection_target=None
        if math.dist(goal,route[-1])>self.grid.resolution:
            self.goal_adjustment={'requested':list(goal),'destination':list(route[-1])}
        elif self.goal_adjustment and math.dist(goal,self.goal_adjustment['destination'])>self.grid.resolution:
            self.goal_adjustment=None
        self.route = [list(p) for p in route]
        return planner, route

    def plan(self, goal):
        if not isinstance(goal, (list, tuple)) or len(goal) != 2:
            raise ValidationError('Supply two map coordinates in centimeters')
        goal = tuple(finite(v, 'goal coordinate', -3000, 3000) for v in goal)
        preview_id = uuid.uuid4().hex
        def work():
            if not self.localized: raise Halt('Scan or relocalize before selecting a driving route.')
            _, route = self._plan(goal)
            self._check()
            self.preview = {'id':preview_id, 'goal':list(goal), 'revision':self.revision, 'pose':self.pose.to_dict(),
                            'length_cm':round(sum(math.dist(a,b) for a,b in zip(route,route[1:], strict=False)),1)}
            self.message = 'Route ready. Press Drive to point to start.'
        return {**self._launch('preview', work, timeout=30), 'preview_id':preview_id}

    def _follow_leg(self, goal, budget, *, planned=None, transit=False):
        # Directed travel uses the whole visible waypoint/leg. Exploration still
        # uses short probes and early scans where map evidence is uncertain.
        planner, route = planned if planned is not None else self._plan(goal)
        traveled = 0.
        for target in route[1:]:
            while math.dist(self._position(),target)>max(3,self.grid.resolution):
                self._check()
                odometry_only=bool(self.match and self.match.get('mode')=='odometry_only')
                if traveled>=budget or (not transit and self.pose.sigma_cm>=self.settings.rescan_sigma_cm and not odometry_only):return
                for _ in range(3):
                    dx,dy = target[0]-self.pose.x_cm, target[1]-self.pose.y_cm
                    turn = angle(math.degrees(math.atan2(dy,dx))-self.pose.theta_deg)
                    if abs(turn)<=3:break
                    self._check()
                    self._move({'mode':'turn','angle_deg':turn,'speed':self.settings.turn_speed})
                # Turning can translate the axle and leave a residual heading error.
                # Validate the measured pose and intended straight segment, not just
                # the ideal line to the waypoint computed before the turn.
                dx,dy = target[0]-self.pose.x_cm, target[1]-self.pose.y_cm
                if abs(angle(math.degrees(math.atan2(dy,dx))-self.pose.theta_deg))>5:
                    raise Halt('Heading did not align with the route. Relocalize before continuing.')
                if not transit and self.pose.sigma_cm>=self.settings.rescan_sigma_cm and not odometry_only:return
                if not planner.visible(self.grid.cell(*self._position()),self.grid.cell(*target)):
                    return
                clearance = planner.clearance.get(self.grid.cell(*self._position()),0)*self.grid.resolution
                length = min(math.hypot(dx,dy),500. if transit else self.settings.segment_cm,budget-traveled)
                if length<2: return
                narrow = self.settings.robot_radius_cm+self.settings.clearance_cm+15
                direction = math.radians(self.pose.theta_deg)
                endpoint = self.grid.cell(self.pose.x_cm+math.cos(direction)*length,self.pose.y_cm+math.sin(direction)*length)
                cautious=odometry_only or bool(getattr(planner,'escape',())) or any(cell in getattr(planner,'uncertain',()) for cell in line(self.grid.cell(*self._position()),endpoint))
                if cautious and not transit:
                    length=min(length,10.)
                    endpoint=self.grid.cell(self.pose.x_cm+math.cos(direction)*length,self.pose.y_cm+math.sin(direction)*length)
                if not planner.visible(self.grid.cell(*self._position()),endpoint):
                    self.message = 'Measured heading leaves the clear route. Rescanning before replanning.'
                    return
                approaching = False
                if clearance>=narrow and not transit:
                    for cell in line(self.grid.cell(*self._position()),endpoint):
                        if planner.clearance.get(cell,0)*self.grid.resolution<narrow:
                            length=min(length,max(5,math.dist(self._position(),self.grid.world(cell))-10))
                            approaching=True;break
                try:sample = self.hardware.wait_for_range(self.intent,self._check,allow_unknown=True)
                except SensorWait:
                    # Queue only a short leg; Hardware.move waits/rechecks real
                    # sensor health before it starts any motor.
                    sample={'distance_cm':None}
                margin = self.settings.robot_radius_cm+self.settings.clearance_cm-self.settings.sensor_forward_cm
                unknown=sample['distance_cm'] is None
                if unknown and not transit:length=min(length,self.settings.unknown_step_cm)
                obstacle_ahead=False
                if not unknown and sample['distance_cm']<length+margin:
                    if not transit:
                        self.message = 'New obstacle ahead. Rescanning before replanning.'
                        return
                    # Advance to the clear stopping point instead of scanning
                    # immediately because the sensor cannot see the whole leg.
                    length=min(length,max(0.,sample['distance_cm']-margin-3.))
                    if length<2:return
                    obstacle_ahead=True
                moved = self._move({'mode':'distance','distance_cm':length,'speed':self.settings.drive_speed},transit=transit)
                traveled += moved
                if moved<1: raise Halt('No measurable progress. Check wheel traction before continuing.')
                # Rescan before/inside a narrow area instead of completing a long blind leg.
                if not transit and (unknown or getattr(self.hardware,'last_motion_metrics',{}).get('unknown_range')):
                    self.message='No echo ahead; completed a short measured step. Scanning again before continuing.'
                    return
                if obstacle_ahead or (not transit and (approaching or clearance<narrow or cautious)): return
        return

    def _validate_preview(self, preview, preview_id=None):
        if (not self.localized or not preview or preview is not self.preview
                or preview['revision']!=self.revision or preview['pose']!=self.pose.to_dict()
                or (preview_id is not None and preview.get('id')!=preview_id)):
            raise ValidationError('Preview a new route from Robbi’s current position first.')

    def navigate(self, preview_id=None, *, goal=None):
        direct=goal is not None
        if direct:
            if preview_id is not None:raise ValidationError('Supply a destination or a preview ID, not both')
            if not isinstance(goal,(list,tuple)) or len(goal)!=2:
                raise ValidationError('Supply two map coordinates in centimeters')
            goal=tuple(finite(v,'goal coordinate',-3000,3000) for v in goal)
        def work():
            # Recheck after acquiring motion ownership: another activity may have
            # invalidated the pose between HTTP admission and worker execution.
            with self.lock:
                if direct:
                    if not self.localized or self.pose.to_dict()!=admitted_pose or self.revision!=admitted_revision:
                        raise ValidationError('Robbi’s position changed. Select the destination again.')
                else:self._validate_preview(preview, preview_id)
                self.preview = None
            RouteRunner(self).run(goal)
        with self.lock:
            preview = self.preview
            if direct:
                if not self.localized:raise ValidationError('Scan or relocalize before driving.')
            else:
                self._validate_preview(preview, preview_id)
                goal = tuple(preview['goal'])
            admitted_pose=self.pose.to_dict();admitted_revision=self.revision
            return self._launch('navigate', work, physical=True)

    def start_explore(self, max_steps=None, *, max_distance_cm=None, timeout_s=None):
        steps = int(finite(30 if max_steps is None else max_steps,'scan steps',1,100))
        if max_steps is not None and steps!=max_steps:raise ValidationError('Scan steps must be a whole number')
        distance = finite(2000 if max_distance_cm is None else max_distance_cm,'travel limit',10,10000)
        timeout = finite(1800 if timeout_s is None else timeout_s,'time limit',10,3600)
        def work():
            begin = self.travel; visited = []; blocked = []; no_growth = 0
            self._scan()
            for _ in range(steps):
                self._check()
                if self.travel-begin>=distance: break
                planner = self._planner()
                targets = planner.frontiers(self._position(),visited,blocked)
                if not targets:
                    self.message = 'No reachable unexplored boundary remains. Check the map for inaccessible rooms.'; return
                goal = targets[0][1]
                start = self._position(); before = len(self.grid.cells)
                self._follow_leg(goal,min(self.settings.leg_cm,distance-(self.travel-begin)))
                visited.append(start); self.steps += 1
                self._scan()
                if math.dist(start,self._position())<8: blocked.append(goal)
                no_growth = no_growth+1 if len(self.grid.cells)-before<10 else 0
                if no_growth>=4:
                    self.message = 'Mapping paused: recent scans found no new reachable space.'; return
            self.message = 'Exploration limit reached. Map saved; press Explore to continue.'
        return self._launch('explore',work,physical=True,timeout=timeout)
