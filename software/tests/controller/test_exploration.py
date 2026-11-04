from concurrent.futures import Future
import math
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from robot.config import settings
from robot.exploration import ExploreTarget, choose_target
from robot.slam import MappingHalted, OccupancyGrid, Pose2D, RangeSample, RangeScan, SlamService


def scan(distance=200):
    return RangeScan(0, Pose2D(), [RangeSample(a, distance, True) for a in range(0, 360, 10)])


def select(s, visited=()):
    return choose_target(s, Pose2D(), OccupancyGrid(), list(visited), 100, 15, 400)


def test_clear_space_plans_one_metre_and_prefers_forward():
    target = select(scan())
    assert target.distance_cm == 100
    assert target.heading_deg == 0


def test_wall_limits_distance_and_familiar_destination_is_penalized():
    assert select(scan(65)).distance_cm == 50
    assert abs(select(scan(), [(100, 0)]).heading_deg) >= 20
    assert select(scan(20)) is None


def test_missing_ranges_and_single_clear_ray_are_not_free_space():
    assert select(RangeScan(0, Pose2D(), [RangeSample(a, None, False) for a in range(360)])) is None
    s = scan(10)
    s.samples[0] = RangeSample(0, 400, True)
    assert select(s) is None
    s = scan()
    s.samples[1] = RangeSample(10, None, False)
    assert abs((select(s).heading_deg - 10 + 180) % 360 - 180) > 22


def test_unknown_ray_does_not_erase_map_obstacles():
    grid = OccupancyGrid()
    grid.update_ray(Pose2D(), 0, 50, True, 400)
    before = dict(grid.cells)
    grid.update_ray(Pose2D(), 0, None, False, 400)
    grid.update_ray(Pose2D(), 0, float("nan"), True, 400)
    assert grid.cells == before


class FakeMotion:
    def __init__(self):
        self.calls = []
        self.stops = 0
        self.result = {}
        self.pending = False
        self.future = None
        self.partial_cm = 7
        self.failure = None

    def move(self, **kwargs):
        self.calls.append(kwargs)
        self.future = Future()
        distance = kwargs.get("distance_cm", 0)
        angle = math.radians(kwargs.get("angle_deg", 0)) * 9.8 / 2
        self.result = {"ok": True, "final": {"left": distance - angle, "right": distance + angle}}
        if self.failure:
            self.result.update(ok=False, reason=self.failure)
        if not self.pending:
            self.future.set_result(None)
        return self.future

    def status(self):
        return {"running": False, "last_result": self.result}

    def stop(self):
        self.stops += 1
        if self.future and not self.future.done():
            self.result = {"ok": False, "reason": "stopped",
                           "final": {"left": self.partial_cm, "right": self.partial_cm}}
            self.future.set_result(None)

    def wheel_delta_cm_from_result(self, result):
        final = result.get("final")
        return (final["left"], final["right"]) if final else None

    def turn_angle_deg_from_wheel_cm(self, left, right):
        return math.degrees((right - left) / 9.8)


@pytest.fixture
def service(monkeypatch):
    for key, value in dict(slam_enabled=True, slam_explore_step_cm=100, slam_max_explore_steps=0,
                           slam_min_pose_confidence=0.45, slam_scan_match_enabled=False,
                           slam_drive_speed=40, slam_turn_speed=40, motion_obstacle_stop_cm=5,
                           slam_scan_mode="stepped").items():
        monkeypatch.setattr(settings, key, value)
    return SlamService(FakeMotion(), lambda: 200, lambda: (True, ""))


def test_long_leg_uses_short_guarded_moves_and_measured_odometry(service):
    assert service._travel_leg(100) == pytest.approx(100)
    assert [c["distance_cm"] for c in service.motion.calls] == [20] * 5
    assert service._pose.x_cm == pytest.approx(100)
    assert service.status()["leg_travelled_cm"] == 100


def test_front_clearance_shortens_leg_not_one_metre_into_wall(service):
    service._distance_getter = lambda: 65 - service._pose.x_cm
    assert service._travel_leg(100) == pytest.approx(50)
    assert [c["distance_cm"] for c in service.motion.calls] == [20, 20, 10]


@pytest.mark.parametrize("missing", [None, float("nan"), -1])
def test_no_motion_if_sensor_unavailable(service, missing):
    service._distance_getter = lambda: missing
    assert not service.start_explore()["ok"]
    with pytest.raises(MappingHalted, match="tof_unavailable"):
        service._travel_leg(100)
    assert not service.motion.calls


