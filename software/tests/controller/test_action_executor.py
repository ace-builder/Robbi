from concurrent.futures import Future

from robot.action_executor import ActionExecutorMixin


class DummyMotion:
    def __init__(self):
        self.calls = []

    def move(self, **kwargs):
        self.calls.append(kwargs)
        future = Future()
        future.set_result(None)
        return future


class DummySlam:
    def __init__(self):
        self.tracked = []
        self.scans = 0

    def track_drive_action(self, drive, future):
        self.tracked.append((drive, future))

    def start_scan(self):
        self.scans += 1
        return {"ok": True}


class DummyExecutor(ActionExecutorMixin):
    def __init__(self):
        self.motion = DummyMotion()
        self.slam = DummySlam()
        self.presented = {}
        self.events = []

    def _set_presentation(self, **values):
        self.presented.update(values)

    def _publish_event(self, event_type, payload):
        self.events.append((event_type, payload))


def test_legacy_timed_action_becomes_sequence():
    executor = DummyExecutor()
    action = executor._expand_legacy_timed_action({"face": "happy", "speak": "Sure.", "animation": "dance"})

    assert action["sequence"] == [
        {"face": "happy", "speak": "Sure.", "wait_for_audio": True},
        {"animation": "dance"},
    ]


def test_sequence_wait_between_adjacent_speech_steps():
    executor = DummyExecutor()

    assert executor._sequence_wait_ms({"speak": "one"}, {"speak": "two"}) == 300
    assert executor._sequence_wait_ms({"speak": "one", "wait_ms": 50}, {"speak": "two"}) == 50
    assert executor._sequence_wait_ms({"face": "happy"}, {"speak": "two"}) == 0


def test_drive_action_uses_motion_and_tracks_slam():
    executor = DummyExecutor()
    drive = {"mode": "distance", "direction": "forward", "distance_cm": 10, "speed": 55}

    executor._execute_drive_action(drive)

    assert executor.motion.calls[0]["distance_cm"] == 10.0
    assert executor.motion.calls[0]["speed"] == 55
    assert executor.slam.tracked[0][0] is drive
    assert executor.presented["last_command"] == "drive:distance"


def test_slam_action_dispatches_scan():
    executor = DummyExecutor()

    executor._execute_slam_action({"action": "scan_room"})

    assert executor.slam.scans == 1
    assert executor.events[-1][0] == "tool.called"
