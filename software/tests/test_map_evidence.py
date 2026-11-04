import math
import json
import struct
import threading
from types import SimpleNamespace as NS

import pytest

from robbi.engine.tof import decode_observation,ToFService
from robbi.navigation.evidence import confirmed_empty
from robbi.navigation.grid import Grid
from robbi.navigation.model import Pose,Ray,Settings,Halt
from robbi.navigation.planning import Planner
from robbi.navigation.storage import MapStore
from test_navigation import service,idle
from test_navigation_hardware import rig


def empty(sweep,angles=range(0,360,4),x=0,y=0):
    return [Ray(x,y,a,0,False,'empty',sweep) for a in angles]


@pytest.mark.parametrize('code,eligible,valid',[(4,True,False),(9,False,True),(6,False,False),(3,False,False),(5,False,False),(7,False,False)])
def test_sensor_status_keeps_no_target_separate_from_errors(code,eligible,valid):
    block=bytearray(15);block[0]=code;block[13:15]=struct.pack('>H',1234)
    result=decode_observation(block)
    assert result['empty_candidate'] is eligible
    assert result['distance_cm']==(123.4 if valid else None)


def test_two_independent_sweeps_make_white_space_to_200cm_without_wall_endpoints():
    assert not confirmed_empty(empty('a'))
    assert not confirmed_empty(empty('a')+empty('a'))
    rays=confirmed_empty(empty('a')+empty('b'))
    assert rays and all(r.source=='inferred_empty' and r.distance_cm==200 and not r.hit for r in rays)
    grid=Grid();grid.insert(Pose(),rays)
    assert grid.cells[30,0]<0 and grid.cells[40,0]<0 and (42,0) not in grid.cells
    assert grid.inferred_free and not any(v>=2 for v in grid.cells.values())
    planner=Planner(grid,12,allow_unknown_margin=True)
    assert (20,0) in planner.uncertain


def test_different_sensor_origins_and_disabled_inference_do_not_confirm():
    assert not confirmed_empty(empty('a',x=0)+empty('b',x=30))
    assert not confirmed_empty(empty('a')+empty('b'),0)


def test_real_return_vetoes_inference_in_its_direction():
    hit=Ray(0,0,0,70,True,'measured','b')
    rays=confirmed_empty(empty('a',[0,4])+empty('b',[0,4])+[hit])
    assert rays==[hit]


def test_isolated_hit_stays_tentative_then_disappears_when_contradicted():
    grid=Grid();grid.insert(Pose(),[Ray(0,0,0,100)])
    assert grid.cells[20,0]==1
    grid.insert(Pose(),confirmed_empty(empty('a',[0])+empty('b',[0])))
    assert grid.cells[20,0]<0 and (20,0) in grid.inferred_free


def test_actual_repeated_hit_confirms_but_same_sweep_duplicate_does_not():
    grid=Grid();a=Ray(0,0,0,100,True,'measured','a')
    grid.insert(Pose(),[a,a]);assert grid.cells[20,0]==1
    grid.insert(Pose(),[Ray(0,0,0,100,True,'measured','b')])
    assert grid.cells[20,0]>=2 and (20,0) in grid.confirmed_hits
    # A weak no-target inference must not erase this supported obstacle.
    grid.insert(Pose(),confirmed_empty(empty('c',[0])+empty('d',[0])))
    assert grid.cells[20,0]>=2 and (30,0) not in grid.cells


def test_sparse_wall_has_spatial_support_and_weak_rays_stop_at_it():
    grid=Grid();grid.insert(Pose(),[Ray(0,0,0,100),Ray(0,0,10,101.5)])
    assert grid.cells[20,0]>=2
    grid.insert(Pose(),confirmed_empty(empty('a',[0])+empty('b',[0])))
    assert grid.cells[20,0]>=2 and (30,0) not in grid.cells


def test_legacy_isolated_ghost_can_be_cleared_but_cluster_cannot():
    rays=confirmed_empty(empty('a',[0])+empty('b',[0]))
    ghost=Grid(cells={(20,0):6});ghost.insert(Pose(),rays)
    assert ghost.cells[20,0]<0
    wall=Grid(cells={(20,0):6,(20,1):6});wall.insert(Pose(),rays)
    assert wall.cells[20,0]>=2


def test_provenance_survives_saved_map_and_old_format_still_loads(tmp_path):
    grid=Grid();grid.insert(Pose(),confirmed_empty(empty('a')+empty('b')))
    store=MapStore(tmp_path);item=store.save(grid,'Open room',[],settings=Settings())
    restored,_=store.load(item['id'])
    assert restored.inferred_free==grid.inferred_free and restored.cells==grid.cells
    path=store.path(item['id']);old=json.loads(path.read_text(encoding='utf-8'))
    old['grid'].pop('inferred_free');old['grid'].pop('confirmed_hits')
    path.write_text(json.dumps(old),encoding='utf-8')
    restored,_=store.load(item['id'])
    assert restored.cells==grid.cells and not restored.inferred_free and not restored.confirmed_hits


