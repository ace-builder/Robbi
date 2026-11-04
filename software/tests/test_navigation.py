"""Synthetic geometry, sensor timelines and state-machine tests; no hardware."""
import json
import math
import threading
import time
from types import SimpleNamespace

import pytest
from flask import Flask

from robbi.errors import BusyError, ValidationError
from robbi.navigation.grid import Grid
from robbi.navigation.hardware import interpolate
from robbi.navigation.localization import Matcher
from robbi.navigation.model import Halt, Pose, Ray, Settings, angle
from robbi.navigation.planning import Planner
from robbi.navigation.service import NavigationService
from robbi.navigation.storage import MapStore
from robbi.navigation.web import blueprint
from robbi.orchestration import Orchestrator, Intent
from robbi.web import install_error_handlers


ROOM = [(-200,-180),(240,-180),(240,110),(130,110),(130,260),(-200,260)]


def scan(pose, polygon=ROOM, step=4):
    edges=list(zip(polygon,polygon[1:]+polygon[:1])); rays=[]
    for a in range(0,360,step):
        heading=math.radians(pose.theta_deg+a);dx,dy=math.cos(heading),math.sin(heading)
        ranges=[]
        for (x,y),(xx,yy) in edges:
            ex,ey=xx-x,yy-y;den=dx*ey-dy*ex
            if abs(den)<1e-8:continue
            px,py=x-pose.x_cm,y-pose.y_cm
            t=(px*ey-py*ex)/den;u=(px*dy-py*dx)/den
            if t>0 and 0<=u<=1:ranges.append(t)
        distance=min(ranges,default=400)
        rays.append(Ray(0,0,a,min(distance,400),distance<400))
    return rays


def room():
    grid=Grid()
    for pose in (Pose(),Pose(80,-80),Pose(-80,120),Pose(60,160)):
        grid.insert(pose,scan(pose),body_radius=12)
    return grid


def test_odometry_integrates_measured_arcs_and_uncertainty_independent_of_tick_rate():
    p=Pose().moved(math.pi*10,90)
    assert (p.x_cm,p.y_cm)==pytest.approx((20,20))
    q=Pose()
    for _ in range(100):q=q.moved(math.pi*.1,.9)
    assert q.x_cm==pytest.approx(p.x_cm)
    assert q.sigma_cm==pytest.approx(p.sigma_cm)


def test_timestamp_interpolation_crosses_heading_wrap_without_extrapolation():
    history=[(1.,Pose(theta_deg=179)),(1.1,Pose(10,0,-179))]
    p=interpolate(history,1.05)
    assert p.x_cm==pytest.approx(5)
    assert abs(p.theta_deg)==pytest.approx(180)
    assert interpolate(history,.99) is None
    assert interpolate(history,1.2) is None


def test_unknown_scan_gaps_are_not_filled_and_size_limit_is_atomic():
    grid=Grid();grid.insert(Pose(),[Ray(0,0,0,100),Ray(0,0,90,100)])
    assert (8,8) not in grid.cells
    previous=dict(grid.cells)
    with pytest.raises(Halt):grid.insert(Pose(5000,0),[Ray(0,0,0,100)])
    assert grid.cells==previous


def test_isolated_first_hit_is_tentative_then_repeated_hit_becomes_obstacle():
    grid=Grid(cells={(10,0):-20})
    grid.insert(Pose(),[Ray(0,0,0,50)])
    assert grid.cells[10,0]==1
    grid.insert(Pose(),[Ray(0,0,0,50)])
    assert grid.cells[10,0]>=2
    assert (10,0) not in Planner(grid,0).safe


