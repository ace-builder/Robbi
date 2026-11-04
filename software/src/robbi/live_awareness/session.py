"""One bounded live session. Network/media work stays off robot control threads."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from robbi.errors import BusyError, ValidationError

from . import gemini, openai_vision
from .errors import LiveProviderError, failure_reason

# WebSocket debug logging includes HTTP headers and media. Keep this transport
# silent even if the robot's root logger is set to DEBUG; UI errors are sanitized.
TRANSPORT_LOG = logging.getLogger("robbi.live_awareness.transport")
TRANSPORT_LOG.disabled = True


def options(payload):
    defaults = {
        "provider": "openai",
        "model": openai_vision.DEFAULT_MODEL,
        "duration_s": 60,
        "audio": False,
        "video": True,
        "react": False,
        "gestures": False,
    }
    if not isinstance(payload, dict) or set(payload) - defaults.keys():
        raise ValidationError("Choose provider, model, duration_s, audio, video, react and gestures only")
    provider = payload.get("provider", "gemini" if str(payload.get("model", "")).startswith(("gemini-", "models/gemini-")) else "openai")
    if provider not in ("openai", "gemini"):
        raise ValidationError("Choose OpenAI or Gemini")
    defaults.update(provider=provider, model=openai_vision.DEFAULT_MODEL if provider == "openai" else gemini.DEFAULT_MODEL)
    result = {**defaults, **payload}
    if not isinstance(result["model"], str) or not re.fullmatch(
        r"[A-Za-z0-9._/-]{1,120}", result["model"]
    ):
        raise ValidationError("Enter a valid model ID")
    if type(result["duration_s"]) is not int or not 15 <= result["duration_s"] <= 300:
        raise ValidationError("Session length must be 15–300 seconds")
    if any(type(result[k]) is not bool for k in ("audio", "video", "react", "gestures")):
        raise ValidationError("Audio, video, reactions and gestures must be true or false")
    if not result["audio"] and not result["video"]:
        raise ValidationError("Select camera, microphone, or both")
    if provider == "openai" and (result["audio"] or not result["video"]):
        raise ValidationError("OpenAI Terra awareness uses camera frames only. Select Gemini Live for microphone streaming.")
    if result["gestures"] and not result["react"]:
        raise ValidationError("Enable reactions before allowing small body gestures")
    return result


class AudioTap:
    """Capture only enqueues; never waits for cloud, a lock, or disk."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.accepting = False
        self._lock = threading.Lock()
        self._chunks = deque()
        self._bytes = 0
        self.dropped = 0

    def push(self, chunk):
        if not self.accepting or not self._lock.acquire(blocking=False):
            return
        try:
            if not self.accepting:
                return
            if len(chunk) > 16000 or len(chunk) % 2:
                self.dropped += 1
                return
            while self._chunks and self._bytes + len(chunk) > 16000:
                self._bytes -= len(self._chunks.popleft()[1])
                self.dropped += 1
            self._chunks.append((self.clock(), chunk))
            self._bytes += len(chunk)
        finally:
            self._lock.release()

    def drain(self):
        with self._lock:
            chunks, self._chunks = self._chunks, deque()
            self._bytes = 0
        now = self.clock()
        return b"".join(data for stamp, data in chunks if now - stamp <= 0.5)

    def clear(self):
        self.accepting = False
        self.drain()


