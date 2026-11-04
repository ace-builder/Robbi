"""Real AppHost lifecycles; deterministic hardware fakes never record or move."""
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import wave

import pytest

from robbi.app_runtime.host import AppHost
from robbi.app_runtime.sdk import AppCancelled
from robbi.app_services import creative_games as games
from app_project_testing import patch_on_load, project_module, source_module

CASES = source_module("tiny-detective", "creative_game_data").CASES
CHARADES = source_module("robot-charades", "creative_game_data").CHARADES
GAME_IDS = {"rhythm-relay", "tiny-detective", "robot-charades"}
from robbi.errors import UnavailableError, ValidationError

ROOT = Path(__file__).resolve().parents[1]


class Robot:
    def __init__(self):
        self.cancelled = threading.Event()
        self.outputs = []
        self.claps = {"ok": True, "clap_times_s": [0, .5, 1], "duration_s": 3}
        self.before_output = None

    def capture_token(self):
        return 0, self.cancelled

    def output(self, kind, *data):
        self.outputs.append((kind, *data))
        if self.before_output:
            self.before_output(kind)
        return {"ok": True, "displayed": True}

    def show_image(self, path, token, cancellation, **kwargs):
        return self.output("screen", Path(path).name)

    def face_animation(self, name, token, cancellation, **kwargs):
        return self.output("face", name, kwargs)

    def play_sound(self, name, token, cancellation):
        return self.output("sound", str(name))

    def gesture(self, name, token, cancellation, **kwargs):
        return self.output("motion", name, kwargs)

    def listen_claps(self, duration_s, token, cancellation):
        self.output("microphone", duration_s)
        return self.claps


@pytest.fixture
def make_host(tmp_path, monkeypatch):
    # Timing accuracy is tested from submitted timestamps, not test-run latency.
    hosts = []

    def make(*, permissions=False):
        robot = Robot()
        host = AppHost(ROOT, tmp_path / str(len(hosts)), robot, permissions_enabled=permissions)
        hosts.append(host)
        patch_on_load(monkeypatch, host, {"rhythm-relay"}, _wait_until=lambda context, instant: context.checkpoint())
        host.scan()
        assert GAME_IDS <= host.packages.projects.keys()
        host.test_robot = robot
        return host

    yield make
    for host in hosts:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


def invoke(host, app, action, **arguments):
    return host.call(app, "play", {"action": action, **arguments}, include_invocation=True)


def state_path(host, app):
    return host.directory / "data" / app / "state.json"


def state(host, app):
    return json.loads(state_path(host, app).read_text(encoding="utf-8"))


def perfect_taps(intervals, offset=0):
    times = [offset]
    for interval in intervals:
        times.append(times[-1] + interval * 1000)
    return times


def select_card(monkeypatch, index, host):
    monkeypatch.setattr(project_module(host, "robot-charades"), "_RNG", SimpleNamespace(choice=lambda choices: index if index in choices else choices[0]))


@pytest.mark.parametrize("index", range(len(CASES)))
def test_all_detective_cases_have_playable_clues_hints_and_exact_deductions(make_host, index):
    host = make_host()
    app = "tiny-detective"
    case = CASES[index]
    started = invoke(host, app, "start", case=index + 1)
    assert started["ok"] and started["data"]["active"] and len(started["data"]["choices"]) == 3
    assert "answer" not in started["data"] and "explanation" not in started["data"]
    assert case["explanation"] not in json.dumps(host.invocation(started["invocation"]["id"]))
    for key, evidence in case["clues"]:
        result = invoke(host, app, "inspect", clue=key)
        assert evidence in result["text"] and "answer" not in result["data"]
    repeated = invoke(host, app, "inspect", clue=case["clues"][0][0])
    assert not repeated["invocation"]["undo"]["available"]
    hinted = invoke(host, app, "hint")
    assert hinted["data"]["hints"] == list(case["hints"][:1]) and "answer" not in hinted["data"]
    incorrect = invoke(host, app, "guess", answer="not " + case["answer"])
    assert not incorrect["data"]["accepted"] and incorrect["data"]["score"] == 0
    solved = invoke(host, app, "guess", answer="I guess " + case["answer"])
    assert solved["data"]["answer"] == case["answer"] and solved["data"]["explanation"] == case["explanation"]
    assert not solved["data"]["active"] and solved["data"]["score"] == 7
    assert len(json.dumps(solved)) < 8000
    assert invoke(host, app, "guess", answer=case["answer"])["data"]["score"] == 7