def test_footprint_planner_avoids_wall_and_unknown_and_reports_unreachable():
    grid=Grid(cells={(x,y):-10 for x in range(-30,31) for y in range(-20,21)})
    for y in range(-20,12):grid.cells[0,y]=10
    p=Planner(grid,12)
    route=p.route((-100,0),(100,0))
    assert len(route)>2 and max(y for x,y in route)>60
    assert all(p.visible(grid.cell(*a),grid.cell(*b)) for a,b in zip(route,route[1:]))
    with pytest.raises(Halt):p.route((-100,0),(200,0))
    for y in range(12,21):grid.cells[0,y]=10
    with pytest.raises(Halt):Planner(grid,12).route((-100,0),(100,0))


def test_narrow_corridor_and_corner_cutting_rejected():
    grid=Grid(cells={(x,y):-8 for x in range(30) for y in range(-2,3)})
    assert not Planner(grid,16).safe
    grid=Grid(cells={(0,0):-8,(1,1):-8})
    p=Planner(grid,0)
    assert not p.visible((0,0),(1,1))


def test_diagonal_wall_does_not_falsely_block_clear_start():
    # A full-radius robot fits here. Square erosion used to reject the start
    # (as on the live map) despite sufficient Euclidean obstacle clearance.
    grid=Grid(cells={(x,y):-8 for x in range(-20,21) for y in range(-20,21)
                     if 2*x+3*y>=-20})
    planner=Planner(grid,20.64)
    assert planner.clearance[0,0]*5>=20.64
    assert planner.route((1.9,-1.8),(50,0))
    # The wall and unknown area are still blocked with the same footprint.
    with pytest.raises(Halt):planner.route((-15,-15),(50,0))
    with pytest.raises(Halt):planner.route((0,0),(-50,-50))


def test_sparse_euclidean_clearance_matches_brute_force():
    import random
    randomizer=random.Random(314)
    free={(x,y) for x in range(-12,13) for y in range(-11,12)
          if randomizer.random()>.035}
    # A distant island must not allocate the enormous bounding rectangle.
    free|={(x+590,y+590) for x in range(8) for y in range(8)}
    grid=Grid(cells={c:-8 for c in free})
    planner=Planner(grid,12)
    boundary={(x+dx,y+dy) for x,y in free for dx,dy in
              ((-1,0),(1,0),(0,-1),(0,1)) if (x+dx,y+dy) not in free}
    for cell in free:
        exact=min(math.dist(cell,blocked) for blocked in boundary)
        assert planner.clearance[cell]==pytest.approx(max(0,exact-math.sqrt(2)))
    assert all(grid.cells[c]<0 for c in planner.safe)


def test_clearance_covers_both_cell_areas_and_honors_cancellation():
    grid=Grid(cells={(x,y):-8 for x in range(-15,16) for y in range(-15,16)})
    grid.cells[4,4]=12
    # The centers are 28 cm apart, but the two cell areas are only 21 cm
    # apart. A 22 cm footprint must still be rejected, including subcell poses.
    planner=Planner(grid,22)
    assert (0,0) not in planner.safe
    with pytest.raises(Halt,match='local map'):
        planner.route((2.4,2.4),(-30,-30))
    calls=[]
    def cancel():
        calls.append(True)
        if len(calls)>2:raise Halt('Stop during clearance')
    with pytest.raises(Halt,match='Stop during clearance'):Planner(grid,12,cancel)


def test_exploration_does_not_turn_unknown_side_cells_into_solid_obstacles():
    grid=Grid(cells={(x,y):-8 for x in range(-16,17) for y in range(-12,13)})
    del grid.cells[-2,-4] # The live failure: one missing cell near a clear start.
    grid.cells[0,11]=12
    planner=Planner(grid,21,allow_unknown_margin=True)
    assert (0,0) in planner.safe and (0,0) in planner.uncertain
    assert planner.route((0,0),(40,0))
    assert (-2,-4) not in planner.safe
    assert (0,10) not in planner.safe # Measured wall is still inflated.


def test_exploration_still_rejects_a_corridor_with_measured_walls():
    grid=Grid(cells={(x,y):-8 for x in range(30) for y in range(-2,3)})
    grid.cells.update({(x,y):12 for x in range(30) for y in (-3,3)})
    assert not Planner(grid,16,allow_unknown_margin=True).safe


