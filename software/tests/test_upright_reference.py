import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from robbi.dynamics import PickupDetector
from robbi.imu_reference import UprightReference
from robbi.imu_view import Alignment


def sample(vector, count, reference=None):
    return dict(fresh=True,calibration='complete',sample_count=count,
                acceleration_m_s2=vector,gyro_deg_s=[4,-8,2],gyro_bias_deg_s=[4,-8,2],
                upright_reference=reference)


def test_saved_axis_alignment_defines_upright_even_if_booted_on_side(tmp_path):
    # A PCB mounted upside down must not be treated as an upside-down robot.
    (tmp_path/'imu-view-alignment.json').write_text(json.dumps({'version':1,'mount_wxyz':[0,1,0,0]}))
    ref=UprightReference(tmp_path).snapshot()
    assert ref['gravity_unit']==pytest.approx([0,0,-1])
    detector=PickupDetector()
    for i in range(60):detector.update(sample([0,9.8,0],i,ref),now=i*.05)
    assert detector.picked_up
    for i in range(60,120):detector.update(sample([0,0,-9.8],i,ref),now=i*.05)
    assert not detector.picked_up and detector.angle==pytest.approx(0)


def test_setting_upright_recovers_latched_bad_startup_reference_and_survives_restart(tmp_path):
    detector=PickupDetector()
    detector.baseline=[0,0,1]
    detector.picked_up=True
    reference=UprightReference(tmp_path)
    ref=reference.save([5.15,-.11,-7.74])
    assert UprightReference(tmp_path).snapshot()==ref
    for i in range(60):detector.update(sample([5.15,-.11,-7.74],i,ref),now=i*.05)
    assert not detector.picked_up and detector.angle==pytest.approx(0,abs=1e-5)
    # A later real tilt still stops movement; calibration is not a bypass.
    for i in range(60,66):detector.update(sample([0,9.8,0],i,ref),now=i*.05)
    assert detector.picked_up


def test_corrupt_reference_cannot_replace_valid_axis_calibration(tmp_path):
    (tmp_path/'imu-upright.json').write_text('{bad')
    (tmp_path/'imu-view-alignment.json').write_text(json.dumps({'version':1,'mount_wxyz':[1,0,0,0]}))
    reference=UprightReference(tmp_path)
    assert reference.snapshot()['source']=='axis_calibration'
    with pytest.raises(ValueError):reference.save([float('nan'),0,1])
    assert reference.snapshot()['gravity_unit']==[0,0,1]


def test_upright_capture_uses_fresh_samples_and_releases_motion(monkeypatch,tmp_path):
    import robbi.imu_view as module
    now=[0.]
    monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    monkeypatch.setattr(module.time,'sleep',lambda n:now.__setitem__(0,now[0]+n))
    setter=Mock(return_value={'gravity_unit':[0,0,1]})
    c=SimpleNamespace(imu=SimpleNamespace(set_upright_reference=setter))
    service=Alignment(tmp_path/'alignment.json',SimpleNamespace(controller=lambda:c))
    service.start=lambda:{'id':'sample'}
    service.end=Mock()
    service.poll=lambda ident:{'imu':sample([0,0,9.8],round(now[0]*100))}
    assert service.upright()['ok']
    assert setter.call_args.args[0]==pytest.approx([0,0,9.8])
    service.end.assert_called_once_with('sample')


def test_moving_pose_is_not_saved(monkeypatch,tmp_path):
    service=Alignment(tmp_path/'alignment.json',Mock())
    service.start=lambda:{'id':'sample'}
    service.end=Mock()
    service.poll=lambda ident:{'imu':dict(sample([0,0,9.8],1),gyro_deg_s=[40,0,0])}
    with pytest.raises(ValueError,match='still'):service.upright()
    service.end.assert_called_once_with('sample')