def test_detective_progress_skips_advance_and_replays_cannot_farm_points(make_host):
    host = make_host()
    app = "tiny-detective"
    invoke(host, app, "start", case=1)
    skipped = invoke(host, app, "skip")
    assert skipped["data"]["answer"] == CASES[0]["answer"] and skipped["data"]["completed_cases"] == 0
    assert invoke(host, app, "start")["data"]["case_number"] == 2
    invoke(host, app, "guess", answer=CASES[1]["answer"])
    invoke(host, app, "start", case=2)
    replay = invoke(host, app, "guess", answer=CASES[1]["answer"])
    assert replay["data"]["score"] == 10 and replay["data"]["completed_cases"] == 1
    assert "+0 points" in replay["text"]


@pytest.mark.parametrize("index", range(len(CHARADES)))
def test_every_charade_performs_without_motors_and_keeps_answer_private(make_host, monkeypatch, index):
    host = make_host()
    select_card(monkeypatch, index, host)
    app = "robot-charades"
    card = CHARADES[index]
    started = invoke(host, app, "start")
    assert started["ok"] and started["data"]["performance_available"]
    detail = host.invocation(started["invocation"]["id"])
    public = json.dumps(detail)
    assert card[3] not in public and '"card"' not in public and "answer" not in detail["result"]["data"]
    assert detail["arguments"].get("answer") == ""  # User-input default is not a hidden solution.
    assert {row[0] for row in host.test_robot.outputs} == {"face", "sound", "screen"}
    assert not started["data"]["motion_requested"]
    assert "answer" not in invoke(host, app, "hint")["data"]
    assert "answer" not in invoke(host, app, "hint")["data"]
    assert not invoke(host, app, "hint")["invocation"]["undo"]["available"]
    wrong = invoke(host, app, "guess", answer="not " + card[0])
    assert not wrong["data"]["accepted"] and wrong["data"]["score"] == 0
    solved = invoke(host, app, "guess", answer="I guess " + card[1][0])
    assert solved["data"]["answer"] == card[0] and solved["data"]["score"] == 2
    assert not solved["data"]["active"] and solved["data"]["solved"] == 1
    assert len(json.dumps(solved)) < 6000


def test_charades_deck_rotates_and_skip_reveals_without_awarding_score(make_host):
    host = make_host()
    host.configure("robot-charades", {"output": "text", "category": "action"})
    cards = []
    for _ in range(3):
        started = invoke(host, "robot-charades", "start")
        assert started["data"]["category"] == "action" and started["data"]["text_mode"]
        cards.append(state(host, "robot-charades")["card"])
        assert invoke(host, "robot-charades", "skip")["data"]["score"] == 0
    assert len(set(cards)) == 3 and not host.test_robot.outputs
    assert invoke(host, "robot-charades", "start")["ok"]


def test_charades_motion_is_explicit_and_owner_gated_before_any_output(make_host, monkeypatch):
    host = make_host(permissions=True)
    select_card(monkeypatch, 0, host)
    app = "robot-charades"
    host.set_permissions(app, {"state": "granted", "robot.display": "granted", "robot.audio": "granted"})
    host.configure(app, {"motion": True})
    denied = invoke(host, app, "start")
    assert denied["permission_required"] and denied["permissions"] == ["robot.motion"]
    assert not host.test_robot.outputs and not state_path(host, app).exists()
    host.set_permissions(app, {"robot.motion": "granted"})
    started = invoke(host, app, "start")
    assert started["data"]["motion_completed"]
    assert host.test_robot.outputs[-2] == ("motion", "tiny-nod", {"with_motion": True, "with_audio": False})
    assert host.test_robot.outputs[-1][0] == "screen"
    assert started["data"]["display_hold_s"] == 120


def test_charades_unavailable_hardware_has_honest_text_fallback_without_asset_names(make_host, monkeypatch):
    host = make_host()
    select_card(monkeypatch, 0, host)

    def unavailable(*args, **kwargs):
        raise UnavailableError("excited-grin hardware missing")

    monkeypatch.setattr(host.test_robot, "face_animation", unavailable)
    monkeypatch.setattr(host.test_robot, "play_sound", unavailable)
    result = invoke(host, "robot-charades", "start")
    assert result["ok"] and not result["data"]["performance_available"] and result["data"]["active"]
    assert "unavailable" in result["text"] and "excited-grin" not in json.dumps(result)