def test_nearby_unobserved_goal_snaps_to_reachable_observed_floor():
    grid=Grid(cells={(x,y):-8 for x in range(-8,9) for y in range(-8,9)})
    planner=Planner(grid,21,allow_unknown_margin=True)
    route=planner.route((0,0),(48,0),snap_cm=26)
    assert route[-1] in [grid.world(c) for c in planner.safe]
    assert math.dist(route[-1],(48,0))<=26
    with pytest.raises(Halt):planner.route((0,0),(300,0),snap_cm=26)


def test_local_match_recovers_encoder_drift():
    actual=Pose(35,25,17)
    result=Matcher(room()).search(scan(actual),Pose(44,18,22),budget_s=15)
    assert result['accepted'],result
    assert math.dist((result['pose'].x_cm,result['pose'].y_cm),(35,25))<10
    assert abs(angle(result['pose'].theta_deg-17))<4


def test_global_relocalization_and_symmetric_room_ambiguity():
    result=Matcher(room()).search(scan(Pose(40,70,49)),budget_s=15)
    assert result['accepted'],result
    assert math.dist((result['pose'].x_cm,result['pose'].y_cm),(40,70))<12
    square=[(-150,-150),(150,-150),(150,150),(-150,150)]
    grid=Grid();grid.insert(Pose(),scan(Pose(),square))
    ambiguous=Matcher(grid).search(scan(Pose(20,30,8),square),budget_s=15)
    assert not ambiguous['accepted'],ambiguous


def test_hint_without_heading_and_compute_cancel():
    result=Matcher(room()).search(scan(Pose(40,70,49)),hint={'x_cm':40,'y_cm':70},budget_s=15)
    assert result['accepted'],result
    def cancel():raise Halt('Stop')
    with pytest.raises(Halt,match='Stop'):Matcher(room(),cancel)


def test_storage_rejects_corruption_and_path_escape(tmp_path):
    store=MapStore(tmp_path);saved=store.save(room(),'Apartment',[[0,0]])
    grid,value=store.load(saved['id'])
    assert value['name']=='Apartment' and grid.cells
    with pytest.raises(ValidationError):store.path('../secret')
    value['grid']['cells'][0][2]=999
    store.path(saved['id']).write_text(json.dumps(value))
    with pytest.raises(ValidationError):store.load(saved['id'])
    assert store.listing()[0]['error']


class FakeHardware:
    def __init__(self):self.moves=[];self.fail=None;self.block=None;self.actual=Pose()
    def ready(self, **kwargs):
        if self.fail:raise Halt(self.fail)
    def range_sample(self):return {'distance_cm':350}
    def wait_for_range(self,intent,check,*,allow_unknown=False):check();return self.range_sample()
    def move(self,spec,pose,intent,settings,check,publish,**kw):
        self.moves.append(spec)
        if self.block:
            while not self.block.wait(.005):check()
        check()
        end=pose.moved(spec.get('distance_cm',0),spec.get('angle_deg',0))
        self.actual=self.actual.moved(spec.get('distance_cm',0),spec.get('angle_deg',0))
        publish(end);return end,[]
    def scan(self,pose,intent,settings,check,publish,**kwargs):
        self.moves.append({'scan':True});check()
        rays=scan(self.actual);self.last_scan_metrics={'raw_points':len(rays)}
        return pose,rays


def idle(service):
    service.thread.join(10)
    assert not service.is_running()
    return service.status()


@pytest.fixture
def service(tmp_path):
    stop=threading.Event();hardware=FakeHardware()
    controller=SimpleNamespace(orchestrator=Orchestrator(),_action_token=lambda:(1,stop),
                               motion=SimpleNamespace(stop=lambda **kw:None),_filming_mode=False)
    value=NavigationService(controller,tmp_path,hardware=hardware);controller.slam=value
    idle(value)
    yield value
    value.stop()
    if value.thread:value.thread.join(3)