@pytest.mark.parametrize("reason", ["tof_unavailable", "obstacle_replan", "voice_recording", "stop_requested"])
def test_mid_move_guards_stop_and_preserve_partial_odometry(service, reason):
    service.motion.pending = True
    polls = [0]

    def tick(seconds):
        assert seconds == 0.05
        polls[0] += 1
        if reason == "tof_unavailable":
            service._distance_getter = lambda: None
        elif reason == "obstacle_replan":
            service._distance_getter = lambda: 10
        elif reason == "voice_recording":
            service._can_run_callback = lambda: (False, "voice_recording")
        else:
            service._stop_event.set()

    service._stop_event.wait = tick
    if reason == "obstacle_replan":
        assert service._move_forward(20) is False
    else:
        with pytest.raises(MappingHalted, match=reason):
            service._move_forward(20)
    assert polls[0] == 1
    assert service.motion.stops >= 1
    assert service._pose.x_cm == pytest.approx(7)


def test_encoder_fault_is_fatal_not_an_obstacle_replan(service):
    service.motion.failure = "encoder_stall"
    with pytest.raises(MappingHalted, match="encoder_stall"):
        service._move_forward(20)


def test_continuous_explore_rescans_and_continues_past_twenty_legs(service):
    service._perform_scan = Mock(side_effect=lambda **kwargs: scan())
    service._choose_explore_target = lambda s: ExploreTarget(0, 100, 1)
    service._turn_to_heading = lambda heading: True
    legs = []

    def travel(distance):
        legs.append(distance)
        if len(legs) == 25:
            service.stop()
        return distance

    service._travel_leg = travel
    service._explore_worker(max_steps=0)
    assert len(legs) == 25
    assert service._perform_scan.call_count == 25
    assert service.status()["last_stop_reason"] == "stop_requested"
    assert not service.status()["active"]


def test_bounded_exploration_scans_final_destination(service):
    service._perform_scan = Mock(side_effect=lambda **kwargs: scan())
    service._choose_explore_target = lambda s: ExploreTarget(0, 100, 1)
    service._turn_to_heading = lambda heading: True
    service._travel_leg = Mock(return_value=100)
    service._explore_worker(max_steps=2)
    assert service._travel_leg.call_count == 2
    assert service._perform_scan.call_count == 3
    assert service.status()["last_stop_reason"] == "step_limit_reached"


def test_low_confidence_and_no_progress_stop_exploration(service):
    service._perform_scan = lambda **kwargs: scan()
    service._pose.confidence = 0.2
    service._explore_worker(max_steps=0)
    assert service.status()["last_stop_reason"] == "low_confidence"
    assert not service.motion.calls
    service._pose.confidence = 1
    service._choose_explore_target = lambda s: ExploreTarget(0, 100, 1)
    service._turn_to_heading = lambda heading: True
    service._travel_leg = Mock(return_value=0)
    service._explore_worker(max_steps=0)
    assert service._travel_leg.call_count == 2
    assert service.status()["last_stop_reason"] == "no_progress"


def test_stepped_scan_uses_actual_headings_and_full_circle(service, monkeypatch):
    monkeypatch.setattr(settings, "slam_scan_step_deg", 90)
    service._sample_at_angle = lambda angle: RangeSample(angle, 100, True)
    calls = []

    def turn(heading):
        calls.append(heading)
        service._pose.theta_deg = (heading - 3) % 360
        return True

    service._turn_to_heading = turn
    result = service._perform_stepped_scan(update_map=False)
    assert [s.angle_deg for s in result.samples] == [0, 87, 177, 267]
    assert calls == [90, 180, 270, 0]


def test_scan_correction_rotates_rays_without_erasing_end_rotation(service, monkeypatch):
    monkeypatch.setattr(settings, "slam_scan_match_enabled", True)
    service._scans = [scan()]
    service._grid.cells[(0, 0)] = -1
    service._pose.theta_deg = 355
    start = Pose2D(theta_deg=0)
    current = RangeScan(0, start, [RangeSample(0, 100, True)])
    service._scan_match = lambda s: {"pose": Pose2D(5, 0, 8, 0.9, "scan_match"), "match": {"accepted": True}}
    service._finish_scan(current, start, True)
    assert service._pose.theta_deg == pytest.approx(3)
    assert current.samples[0].angle_deg == 8
    assert current.pose.x_cm == 5