def test_hardware_captures_fresh_empty_bearings_with_a_single_sweep_id(rig):
    hardware,intent,clock,motor,sensor=rig;sensor.no_return=True
    status=hardware.controller.tof.status
    hardware.controller.tof.status=lambda:{**status(),'empty_candidate':True}
    end,rays=hardware.scan(Pose(),intent,Settings(),lambda:None,lambda p:None,allow_sparse=True)
    assert len(rays)>=24 and len({r.sweep_id for r in rays})==1
    assert all(r.source=='empty' and not r.hit and r.distance_cm==0 for r in rays)
    # Those observations alone cannot write free space.
    grid=Grid();grid.insert(Pose(),rays)
    assert set(grid.cells)<={(0,0)} # Only Robbi's known footprint, not empty directions.


def test_first_map_can_confirm_an_open_room_without_fake_obstacles(service):
    count=[0]
    def scan(pose,*args,**kwargs):
        count[0]+=1
        return pose,empty(str(count[0]))
    service.hardware.scan=scan
    service.start_scan();status=idle(service)
    assert not status['last_error'] and status['localized']
    assert count[0]>=2 and service.grid.inferred_free
    assert all(v<2 for v in service.grid.cells.values())
    service.start_scan();status=idle(service)
    assert not status['last_error'] and service.match['mode']=='odometry_only'


def test_missing_connection_is_explored_automatically_and_keeps_original_goal(service):
    service.grid=Grid(cells={(x,y):-8 for x in range(-6,21) for y in range(-10,11)})
    service.pose=Pose();service.localized=True;scans=[]
    def scan(*args,**kwargs):
        scans.append(1)
        service.grid.cells.update({(x,y):-8 for x in range(0,41) for y in range(-10,11)})
        service.grid.revision+=1;service._snapshot()
    service._scan=scan
    service.navigate(goal=(150,0));status=idle(service)
    assert not status['last_error'] and 'Arrived' in status['message']
    assert abs(service.pose.x_cm-150)<8 and scans
    assert any(m.get('mode')=='distance' for m in service.hardware.moves)


def test_unknown_connection_keeps_odometry_and_does_not_pollute_the_map(service):
    service.start_scan();idle(service)
    before=dict(service.grid.cells)
    service.navigate(goal=(2900,2900));status=idle(service)
    assert not status['last_error'] and status['localized'],status
    assert service.grid.cells==before
    assert status['route_progress']['position_checks']==2


def test_stop_during_automatic_connection_search_does_not_resume_motion(service):
    service.grid=Grid(cells={(x,y):-8 for x in range(-6,21) for y in range(-10,11)})
    service.pose=Pose();service.localized=True
    entered=threading.Event();service.hardware.block=threading.Event()
    move=service.hardware.move
    def blocked(*args,**kwargs):
        entered.set();return move(*args,**kwargs)
    service.hardware.move=blocked
    service.navigate(goal=(150,0));assert entered.wait(2)
    service.stop();status=idle(service)
    assert status['last_error']=='Stopped by user' and not status['active']
    assert len(service.hardware.moves)==1 and not status['route']


def test_unknown_behind_a_wall_is_not_an_exploration_opening():
    cells={(x,y):(-8 if abs(x)<20 and abs(y)<20 else 6)
           for x in range(-20,21) for y in range(-20,21)}
    grid=Grid(cells=cells)
    assert not Planner(grid,16,allow_unknown_margin=True,escape_radius=12).frontiers((0,0))
    for x in range(16,21):
        for y in range(-6,7):grid.cells[x,y]=-8
    assert Planner(grid,16,allow_unknown_margin=True,escape_radius=12).frontiers((0,0))


def test_no_target_status_is_preserved_by_the_poller_in_cached_history(monkeypatch):
    sensor=NS(data_ready=True,ranging_observation=lambda:{'distance_cm':None,
              'raw_range_status':4,'empty_candidate':True},clear_interrupt=lambda:None)
    tof=ToFService();monkeypatch.setattr(tof,'_channel_enabled',lambda name:True)
    tof._poll_channel('h1',sensor)
    sample=tof.samples_since()['samples'][-1]
    assert sample['available'] and sample['empty_candidate'] and sample['raw_range_status']==4
    assert sample['distance_cm'] is None
    sensor.ranging_observation=lambda:{'distance_cm':None,'raw_range_status':6,'empty_candidate':False}
    tof._poll_channel('h1',sensor)
    assert tof.samples_since()['samples'][-1]['empty_candidate'] is False


def test_first_empty_sweep_cancellation_does_not_publish_free_space(service):
    captures=[]
    def scan(pose,*args,**kwargs):
        captures.append(1)
        if len(captures)==2:raise Halt('Stopped by user')
        return pose,empty('a')
    service.hardware.scan=scan
    service.start_scan();status=idle(service)
    assert status['last_error']=='Stopped by user' and not service.grid.cells