def test_loading_old_map_keeps_current_motor_power_preferences(service):
    old=Settings(drive_speed=35,turn_speed=35,scan_speed=35,sensor_forward_cm=6,
                 scan_bin_deg=6,scan_max_fill_turns=0,scan_rate_deg_s=60)
    saved=service.store.save(room(),'Old map',[],settings=old)
    service.load(saved['id']);idle(service)
    assert service.settings.drive_speed==service.settings.turn_speed==service.settings.scan_speed==100
    assert service.settings.sensor_forward_cm==6
    assert service.settings.scan_bin_deg==1
    assert service.settings.scan_max_fill_turns==8
    assert service.settings.scan_rate_deg_s==90


def test_map_lifecycle_click_navigation_and_load_requires_relocalization(service):
    service.start_scan();assert idle(service)['localized']
    service.plan((50,0));assert idle(service)['preview']
    service.navigate();status=idle(service)
    assert not status['last_error'],status
    assert math.dist(service._position(),(50,0))<8
    service.save('Home');idle(service)
    ident=service.sessions()['sessions'][0]['id']
    service.load(ident);assert not idle(service)['localized']
    with pytest.raises(ValidationError):service.navigate()
    service.start_scan({'x_cm':50,'y_cm':0,'theta_deg':0},relocalize=True)
    assert idle(service)['localized']


def test_first_map_collects_multiple_real_sweeps_and_later_scan_is_quick(service):
    service.start_scan();status=idle(service)
    assert status['localized'] and not status['last_error']
    assert status['scan_metrics']['bootstrap']
    assert 3<=status['scan_metrics']['sweeps']<=6
    assert status['scan_metrics']['points']>=90
    before=service.hardware.moves.count({'scan':True})
    service.start_scan();assert not idle(service)['last_error']
    assert service.hardware.moves.count({'scan':True})==before+1


def test_first_scan_combines_sparse_sweeps_into_a_denser_map(service):
    captures=[]
    def sparse(pose,*args,**kwargs):
        shift=4*len(captures);captures.append(True)
        service.hardware.last_scan_metrics={'raw_points':30}
        return pose.moved(0,360),[Ray(0,0,a+shift,100) for a in range(0,360,12)]
    service.hardware.scan=sparse
    service.start_scan();status=idle(service)
    assert status['localized'] and not status['last_error']
    assert len(captures)==3 and status['scan_metrics']['points']==90
    assert status['scan_metrics']['raw_points']==90
    assert status['scan_metrics']['largest_gap_deg']==4


def test_first_scan_stop_between_sweeps_keeps_map_atomic(service):
    original=service.hardware.scan;captures=[]
    def cancel(pose,*args,**kwargs):
        captures.append(True)
        if len(captures)==2:raise Halt('Stopped by user')
        return original(pose,*args,**kwargs)
    service.hardware.scan=cancel
    service.start_scan();status=idle(service)
    assert len(captures)==2 and status['last_error']=='Stopped by user'
    assert not service.grid.cells and not status['localized']


def test_cautious_exploration_moves_only_a_short_segment_near_unknown_margin(service):
    service.grid=Grid(cells={(x,y):-8 for x in range(-6,31) for y in range(-3,4)})
    service.pose=Pose();service.localized=True
    service._launch('test_follow',lambda:service._follow_leg((100,0),200),physical=True)
    status=idle(service)
    assert not status['last_error'],status
    moves=[m for m in service.hardware.moves if m.get('mode')=='distance']
    assert len(moves)==1 and moves[0]['distance_cm']<=10