def test_explore_api_rejects_malformed_limits_instead_of_running_forever(monkeypatch):
    monkeypatch.setitem(sys.modules, "robbi.engine.controller", SimpleNamespace(RobotController=object))
    path = Path(__file__).resolve().parents[2] / "src" / "robbi" / "engine" / "webapp.py"
    spec = importlib.util.spec_from_file_location("robbi.engine._mapping_web_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    controller = SimpleNamespace(slam_start_explore=Mock(return_value={"ok": True}))
    client = module.create_app(controller).test_client()
    for invalid in (-1, 101, 0.5, "oops", True, [0]):
        assert client.post("/slam/start_explore", json={"max_steps": invalid}).status_code == 400
    assert not controller.slam_start_explore.called
    assert client.post("/slam/start_explore", json={"max_steps": 0}).status_code == 200
    controller.slam_start_explore.assert_called_once_with(max_steps=0)


def test_sparse_continuous_scan_is_not_committed_before_fallback(service, monkeypatch):
    monkeypatch.setattr(settings, "slam_scan_mode", "continuous")
    repeated = RangeScan(0, Pose2D(), [RangeSample(0, 100, True) for _ in range(100)])
    service._perform_continuous_scan = Mock(return_value=repeated)
    service._perform_stepped_scan = Mock(return_value=scan())
    service._finish_scan = Mock()
    service._perform_scan(update_map=True)
    service._finish_scan.assert_not_called()
    service._perform_stepped_scan.assert_called_once_with(update_map=True)


def test_reset_does_not_clear_state_while_worker_is_still_stopping(service):
    service._thread = Mock()
    service._thread.is_alive.return_value = True
    service._pose.x_cm = 123
    result = service.reset()
    assert result["ok"] is False
    assert service._pose.x_cm == 123


def test_start_requires_encoders_and_idle_motion(service):
    service.motion.status = lambda: {"running": False, "available": True, "encoder_enabled": False}
    assert not service.start_explore(0)["ok"]
    service.motion.status = lambda: {"running": True, "available": True, "encoder_enabled": True}
    assert not service.start_explore(0)["ok"]
    assert not service.motion.calls


def test_preview_is_live_but_not_scan_match_evidence(service):
    service._begin_preview(36)
    service._preview_sample(Pose2D(), RangeSample(0, 100, True), 1)
    data = service.map_data()
    assert data["grid"]["cells"]
    assert not service._grid.cells
    assert data["status"]["scan_samples_collected"] == 1
    assert data["status"]["scan_samples_target"] == 36
    assert data["status"]["scan_preview"]
    service._finish_scan(scan(), Pose2D(), True)
    assert not service._preview_grid.cells
    assert not service.status()["scan_preview"]
    assert service._grid.cells


def test_mapping_passes_run_cancel_token_to_every_motion(service):
    service._turn_relative(10)
    service._move_forward(20)
    assert all(call["cancel_event"] is service._stop_event for call in service.motion.calls)


def test_stop_during_step_scan_never_starts_the_next_turn(service, monkeypatch):
    monkeypatch.setattr(settings, "slam_scan_step_deg", 10)
    turns = []
    service._turn_to_heading = turns.append

    def sample(angle):
        service.stop()
        return RangeSample(angle, 100, True)

    service._sample_at_angle = sample
    with pytest.raises(MappingHalted, match="stop_requested"):
        service._perform_stepped_scan(True)
    assert not turns
    assert not service._scans


def test_stepped_scan_rejects_empty_map_before_exploration(service, monkeypatch):
    monkeypatch.setattr(settings, "slam_scan_step_deg", 90)
    service._sample_at_angle = lambda angle: RangeSample(angle, None, False)
    service._turn_to_heading = lambda heading: True
    with pytest.raises(MappingHalted, match="scan_too_sparse"):
        service._perform_stepped_scan(True)
    assert not service._scans


def test_stationary_scan_waits_for_distinct_sensor_frames(service, monkeypatch):
    from robot import slam
    clock = [0.0]
    monkeypatch.setattr(slam.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(settings, "slam_sample_settle_s", 0)
    monkeypatch.setattr(settings, "slam_samples_per_angle", 3)
    monkeypatch.setattr(settings, "tof_poll_hz", 10)
    service._stop_event.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)

    def sensor():
        count = int((clock[0] + .000001) / .1)
        return dict(sample_count=count, available=True, age_s=0,
                    distance_cm=999 if count == 0 else 100+count)

    service._range_snapshot_getter = sensor
    sample = service._sample_at_angle(10)
    assert sample.valid
    assert sample.distance_cm == 102
    assert clock[0] >= .3


def test_frozen_tof_frame_halts_scan_instead_of_reusing_stale_reading(service, monkeypatch):
    from robot import slam
    clock = [0.0]
    monkeypatch.setattr(slam.time, "monotonic", lambda: clock[0])
    service._stop_event.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    service._range_snapshot_getter = lambda: dict(sample_count=1, available=True, age_s=0, distance_cm=100)
    with pytest.raises(MappingHalted, match="tof_unavailable"):
        service._sample_at_angle(10)
    assert clock[0] < 2


@pytest.mark.parametrize("score,confidence", [(0.1, 0.425), (0.2, 0.58), (1.5, 0.7)])
def test_scan_confidence_requires_evidence_and_ties_do_not_shift_pose(service, score, confidence):
    service._pose.confidence = 0.5
    service._grid.score_scan = Mock(return_value=score)
    result = service._scan_match(scan())
    assert result["pose"].confidence == pytest.approx(confidence)
    assert result["pose"].x_cm == result["pose"].y_cm == result["pose"].theta_deg == 0