@pytest.mark.parametrize("category", ["animal", "job"])
def test_animal_and_job_categories_have_multiple_actual_playable_scenes(make_host, category):
    host = make_host()
    host.configure("robot-charades", {"category": category, "output": "text"})
    seen = set()
    for _ in range(2):
        result = invoke(host, "robot-charades", "start")
        card = CHARADES[state(host, "robot-charades")["card"]]
        assert result["data"]["category"] == category and "Acting scene" in result["data"]["scene"]
        seen.add(card[0])
        assert invoke(host, "robot-charades", "guess", answer=card[0])["data"]["outcome"] == "solved"
    assert len(seen) == 2


def test_every_charade_uses_existing_bounded_expression_and_sound_assets():
    for card in CHARADES:
        description = json.loads((ROOT / "assets/face_animations" / (card[3] + ".json")).read_text())
        assert (ROOT / "assets/face_animations" / description["sheet"]).is_file()
        assert sum(description["durations_ms"]) <= 5000
        with wave.open(str(ROOT / "assets/sounds" / (card[3] + ".wav")), "rb") as recording:
            assert 0 < recording.getnframes() / recording.getframerate() <= 5


def test_raw_backend_exception_cannot_leak_secret_asset_names(make_host, monkeypatch):
    host = make_host()
    select_card(monkeypatch, 0, host)

    def broken(*args, **kwargs):
        raise FileNotFoundError("private excited-grin.png")

    monkeypatch.setattr(host.test_robot, "face_animation", broken)
    monkeypatch.setattr(host.test_robot, "play_sound", broken)
    result = invoke(host, "robot-charades", "start")
    assert result["ok"] and not result["data"]["performance_available"]
    assert "excited-grin" not in json.dumps(host.invocation(result["invocation"]["id"]))


@pytest.mark.parametrize("app", ["robot-charades", "rhythm-relay", "tiny-detective"])
def test_cancelled_output_never_commits_new_round(make_host, app):
    host = make_host()
    host.test_robot.before_output = lambda kind: host.test_robot.cancelled.set()
    result = invoke(host, app, "start")
    assert result["cancelled"] and not state_path(host, app).exists()
    assert len(host.test_robot.outputs) == 1


def test_rhythm_manual_taps_score_offsets_and_adapt_level(make_host):
    host = make_host()
    app = "rhythm-relay"
    first = invoke(host, app, "start")
    assert first["data"]["claps"] == 3 and first["data"]["audio_played"] and first["data"]["displayed"]
    assert len(host.test_robot.outputs) == 4  # One timeline plus three generated claps.
    result = invoke(host, app, "attempt", tap_times_ms=perfect_taps(first["data"]["intervals_s"], 123))
    assert result["data"]["timing_score"] == 100 and result["data"]["matched"]
    assert result["data"]["score"] == 100 and not result["data"]["active"]
    assert invoke(host, app, "start")["data"]["level"] == 2
    assert not any(row[0] == "microphone" for row in host.test_robot.outputs)


def test_rhythm_cooperative_alternation_and_two_misses_reduce_next_level(make_host):
    host = make_host()
    app = "rhythm-relay"
    host.configure(app, {"mode": "cooperative", "level": 4, "sound": False, "screen": False})
    first = invoke(host, app, "start")
    assert first["data"]["players"] == [1, 2, 1, 2, 1, 2]
    for _ in range(2):
        missed = invoke(host, app, "attempt", tap_times_ms=[0, 50])
        assert not missed["data"]["matched"] and missed["data"]["timing_score"] == 0
    assert invoke(host, app, "start")["data"]["level"] == 3
    assert not host.test_robot.outputs


@pytest.mark.parametrize("level", range(1, 6))
def test_all_rhythm_levels_fit_bounded_recording_and_distinct_claps(level):
    for bpm in (100, 120, 160):
        intervals = source_module("rhythm-relay")._rhythm_intervals(level, bpm)
        assert 2 <= len(intervals) <= 5 and sum(intervals) <= 4.5
        assert all(.18 <= value <= .9 for value in intervals)


@pytest.mark.parametrize("times", [[0], [0, 0], [1, 0], [0, float("nan")], [0, float("inf")], [0, True], [0, 15001], [0, "500"]])
def test_invalid_timestamps_are_not_scored(times):
    with pytest.raises(ValidationError):
        source_module("rhythm-relay")._tap_times(times)


def test_rhythm_microphone_requires_explicit_permission_and_real_returned_timings(make_host):
    host = make_host(permissions=True)
    app = "rhythm-relay"
    host.configure(app, {"sound": False, "screen": False})
    host.set_permissions(app, {"state": "granted"})
    invoke(host, app, "start")
    before = state_path(host, app).read_bytes()
    blocked = invoke(host, app, "listen")
    assert blocked["permission_required"] and blocked["permissions"] == ["robot.microphone"]
    assert not host.test_robot.outputs and state_path(host, app).read_bytes() == before
    host.set_permissions(app, {"robot.microphone": "granted"})
    result = invoke(host, app, "listen")
    assert result["data"]["matched"] and result["data"]["timing_score"] == 100
    assert host.test_robot.outputs == [("microphone", 3.0)]