def test_scan_automatically_searches_saved_map_when_position_is_lost(service,monkeypatch):
    service.start_scan();idle(service);service.localized=False
    priors=[];original=Matcher.search
    def search(matcher,rays,prior=None,**kwargs):
        priors.append(prior);return original(matcher,rays,prior,**kwargs)
    monkeypatch.setattr(Matcher,'search',search)
    service.start_scan({'x_cm':0,'y_cm':0,'theta_deg':0})
    assert idle(service)['localized']
    assert priors and all(prior is None for prior in priors)


def test_click_to_drive_from_clear_diagonal_wall_pose(service):
    service.grid=Grid(cells={(x,y):-8 for x in range(-20,21) for y in range(-20,21)
                             if 2*x+3*y>=-20})
    service.pose=Pose(1.9,-1.8,0,4.64,2);service.localized=True
    service._snapshot()
    service.plan((20,0));status=idle(service)
    assert not status['last_error'] and status['preview']
    assert not service.hardware.moves, 'Route preview must never move the robot'
    service.navigate(status['preview']['id']);status=idle(service)
    assert not status['last_error'],status
    assert math.dist(service._position(),(20,0))<8
    assert status['localized'] and not status['preview']


def test_stop_pickup_and_reservation_prevent_overlap_and_automatic_resume(service):
    service.start_scan();idle(service);service.plan((60,0));idle(service)
    service.hardware.block=threading.Event();service.navigate()
    deadline=time.monotonic()+2
    while not service.hardware.moves[-1].get('distance_cm') and time.monotonic()<deadline:time.sleep(.005)
    with pytest.raises(BusyError):
        service.controller.orchestrator.acquire('motion',intent=Intent('app',50),wait_s=0)
    with pytest.raises(BusyError):service.start_scan()
    service.invalidate_pose('Picked up')
    assert not idle(service)['localized']
    count=len(service.hardware.moves);time.sleep(.02);assert len(service.hardware.moves)==count
    assert not service.controller.orchestrator.snapshot()['resources']


def test_http_invalid_requests_and_preview_never_move(service):
    app=Flask(__name__);install_error_handlers(app)
    app.register_blueprint(blueprint(SimpleNamespace(controller=lambda:service.controller)))
    client=app.test_client()
    for path,data in [('plan',{'goal':{'x_cm':float('nan'),'y_cm':0}}),('navigate',{'ignore_obstacles':True}),
                      ('settings',{'settings':None}),('load',{'id':'../../secret'}),('scan',{'speed':99})]:
        assert client.post('/api/navigation/'+path,json=data).status_code==400
    assert service.hardware.moves==[]
    assert client.post('/api/navigation/plan',json={'goal':{'x_cm':10,'y_cm':0}}).status_code==202
    idle(service);assert service.hardware.moves==[]
    snapshot=client.get('/api/navigation/map').json
    assert client.get('/api/navigation/map?since='+str(snapshot['revision'])).json['grid'] is None


def test_direct_http_drive_plans_and_moves_without_preview(service):
    app=Flask(__name__);install_error_handlers(app)
    app.register_blueprint(blueprint(SimpleNamespace(controller=lambda:service.controller)))
    client=app.test_client()
    service.start_scan();idle(service)
    before=len(service.hardware.moves)
    assert service.preview is None
    response=client.post('/api/navigation/navigate',json={'goal':{'x_cm':50,'y_cm':0}})
    assert response.status_code==202
    status=idle(service)
    assert not status['last_error'] and status['localized']
    assert math.dist(service._position(),(50,0))<8
    assert len(service.hardware.moves)>before


def test_unmapped_goal_uses_live_range_without_repeated_connection_scans(service):
    service.start_scan();idle(service)
    before=len(service.hardware.moves)
    service.navigate(goal=(2900,2900));status=idle(service)
    assert not status['last_error'] and status['localized'],status
    assert not status['preview'] and not status['route']
    moves=service.hardware.moves[before:]
    assert sum(bool(move.get('scan')) for move in moves)==2
    assert any(move.get('mode')=='distance' for move in moves)
    assert len(moves)<500
    service.navigate(goal=(40,0));assert not idle(service)['last_error']


