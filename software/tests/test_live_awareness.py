"""Real session composition with a local fake transport; no paid API calls."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from robbi.errors import BusyError, ValidationError
from robbi.live_awareness import gemini
from robbi.live_awareness.session import AudioTap, LiveSession, options
from robbi.services.attention import Attention
from test_autonomous_runtime import autonomy_host as _autonomy_host_fixture

autonomy_host = _autonomy_host_fixture


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"audio": 1},
        {"duration_s": True},
        {"duration_s": 301},
        {"audio": False, "video": False},
        {"model": "secret?key=oops"},
        {"api_key": "no"},
        {"react": "yes"},
    ],
)
def test_session_options_reject_invalid_or_secret_fields(payload):
    with pytest.raises(ValidationError):
        options(payload)


def test_default_session_cannot_act_and_has_no_audio():
    assert options({}) == {
        "provider": "openai",
        "model": "gpt-6.1-terra",
        "duration_s": 60,
        "audio": False,
        "video": True,
        "react": False,
        "gestures": False,
    }


def test_audio_buffer_is_bounded_fresh_and_never_waits_for_lock():
    now = [10.0]
    tap = AudioTap(lambda: now[0])
    tap.push(b"\0" * 100)
    assert not tap.drain()
    tap.accepting = True
    for _ in range(1000):
        tap.push(b"\0" * 640)
    assert len(tap.drain()) <= 16000 and tap.dropped > 0
    with tap._lock:
        tap.push(b"\0" * 640)  # would deadlock if the microphone waited
    tap.push(b"\0" * 640)
    now[0] += 0.6
    assert not tap.drain()
    tap.push(b"\0" * 640)
    tap.clear()
    assert not tap.drain() and not tap.accepting


def test_observations_do_not_execute_remote_fields_or_malformed_calls():
    call = {
        "name": "report_observation",
        "args": {
            "summary": "A ball rolled into view.",
            "changed": True,
            "salience": "medium",
        },
    }
    assert gemini.report(call)["changed"]
    for field, value in [
        ("changed", "true"),
        ("salience", {}),
        ("summary", ["not text"]),
        ("summary", "x" * 601),
    ]:
        assert gemini.report({**call, "args": {**call["args"], field: value}}) is None
    assert gemini.report({**call, "args": {**call["args"], "drive": "forward"}}) is None
    assert gemini.report({"name": "drive", "args": {}}) is None


class FakeWebSocket:
    def __init__(self, *, hang=False, malicious=False):
        self.messages = []
        self.pending = None
        self.hang, self.malicious = hang, malicious
        self.closed = False

    async def __aenter__(self):
        self.pending = asyncio.Queue()
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send(self, raw):
        message = json.loads(raw)
        self.messages.append(message)
        if "setup" in message:
            await self.pending.put({"setupComplete": {}})
        if ("activityEnd" in message.get("realtimeInput", {}) or message.get("clientContent", {}).get("turnComplete")) and not self.hang:
            args = {
                "summary": "A new object appeared.",
                "changed": True,
                "salience": "medium",
            }
            if self.malicious:
                args["drive"] = {"distance_cm": 1000}
            await self.pending.put(
                {
                    "toolCall": {
                        "functionCalls": [
                            {
                                "id": "observe-1",
                                "name": "report_observation",
                                "args": args,
                            }
                        ]
                    }
                }
            )
            # Native model audio never reaches Robbi's speaker.
            await self.pending.put(
                {
                    "serverContent": {
                        "modelTurn": {"parts": [{"inlineData": {"data": "AAAA"}}]},
                        "turnComplete": True,
                    },
                    "usageMetadata": {"totalTokenCount": 123, "not_public": "private"},
                }
            )

    async def recv(self):
        return json.dumps(await self.pending.get())


def session(ws=None, **changes):
    ws = ws or FakeWebSocket()
    reports, taps = [], []
    args = {
        "key": lambda: "fake-key-do-not-log",
        "frame": lambda: {
            "ok": True,
            "data": b"jpeg",
            "captured_monotonic": time.monotonic(),
        },
        "gate": lambda: "",
        "audio_allowed": lambda: (True, ""),
        "attach_audio": taps.append,
        "on_report": lambda value, react: reports.append((value, react)),
        "clear_evidence": lambda: None,
        "connector": lambda *a, **k: ws,
    }
    args.update(changes)
    live = LiveSession(**args)
    live.WINDOW_S = 0.12
    live.FRAME_INTERVAL_S = 0.02
    live.MAX_TURNS = 2
    return live, ws, reports, taps


def finish(live, timeout=3):
    live._thread.join(timeout)
    assert not live._thread.is_alive(), live.status()


def test_real_session_streams_bounded_frames_validates_tools_and_closes():
    live, ws, reports, taps = session()
    try:
        assert not live.indicator()["running"]
        live.start({"provider": "gemini",})
        finish(live)
        status = live.status()
        assert status["state"] == "off" and status["turns"] == 2
        assert status["frames"] > 0 and status["audio_bytes"] == 0
        assert len(reports) == 2 and all(not react for _, react in reports)
        assert status["usage"] == {"totalTokenCount": 123}
        assert ws.closed and taps[-1] is None
        assert "fake-key" not in json.dumps(status)
        assert next(iter(ws.messages[0])) == "setup"
        responses = [
            m["toolResponse"]["functionResponses"][0]
            for m in ws.messages
            if "toolResponse" in m
        ]
        assert all(
            r["response"] == {"recorded": True, "robot_action_executed": False}
            for r in responses
        )
    finally:
        live.shutdown()


def test_malformed_observation_cannot_reach_attention():
    live, ws, reports, _ = session(FakeWebSocket(malicious=True))
    try:
        live.start({"provider": "gemini","react": True})
        finish(live)
        assert not reports
        assert live.status()["state"] == "failed"
        assert "no usable observations" in live.status()["reason"]
        assert any(
            not m["toolResponse"]["functionResponses"][0]["response"]["recorded"]
            for m in ws.messages
            if "toolResponse" in m
        )
    finally:
        live.shutdown()


def test_stop_cancels_hung_provider_without_waiting_or_restarting():
    live, ws, reports, _ = session(FakeWebSocket(hang=True))
    try:
        live.start({"provider": "gemini",})
        deadline = time.monotonic() + 2
        while (
            not any("activityEnd" in m.get("realtimeInput", {}) or m.get("clientContent", {}).get("turnComplete") for m in ws.messages)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        with pytest.raises(BusyError):
            live.start({"provider": "gemini",})
        live.stop()
        finish(live, 1)
        assert ws.closed and not reports and live.status()["state"] == "off"
        assert live._tap.drain() == b""
    finally:
        live.shutdown()
    with pytest.raises(BusyError):
        live.start({"provider": "gemini",})


def test_missing_key_blocked_gate_and_optional_dependency_do_not_break_startup():
    live, _, _, _ = session(key=lambda: "")
    assert not live.status()["key_configured"]
    with pytest.raises(ValidationError):
        live.start({"provider": "gemini",})
    live, _, _, _ = session(gate=lambda: "Filming mode is active")
    with pytest.raises(BusyError, match="Filming"):
        live.start({"provider": "gemini",})
    assert live._thread is None


def test_provider_exception_never_leaks_secrets_or_media():
    def connect(*args, **kwargs):
        raise RuntimeError("fake-key-do-not-log and camera content")

    live, _, _, _ = session(connector=connect)
    live.start({"provider": "gemini",})
    finish(live)
    assert live.status()["state"] == "failed"
    assert "fake-key" not in json.dumps(live.status())
    live.shutdown()


def test_pending_camera_capture_does_not_build_queue():
    release = threading.Event()
    live, _, _, _ = session(frame=lambda: release.wait(2))
    try:
        for _ in range(1000):
            assert live._poll_frame() is None
        assert live._frames._work_queue.qsize() <= 1
    finally:
        release.set()
        live.shutdown()


def test_live_evidence_expires_is_ephemeral_and_ignores_timestamp_novelty(tmp_path):
    clock = [100.0]
    attention = Attention(tmp_path / "attention.json", clock=lambda: clock[0])
    item = {
        "summary": "A ball rolled into view.",
        "changed": True,
        "salience": "medium",
    }
    attention.observe_live(item, react=False)
    assert attention.live_evidence() is None
    attention.observe_live(item, react=True)
    evidence = attention.live_evidence()
    assert attention.claim({"live": evidence})
    clock[0] += 5
    attention.observe_live(item, react=True)
    assert attention.live_evidence() == evidence
    assert not attention.novel({"live": {**evidence, "at": clock[0]}})
    assert Attention(attention.path).live_evidence() is None
    clock[0] += 11
    assert attention.live_evidence() is None


def setup_attention(host, clock, tmp_path, answer):
    host.attention = Attention(tmp_path / "attention.json", clock=lambda: clock[0])
    host.model_answer = answer
    host.assets = SimpleNamespace(names=lambda category: ["curious.png"])
    host.attention.observe_live(
        {"summary": "A ball rolled into view.", "changed": True, "salience": "medium"},
        react=True,
    )


def test_live_reaction_uses_memory_and_existing_execution_not_remote_actions(
    autonomy_host, tmp_path
):
    host, clock = autonomy_host
    setup_attention(
        host,
        clock,
        tmp_path,
        {
            "initiative": {
                "kind": "express",
                "reason": "Notice the ball",
                "focus": "Watch the ball",
            },
            "face": "curious.png",
            "speak": "Do not speak for express",
            "drive": {"distance_cm": 50},
        },
    )
    host._run_heartbeat_turn()
    assert host.outputs == [{"face": "curious.png"}]
    assert not host.motion_calls and not host.memory_calls
    assert host.autonomy_policy.status()["model_calls"] == 1
    assert "curious.png" in host.model_calls[0][1]["tools_text"]
    assert "A ball rolled" in host.model_calls[0][1]["memory_text"]
    assert not host._live_reaction_active


def test_live_can_react_again_without_hour_long_checkin_cooldown(
    autonomy_host, tmp_path
):
    host, clock = autonomy_host
    setup_attention(
        host, clock, tmp_path, {"initiative": {"kind": "speak"}, "speak": "A ball! "}
    )
    host._run_heartbeat_turn()
    clock[0] += 9
    host.model_answer = {'initiative': {'kind': 'speak'}, 'speak': 'Now the ball is gone.'}
    host.attention.observe_live(
        {"summary": "The ball was picked up.", "changed": True, "salience": "medium"},
        react=True,
    )
    host._run_heartbeat_turn()
    assert len(host.outputs) == 2
    assert host.autonomy_policy.status()["model_calls"] == 2


def test_clear_live_during_inference_discards_late_response(autonomy_host, tmp_path):
    host, clock = autonomy_host
    setup_attention(host, clock, tmp_path, {})

    def ask(*args, **kwargs):
        host.attention.clear_live()
        return {"initiative": {"kind": "speak"}, "speak": "Too late"}

    host.conversation_service.ask = ask
    host._run_heartbeat_turn()
    assert not host.outputs and not host._live_reaction_active


def test_live_does_not_bypass_pause_or_daily_model_budget(autonomy_host, tmp_path):
    host, clock = autonomy_host
    setup_attention(
        host, clock, tmp_path, {"initiative": {"kind": "speak"}, "speak": "Forbidden"}
    )
    host.autonomy_policy.options = lambda: {"max_model_calls_per_day": 0}
    host._run_heartbeat_turn()
    assert not host.model_calls and not host.outputs
    host.autonomy_policy.pause()
    host._run_heartbeat_turn()
    assert not host.model_calls and not host.outputs


def test_live_clear_does_not_wait_for_a_journal_disk_write(tmp_path):
    attention = Attention(tmp_path / "attention.json")
    cleared = threading.Event()
    # Simulates another thread holding the journal lock across slow SD I/O.
    with attention._lock:
        worker = threading.Thread(
            target=lambda: (attention.clear_live(), cleared.set())
        )
        worker.start()
        immediate = cleared.wait(0.2)
    worker.join(1)
    assert immediate


def test_global_upload_limit_closes_before_sending_excess():
    live, ws, reports, _ = session()
    live.MAX_SENT = 20
    try:
        live.start({"provider": "gemini",})
        finish(live)
        assert not ws.messages and not reports
        assert live.status()["reason"] == "Session upload limit reached"
    finally:
        live.shutdown()


def test_live_http_controls_reuse_owner_boundary():
    from flask import Flask
    from robbi.live_awareness.web import blueprint
    from robbi.web import install_error_handlers

    live, _, _, _ = session()
    runtime_calls = []
    controller = SimpleNamespace(live_awareness=live, autonomy_status=dict)
    runtime = SimpleNamespace(
        controller=lambda: runtime_calls.append("controller") or controller
    )
    app = Flask(__name__)
    app.config["ROBBI_RUNTIME_CONFIG"] = SimpleNamespace(
        values={"server": {"auth_enabled": True}}
    )
    app.register_blueprint(blueprint(runtime))
    install_error_handlers(app)
    client = app.test_client()
    for method, path in [
        ("get", "/api/live-awareness"),
        ("post", "/api/live-awareness/start"),
        ("post", "/api/live-awareness/stop"),
    ]:
        assert getattr(client, method)(path, json={}).status_code == 403
    assert not runtime_calls
    app.config["ROBBI_RUNTIME_CONFIG"].values["server"]["auth_enabled"] = False
    assert client.get("/api/live-awareness").status_code == 200
    assert (
        client.post("/api/live-awareness/start", json={"duration_s": 999}).status_code
        == 400
    )
    assert not live.indicator()["running"]
    assert client.post("/api/live-awareness/stop", json={}).status_code == 200
    live.shutdown()