@pytest.mark.parametrize("capture", [{"ok": False}, {"ok": True, "clap_times_s": [], "duration_s": 3},
                                     {"ok": True, "clap_times_s": [.5], "duration_s": 3},
                                     {"ok": True, "clap_times_s": [1, .5], "duration_s": 3},
                                     {"ok": True, "clap_times_s": [0, 99], "duration_s": 3},
                                     {"ok": True, "clap_times_s": [0, .5], "duration_s": "3"}])
def test_failed_empty_or_malformed_capture_never_fabricates_a_score(make_host, capture):
    host = make_host()
    app = "rhythm-relay"
    host.configure(app, {"sound": False, "screen": False})
    invoke(host, app, "start")
    before = state_path(host, app).read_bytes()
    host.test_robot.claps = capture
    result = invoke(host, app, "listen")
    assert result["ok"] and "no attempt was scored" in result["text"].lower()
    assert state_path(host, app).read_bytes() == before


def test_stop_during_microphone_capture_preserves_previous_score(make_host):
    host = make_host()
    app = "rhythm-relay"
    host.configure(app, {"sound": False, "screen": False})
    invoke(host, app, "start")
    before = state_path(host, app).read_bytes()
    host.test_robot.before_output = lambda kind: host.test_robot.cancelled.set()
    result = invoke(host, app, "listen")
    assert result["cancelled"] and state_path(host, app).read_bytes() == before
    assert [kind for kind, *_ in host.test_robot.outputs] == ["microphone"]


@pytest.mark.parametrize("app", sorted(GAME_IDS))
def test_state_undo_restores_progress_without_publishing_private_state(make_host, app):
    host = make_host()
    if app == "rhythm-relay":
        host.configure(app, {"sound": False, "screen": False})
    elif app == "tiny-detective":
        host.configure(app, {"screen": False})
    else:
        host.configure(app, {"output": "text"})
    started = invoke(host, app, "start")
    before = state_path(host, app).read_bytes()
    if app == "rhythm-relay":
        changed = invoke(host, app, "attempt", tap_times_ms=perfect_taps(started["data"]["intervals_s"]))
    else:
        changed = invoke(host, app, "hint")
    assert changed["invocation"]["undo"]["available"]
    undo = host.undo(changed["invocation"]["id"])
    assert undo["ok"] and state_path(host, app).read_bytes() == before
    assert '"card"' not in json.dumps(undo) and '"intervals"' not in json.dumps(undo)
    assert invoke(host, app, "stop")["data"]["active"] is False


def test_read_only_status_neither_replays_hardware_nor_mutates_state(make_host):
    host = make_host()
    for app in GAME_IDS:
        invoke(host, app, "start")
        outputs = len(host.test_robot.outputs)
        before = state_path(host, app).read_bytes()
        status = invoke(host, app, "status")
        assert not status["invocation"]["undo"]["available"]
        assert state_path(host, app).read_bytes() == before and len(host.test_robot.outputs) == outputs


def test_voice_routes_require_explicit_game_names_and_respect_disabled_apps(make_host):
    host = make_host()
    assert games.creative_game_request(host, "Play Rhythm Relay")["arguments"] == {"action": "start"}
    assert games.creative_game_request(host, "Tiny Detective inspect gate")["arguments"] == {"action": "inspect", "clue": "gate"}
    assert games.creative_game_request(host, "Robot Charades guess happy")["arguments"] == {"action": "guess", "answer": "happy"}
    for text in ("happy", "I guess sleepy", "stop", "listen", "I like robot charades", "rhythm relay guess 2"):
        assert games.creative_game_request(host, text) is None
    host.set_enabled("rhythm-relay", False)
    assert games.creative_game_request(host, "play rhythm relay") is None


def test_corrupt_private_game_state_is_rejected_instead_of_leaking(make_host):
    host = make_host()
    app = "robot-charades"
    invoke(host, app, "start")
    saved = state(host, app)
    saved["card"] = 1000
    saved["private_answer"] = "SECRET"
    state_path(host, app).write_text(json.dumps(saved))
    result = invoke(host, app, "status")
    assert not result["ok"] and "SECRET" not in json.dumps(result)