def test_direct_drive_arrives_at_adjusted_stopping_point_instead_of_chasing_wall(service):
    service.start_scan();idle(service)
    service.navigate(goal=(240,0));status=idle(service)
    assert not status['last_error'],status
    assert status['goal_adjustment']['requested']==[240,0]
    target=status['goal_adjustment']['destination']
    assert math.dist(service._position(),target)<8
    assert math.dist(target,(240,0))>15
    assert status['localized'] and 'clear stopping point' in status['message']


def test_direct_drive_rechecks_pose_after_motion_ownership(service,monkeypatch):
    service.start_scan();idle(service)
    before=len(service.hardware.moves)
    acquire=service.controller.orchestrator.acquire
    def invalidate(*args,**kwargs):
        lease=acquire(*args,**kwargs)
        service.invalidate_pose('Moved while waiting for ownership')
        return lease
    monkeypatch.setattr(service.controller.orchestrator,'acquire',invalidate)
    service.navigate(goal=(50,0));status=idle(service)
    assert status['last_error'] and not status['localized']
    assert len(service.hardware.moves)==before


def test_route_corrects_a_measured_heading_miss_before_forward_motion(service):
    service.start_scan();idle(service)
    service.pose=Pose(theta_deg=-45)
    move=service.hardware.move;turns=[]
    def under_turn(spec,*args,**kwargs):
        if spec['mode']=='turn':
            turns.append(spec['angle_deg'])
            if len(turns)==1:spec={**spec,'angle_deg':spec['angle_deg']-10}
        return move(spec,*args,**kwargs)
    service.hardware.move=under_turn
    service.navigate(goal=(20,0));status=idle(service)
    assert not status['last_error'],status
    assert len(turns)==2 and turns[1]==pytest.approx(10)
    assert math.dist(service._position(),(20,0))<8


@pytest.mark.parametrize('body',[{'goal':None},{'goal':{'x_cm':True,'y_cm':0}},
                                {'goal':{'x_cm':float('inf'),'y_cm':0}},
                                {'goal':{'x_cm':50,'y_cm':0},'preview_id':'a'*32}])
def test_direct_drive_validates_destination_before_admission(service,body):
    app=Flask(__name__);install_error_handlers(app)
    app.register_blueprint(blueprint(SimpleNamespace(controller=lambda:service.controller)))
    assert app.test_client().post('/api/navigation/navigate',json=body).status_code==400
    assert not service.hardware.moves


def test_exploration_is_bounded_and_uses_frontier_coordinates(service):
    service.start_explore(max_steps=2,max_distance_cm=60)
    status=idle(service)
    assert not status['last_error'],status
    assert status['travel_cm']<=65
    assert status['explore_steps']<=2


def test_restart_restores_map_without_position_or_movement(service):
    service.start_scan();idle(service)
    hardware=FakeHardware()
    restored=NavigationService(service.controller,service.directory,hardware=hardware)
    status=idle(restored)
    assert status['cells']>0 and not status['localized'] and not status['active']
    assert not hardware.moves
    assert restored.grid.cells==service.grid.cells


def test_pickup_global_search_reacquires_new_place_without_polluting_map(service):
    service.start_scan();idle(service)
    service.hardware.actual=Pose(-60,80,67)
    service.invalidate_pose('Carried to another place')
    before=dict(service.grid.cells)
    assert service.grid.cells==before and not service.localized
    service.start_scan(relocalize=True);status=idle(service)
    assert status['localized'],status
    assert math.dist(service._position(),(-60,80))<12
    assert abs(angle(service.pose.theta_deg-67))<5


def test_external_motion_invalidates_previews_and_stop_cancels_planning(service):
    service.start_scan();idle(service);service.plan((50,0));idle(service)
    service.observe_motion(Intent('filming-shot',80))
    assert not service.localized and not service.preview
    with pytest.raises(ValidationError):service.navigate()