class LiveSession:
    WINDOW_S = 3
    FRAME_INTERVAL_S = 1
    MAX_TURNS = 20
    MAX_SENT = 12 * 1024 * 1024
    MAX_RECEIVED = 8 * 1024 * 1024

    def __init__(
        self,
        *,
        key,
        frame,
        gate,
        audio_allowed,
        attach_audio,
        on_report,
        clear_evidence,
        connector=None,
        openai_key=lambda: "",
        openai_factory=None,
        clock=time.monotonic,
    ):
        self.key, self.frame, self.gate = key, frame, gate
        self.audio_allowed, self.attach_audio = audio_allowed, attach_audio
        self.on_report, self.clear_evidence = on_report, clear_evidence
        self.connector, self.clock = connector, clock
        self.openai_key, self.openai_factory = openai_key, openai_factory
        self._lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self._closed = False
        self._loop = self._task = None
        self._state = {
            "state": "off",
            "reason": "Start a timed experiment to connect",
            "observations": [],
        }
        self._tap = AudioTap(clock)
        # Exactly one camera read may be outstanding, including across sessions.
        self._frames = None
        self._frame_future = None

    def status(self):
        with self._lock:
            result = {**self._state, "observations": list(self._state["observations"])}
            result["running"] = bool(self._thread and self._thread.is_alive())
            result["audio_dropped"] = self._tap.dropped
            result["providers"] = self._providers()
            selected = self._state.get("options", {}).get("provider", "openai")
            result.update({k: result["providers"][selected][k] for k in ("key_configured", "dependency_available")})
            result["remaining_s"] = (
                max(0, round(self._deadline - self.clock())) if result["running"] else 0
            )
            return result

    def _providers(self):
        return {
            "openai": {"key_configured": bool(self.openai_key()),
                       "dependency_available": self.openai_factory is not None or importlib.util.find_spec("openai") is not None,
                       "default_model": openai_vision.DEFAULT_MODEL},
            "gemini": {"key_configured": bool(self.key()),
                       "dependency_available": self.connector is not None or importlib.util.find_spec("websockets") is not None,
                       "default_model": gemini.DEFAULT_MODEL},
        }

    def indicator(self):
        with self._lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "state": self._state["state"],
            }

    def _update(self, **values):
        with self._lock:
            self._state.update(values)

    def start(self, payload):
        config = options(payload)
        with self._lock:
            if self._closed:
                raise BusyError("Live awareness is shutting down")
            if self._thread and self._thread.is_alive():
                raise BusyError("Stop the current experiment first")
            provider = self._providers()[config["provider"]]
            if not provider["key_configured"]:
                raise ValidationError(
                    f"Save an {config['provider']} API key in Settings → Connections first"
                )
            if not provider["dependency_available"]:
                raise ValidationError(
                    "Install Robbi's provider dependency: pip install '.[providers]'" if config["provider"] == "openai" else "Install Robbi's live dependency: pip install '.[live]'"
                )
            reason = self.gate()
            if reason:
                raise BusyError(reason)
            self.clear_evidence()
            self._config = config
            self._stop = threading.Event()
            self._tap = AudioTap(self.clock)
            self._deadline = self.clock() + config["duration_s"]
            self._state = {
                "state": "connecting",
                "reason": "Connecting to " + ("OpenAI camera awareness" if config["provider"] == "openai" else "Gemini Live"),
                "options": config,
                "observations": [],
                "frames": 0,
                "audio_bytes": 0,
                "sent_bytes": 0,
                "received_bytes": 0,
                "turns": 0,
                "latency_ms": None,
                "usage": {},
            }
            self._thread = threading.Thread(
                target=self._run, name="live-awareness", daemon=True
            )
            try:
                self._thread.start()
            except BaseException:
                self._thread = None
                self._update(
                    state="failed", reason="Could not start live awareness worker"
                )
                raise
        return self.status()

    def stop(self, reason="Stopped by owner"):
        with self._lock:
            self._stop.set()
            self._tap.clear()
            self.clear_evidence()
            self._state.update(
                state="stopping" if self._thread and self._thread.is_alive() else "off",
                reason=reason,
            )
            loop, task = self._loop, self._task
            if loop is not None and task is not None:
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass

    def shutdown(self):
        with self._lock:
            self._closed = True
        self.stop("Robot shutting down")
        if self._frames:
            self._frames.shutdown(wait=False, cancel_futures=True)

    def _run(self):
        try:
            asyncio.run(self._session())
        except asyncio.CancelledError:
            pass
        except TimeoutError:
            if self.clock() >= self._deadline:
                self._update(reason="Timed experiment finished")
            else:
                self._update(
                    state="failed", reason="Live provider timed out; restart explicitly"
                )
        except Exception as exc:  # noqa: BLE001 -- quarantine arbitrary provider/transport errors
            # Provider exception text can include credentials, request bodies or audio.
            self._update(
                state="failed",
                reason=failure_reason(exc, self._config["provider"]),
            )
        finally:
            self._tap.clear()
            try:
                self.attach_audio(None)
            finally:
                try:
                    self.clear_evidence()
                finally:
                    with self._lock:
                        self._loop = self._task = None
                        if self._state["state"] != "failed":
                            self._state["state"] = "off"

    async def _send(self, ws, message):
        if self._stop.is_set() or self.clock() >= self._deadline or self.gate():
            raise asyncio.CancelledError()
        data = json.dumps(message, separators=(",", ":"))
        total = self._state["sent_bytes"] + len(data.encode())
        if total > self.MAX_SENT:
            self._update(reason="Session upload limit reached")
            raise asyncio.CancelledError()
        await asyncio.wait_for(ws.send(data), timeout=3)
        self._update(sent_bytes=total)

    async def _receive(self, ws, timeout=10):
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        total = self._state["received_bytes"] + len(raw)
        if len(raw) > 1024 * 1024 or total > self.MAX_RECEIVED:
            raise ValueError("Response limit exceeded")
        self._update(received_bytes=total)
        result = json.loads(raw)
        if isinstance(result, dict) and "error" in result:
            raise LiveProviderError(failure_reason(result["error"], self._config["provider"]))
        if not isinstance(result, dict) or "goAway" in result:
            raise ValueError("Provider session ended")
        return result

    async def _session(self):
        with self._lock:
            self._loop, self._task = asyncio.get_running_loop(), asyncio.current_task()
        if self._stop.is_set():
            return
        if self._config["provider"] == "openai":
            async with asyncio.timeout(max(.01, self._deadline - self.clock())):
                await openai_vision.observe(self)
            return
        connect = self.connector
        if connect is None:
            from websockets.asyncio.client import connect
        async with asyncio.timeout(max(0.01, self._deadline - self.clock())):
            async with connect(
                gemini.URL,
                additional_headers={"x-goog-api-key": self.key()},
                open_timeout=8,
                close_timeout=1,
                max_size=1024 * 1024,
                max_queue=4,
                compression=None,
                ping_interval=10,
                ping_timeout=5,
                logger=TRANSPORT_LOG,
            ) as ws:
                await self._send(ws, gemini.setup(self._config["model"]))
                if "setupComplete" not in await self._receive(ws):
                    raise ValueError("Live setup was not acknowledged")
                self.attach_audio(self._tap if self._config["audio"] else None)
                self._update(
                    state="streaming",
                    reason="Observing; robot actions use existing autonomy gates",
                )
                missed_reports = 0
                for turn in range(self.MAX_TURNS):
                    if self._stop.is_set() or self.gate():
                        break
                    reported = await self._window(ws)
                    if self._stop.is_set():
                        break
                    self._update(turns=turn + 1)
                    missed_reports = 0 if reported else missed_reports + 1
                    if missed_reports >= 2:
                        self._update(
                            state="failed",
                            reason="Live model returned no usable observations twice. Choose a Live model with function calling and restart.",
                        )
                        return
                self._update(
                    reason="Experiment finished (time, turn limit or robot activity); start again explicitly"
                )

    async def _window(self, ws):
        started = self.clock()
        next_frame = started
        sent_media = False
        sent_audio = False
        while self.clock() - started < self.WINDOW_S and not self._stop.is_set():
            if self.gate():
                self._update(reason="Robot activity stopped live awareness")
                raise asyncio.CancelledError()
            if self._config["video"] and self.clock() >= next_frame:
                next_frame = self.clock() + self.FRAME_INTERVAL_S
                frame = self._poll_frame()
                if frame:
                    await self._send(ws, gemini.media("video", frame))
                    sent_media = True
                    self._update(frames=self._state["frames"] + 1)
            allowed, reason = self.audio_allowed()
            self._tap.accepting = bool(self._config["audio"] and allowed)
            self._update(
                audio_status="streaming"
                if self._tap.accepting
                else reason or "Not selected"
            )
            chunk = self._tap.drain()
            if self._tap.accepting and chunk:
                if not sent_audio:
                    await self._send(ws, {"realtimeInput": {"activityStart": {}}})
                await self._send(ws, gemini.media("audio", chunk))
                sent_media = True
                sent_audio = True
                self._update(audio_bytes=self._state["audio_bytes"] + len(chunk))
            await asyncio.sleep(0.05)
        self._tap.clear()
        if not sent_media:
            self._update(
                reason="No fresh camera or microphone data; experiment stopped"
            )
            raise asyncio.CancelledError()
        if sent_audio:
            await self._send(ws, {"realtimeInput": {"activityEnd": {}}})
        else:
            # Manual speech boundaries without any audio fail Gemini's activity
            # precondition. Camera-only windows use an explicit content turn.
            await self._send(ws, {"clientContent": {"turns": [{"role": "user", "parts": [
                {"text": "Report this camera observation window with report_observation. No audio was supplied in this window; do not infer speech or a question."}
            ]}], "turnComplete": True}})
        ended = self.clock()
        accepted = False
        seen_ids = set()
        while self.clock() - ended < 12:
            message = await self._receive(
                ws, timeout=max(0.01, 12 - (self.clock() - ended))
            )
            calls = message.get("toolCall", {}).get("functionCalls", [])
            if not isinstance(calls, list) or len(calls) > 4:
                raise ValueError("Too many observation calls")
            responses = []
            for call in calls:
                if (
                    not isinstance(call, dict)
                    or not isinstance(call.get("id"), str)
                    or len(call["id"]) > 200
                ):
                    raise ValueError("Invalid observation ID")
                observation = gemini.grounded_report(gemini.report(call), audio=sent_audio)
                valid = bool(
                    observation and not accepted and call["id"] not in seen_ids
                )
                seen_ids.add(call["id"])
                if len(seen_ids) > 8:
                    raise ValueError("Too many tool calls")
                if valid and not self._stop.is_set() and not self.gate():
                    accepted = True
                    observation = {**observation, "at": time.time()}
                    with self._lock:
                        self._state["observations"] = [
                            *self._state["observations"],
                            observation,
                        ][-6:]
                        self._state["latency_ms"] = round((self.clock() - ended) * 1000)
                        self.on_report(observation, self._config["react"])
                responses.append(
                    {
                        "id": call["id"],
                        "name": str(call.get("name", ""))[:120],
                        "response": {"recorded": valid, "robot_action_executed": False},
                    }
                )
            if responses:
                await self._send(ws, {"toolResponse": {"functionResponses": responses}})
            usage = message.get("usageMetadata", {})
            if isinstance(usage, dict):
                self._update(
                    usage={
                        k: v
                        for k, v in usage.items()
                        if k
                        in {"promptTokenCount", "responseTokenCount", "totalTokenCount"}
                        and type(v) is int
                    }
                )
            if message.get("serverContent", {}).get("turnComplete"):
                return accepted
        raise TimeoutError("Observer did not finish")

    def _poll_frame(self):
        if self._frames is None:
            self._frames = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="live-camera"
            )
        result = None
        if self._frame_future is not None and self._frame_future.done():
            try:
                frame = self._frame_future.result()
                data = frame.get("data")
                if (
                    frame.get("ok")
                    and isinstance(data, bytes)
                    and 0 < len(data) <= 128 * 1024
                    and 0 <= self.clock() - frame.get("captured_monotonic", 0) <= 1.5
                ):
                    result = data
            except Exception:  # noqa: BLE001 -- optional camera driver boundary
                self._update(
                    camera_status="Camera snapshot failed; other inputs remain available"
                )
            self._frame_future = None
        if self._frame_future is None and not self._closed and not self._stop.is_set():
            self._frame_future = self._frames.submit(self.frame)
        return result