def test_lease_renews_without_releasing_motion_between_segments(service):
    # Force renewal from inside a worker; contender must still see one owner.
    observed=[]
    def work():
        previous=service.lease;service._renewed-=41;service._check()
        assert service.lease is not previous and not service.intent.is_set()
        with pytest.raises(BusyError):service.controller.orchestrator.acquire('motion',intent=Intent('other'),wait_s=0)
        observed.append(True)
    service._launch('preview',work)
    assert not idle(service)['last_error'] and observed


def test_noisy_scan_still_localizes_but_outlier_scan_is_not_inserted(service):
    grid=room();pose=Pose(40,70,49)
    rays=scan(pose)
    rays=[Ray(r.x_cm,r.y_cm,r.angle_deg,max(5,r.distance_cm+(3 if i%2 else -3)),r.hit) for i,r in enumerate(rays)]
    matched=Matcher(grid).search(rays,Pose(44,66,51),budget_s=15)
    assert matched['accepted']
    service.grid=grid;service.localized=True;service.pose=pose
    before=dict(grid.cells)
    service.hardware.scan=lambda *args:(pose,[Ray(0,0,i*4,20) for i in range(90)])
    service.start_scan();status=idle(service)
    assert not status['localized'] and status['last_error']
    assert grid.cells==before


def test_ambiguous_partial_scan_gets_one_capture_with_measured_frame_transform(service,monkeypatch):
    service.start_scan();idle(service)
    captures=[];searches=[]
    def capture(pose,*args):
        captures.append(pose)
        return pose.moved(0,88),scan(pose)
    service.hardware.scan=capture
    service.hardware.last_scan_metrics={'partial_coverage':True}
    def search(self,rays,*args,**kwargs):
        searches.append(list(rays))
        return {'pose':Pose(),'accepted':len(searches)>1,'score':.9,'margin':.02 if len(searches)==1 else .08,'reason':'Ambiguous'}
    monkeypatch.setattr(Matcher,'search',search)
    service.start_scan();status=idle(service)
    assert not status['last_error'] and status['localized']
    assert len(captures)==len(searches)==2
    assert len(searches[1])==2*len(searches[0])
    # The second scan begins 88 degrees later; corresponding world hits still
    # coincide after transforming its rays into the first capture's frame.
    first=searches[1][0].endpoint()
    corresponding=searches[1][len(searches[0])+68].endpoint()
    assert corresponding==pytest.approx(first)
    assert status['scan_metrics']['localization_extra_scans']==1


def test_second_ambiguous_capture_is_bounded_and_never_inserted(service,monkeypatch):
    service.start_scan();idle(service);before=dict(service.grid.cells)
    captures=[]
    original=service.hardware.scan
    def capture(*args):captures.append(True);return original(*args)
    service.hardware.scan=capture
    monkeypatch.setattr(Matcher,'search',lambda *args,**kwargs:{'pose':Pose(),'accepted':False,'score':.9,'margin':.02,'reason':'Still ambiguous'})
    service.start_scan();status=idle(service)
    assert len(captures)==2 and status['last_error']=='Still ambiguous'
    assert service.grid.cells==before and not status['localized']


def test_preview_token_binds_http_drive_to_exact_preview(service):
    app=Flask(__name__);install_error_handlers(app)
    app.register_blueprint(blueprint(SimpleNamespace(controller=lambda:service.controller)))
    client=app.test_client()
    service.start_scan();idle(service)
    first=client.post('/api/navigation/plan',json={'goal':{'x_cm':50,'y_cm':0}}).json
    idle(service)
    second=client.post('/api/navigation/plan',json={'goal':{'x_cm':60,'y_cm':0}}).json
    idle(service)
    before=len(service.hardware.moves)
    for body in ({},{'preview_id':first['preview_id']},{'preview_id':123}):
        assert client.post('/api/navigation/navigate',json=body).status_code==400
    assert len(service.hardware.moves)==before
    assert client.post('/api/navigation/navigate',json={'preview_id':second['preview_id']}).status_code==202
    assert not idle(service)['last_error']
    assert math.dist(service._position(),(60,0))<8
    assert client.post('/api/navigation/navigate',json={'preview_id':second['preview_id']}).status_code==400


def test_stop_discards_idle_preview_without_losing_localization(service):
    service.start_scan();idle(service);service.plan((50,0));idle(service)
    service.stop()
    assert service.localized and not service.preview and not service.route
    with pytest.raises(ValidationError):service.navigate()


def test_arrival_adds_one_localization_scan_after_continuous_travel(service):
    service.start_scan();idle(service);service.plan((50,0));idle(service)
    captures=service.hardware.moves.count({'scan':True})
    service.navigate();status=idle(service)
    assert not status['last_error']
    assert math.dist(service._position(),(50,0))<8
    assert service.hardware.moves.count({'scan':True})==captures+1
    assert not status['preview'] and not status['route']


def test_pose_invalidation_during_motion_acquisition_prevents_drive(service,monkeypatch):
    service.start_scan();idle(service);service.plan((50,0));idle(service)
    acquire=service.controller.orchestrator.acquire
    def acquire_and_invalidate(*args,**kwargs):
        lease=acquire(*args,**kwargs)
        service.invalidate_pose('Moved before ownership was acquired')
        return lease
    monkeypatch.setattr(service.controller.orchestrator,'acquire',acquire_and_invalidate)
    before=len(service.hardware.moves)
    service.navigate();status=idle(service)
    assert status['last_error'] and not status['localized']
    assert len(service.hardware.moves)==before
    assert not service.controller.orchestrator.snapshot()['resources']


def test_clearance_cache_reuses_only_same_map_revision_and_radius(service):
    observed=[]
    def work():
        service.grid=room()
        first=service._planner()
        assert service._planner() is first
        service.grid._apply(set(),{(10,0)})
        second=service._planner()
        assert second is not first and (10,0) not in second.safe
        service.pose.sigma_cm+=1
        third=service._planner()
        assert third is not second
        service.grid=Grid(cells=dict(service.grid.cells))
        assert service._planner() is not third
        service.intent.cancel('Stopped during cached planning')
        with pytest.raises(Halt):service._planner()
        observed.append(True)
    service._launch('preview',work);idle(service)
    assert observed


@pytest.mark.parametrize('residual',[4,12])
def test_measured_heading_must_keep_forward_segment_in_safe_space(service,residual):
    from collections import defaultdict
    service.grid=Grid(cells={(x,y):-10 for x in range(-30,31) for y in range(-30,31)})
    service.pose=Pose(theta_deg=90);service.localized=True
    service.settings.segment_cm=50
    planner=SimpleNamespace(route=lambda *args,**kwargs:[(0,0),(100,0)],radius=21,
                            visible=lambda a,b:b[1]==0,
                            clearance=defaultdict(lambda:100))
    service._planner=lambda:planner
    move=service.hardware.move
    def inaccurate(spec,*args,**kwargs):
        if spec['mode']=='turn':spec={**spec,'angle_deg':spec['angle_deg']+residual}
        return move(spec,*args,**kwargs)
    service.hardware.move=inaccurate
    service._launch('test_follow',lambda:service._follow_leg((100,0),200),physical=True)
    status=idle(service)
    assert all(spec.get('mode')!='distance' for spec in service.hardware.moves)
    assert ('Heading did not align' in status['last_error']) if residual>5 else not status['last_error']


@pytest.mark.parametrize('goal',[(float('nan'),0),(True,0),(0,float('inf')),(0,),None])
def test_direct_plan_rejects_invalid_coordinates(service,goal):
    with pytest.raises(ValidationError):service.plan(goal)
    assert not service.hardware.moves
