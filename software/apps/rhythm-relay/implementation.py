"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

import io

import math

import random

import time

from PIL import Image, ImageDraw, ImageFont

from robbi.app_runtime.permissions import AppPermissionRequired

from robbi.app_runtime.sdk import AppCancelled

from robbi.errors import ValidationError

_RNG = random.SystemRandom()

def _integer(value, lower, upper):
    return type(value) is int and lower <= value <= upper

def _state(context, game):
    context.request_permissions("state")
    state = context.read_state()
    if not state:
        return {"version": 1, "game": game, "round": 0, "score": 0, "solved": 0, "active": False}
    if (state.get("version") != 1 or state.get("game") != game or type(state.get("active")) is not bool
            or any(not _integer(state.get(key), 0, 1000000) for key in ("round", "score", "solved"))):
        raise ValidationError("Saved game is invalid. Restore a saved app-state backup.")
    return state

def _public(state):
    return {key: state[key] for key in ("game", "round", "score", "solved", "active")}

def _reply(context, state, text, data=None, *, changed=False, artifacts=None):
    context.checkpoint()
    if changed:
        state["updated_at"] = time.time()
        context.write_state(state)
    return {"ok": True, "text": text, "data": {**_public(state), **(data or {})}, **({"artifacts": artifacts} if artifacts else {})}

def _receipt(context, callback, *args, **kwargs):
    try:
        result = callback(*args, **kwargs)
        context.checkpoint()
        return result if isinstance(result, dict) else {}
    except (AppCancelled, AppPermissionRequired):
        raise
    except Exception:
        # Hardware/backend errors can name the secret expression asset. Keep
        # these out of public game text while preserving Stop/consent failures.
        context.checkpoint()
        return {}

def _stop(context, state):
    state["active"] = False
    state["outcome"] = "stopped"
    return _reply(context, state, "Game stopped. Your completed-round score is saved.", changed=True)

def _round_ready(state):
    if state["round"] >= 10000 or state["score"] >= 900000:
        raise ValidationError("This saved game has reached its progress limit. Archive its state before starting again.")

def _wait_until(context, instant):
    while time.monotonic() < instant:
        context.checkpoint()
        context.wait(min(.025, max(0, instant - time.monotonic())))
    context.checkpoint()

def _rhythm_intervals(level, bpm):
    count = min(6, level + 2)
    choices = (1,) if level == 1 else (.5, 1) if level == 2 else (.5, 1, 1.5)
    return [round(60 / bpm * _RNG.choice(choices), 4) for _ in range(count - 1)]

def _rhythm_state(state):
    if state["round"] == 0:
        return
    intervals = state.get("intervals")
    if (not _integer(state.get("level"), 1, 5) or state.get("mode") not in {"solo", "cooperative"}
            or not isinstance(intervals, list) or not 2 <= len(intervals) <= 5
            or any(type(v) not in {int, float} or not math.isfinite(v) or not .15 <= v <= .9 for v in intervals)
            or sum(intervals) > 4.51 or not _integer(state.get("misses", 0), 0, 1)
            or not _integer(state.get("attempts", 0), 0, 1000)
            or not _integer(state.get("next_level", state.get("level")), 1, 5)
            or state.get("outcome") not in {"ready", "try_again", "matched", "stopped"}):
        raise ValidationError("Saved rhythm is invalid. Restore a saved app-state backup.")

def _rhythm_data(state):
    intervals = state.get("intervals", [])
    return {"level": state.get("level", 1), "mode": state.get("mode", "solo"),
            "intervals_s": intervals[:], "claps": len(intervals) + 1 if intervals else 0,
            "attempts": state.get("attempts", 0), "last_timing_score": state.get("last_timing_score"),
            "outcome": state.get("outcome", "not_started"),
            "players": [1 + i % 2 for i in range(len(intervals) + 1)] if state.get("mode") == "cooperative" else []}

def _rhythm_image(state):
    canvas = Image.new("RGB", (280, 240), "#101b2b")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=18)
    small = ImageFont.load_default(size=13)
    draw.text((140, 22), "RHYTHM RELAY", anchor="mm", font=font, fill="#e8faf6")
    draw.text((140, 51), f"Round {state['round']}  /  Level {state['level']}", anchor="mm", font=small, fill="#a4b5c9")
    total = sum(state["intervals"])
    times = [0]
    for interval in state["intervals"]:
        times.append(times[-1] + interval)
    draw.line((25, 111, 255, 111), fill="#5a6c81", width=3)
    for index, moment in enumerate(times):
        x = 25 + round(230 * moment / total)
        colour = "#f6bd60" if state["mode"] == "cooperative" and index % 2 else "#59d3c0"
        draw.ellipse((x - 10, 101, x + 10, 121), fill=colour)
        draw.text((x, 140), str(index + 1), anchor="mm", font=small, fill="#e8faf6")
    draw.text((140, 180), "Players alternate claps" if state["mode"] == "cooperative" else "Copy the gaps between claps", anchor="mm", font=small, fill="#e8faf6")
    draw.text((140, 208), "Tap here or listen on Robbi", anchor="mm", font=small, fill="#a4b5c9")
    stream = io.BytesIO()
    canvas.save(stream, format="PNG")
    return stream.getvalue()

def _play_rhythm(context, state):
    sound, screen = context.config.get("sound", True), context.config.get("screen", True)
    context.request_permissions(*(["robot.audio"] if sound else []), *(["robot.display"] if screen else []))
    artifacts, displayed, played = [], False, False
    if screen:
        artifact = context.save_image(_rhythm_image(state), "Rhythm relay beat timeline")
        artifacts.append(artifact)
        displayed = _receipt(context, context.robot.show_image, artifact["id"], hold_s=60).get("displayed") is True
    if sound:
        due = time.monotonic()
        played = True
        for index in range(len(state["intervals"]) + 1):
            _wait_until(context, due)
            if _receipt(context, context.robot.play_sound_file, "assets/clap.wav").get("ok") is not True:
                played = False
                break
            if index < len(state["intervals"]):
                due += state["intervals"][index]
    return {"displayed": displayed, "display_hold_s": 60 if displayed else 0, "audio_played": played, "audio_requested": sound}, artifacts

def _tap_times(value, *, maximum=16, bound=15000):
    if not isinstance(value, list) or not 2 <= len(value) <= maximum:
        raise ValidationError(f"Supply 2–{maximum} tap times in milliseconds, starting at zero or later.")
    if any(type(v) not in {int, float} or not math.isfinite(v) or not 0 <= v <= bound for v in value):
        raise ValidationError("Tap times must be finite milliseconds within the recording window.")
    if any(b <= a for a, b in zip(value, value[1:])):
        raise ValidationError("Tap times must be strictly increasing; do not submit intervals as tap times.")
    return value

def _score_rhythm(context, state, times):
    observed = [(b - a) / 1000 for a, b in zip(times, times[1:])]
    expected = state["intervals"]
    exact_count = len(observed) == len(expected)
    error = sum(abs(a - b) / b for a, b in zip(observed, expected)) / len(expected) if exact_count else 1
    score = max(0, min(100, round(100 * (1 - 2 * error))))
    passed = exact_count and score >= 80
    state["attempts"] += 1
    state["last_timing_score"] = score
    if passed:
        state.update(active=False, outcome="matched", misses=0, solved=state["solved"] + 1,
                     score=state["score"] + score, next_level=min(5, state["level"] + 1))
        text = f"Rhythm matched: {score}/100. " + ("You completed it together! " if state["mode"] == "cooperative" else "") + "Start the next round when ready."
    else:
        misses = state.get("misses", 0) + 1
        state.update(outcome="try_again", misses=misses % 2)
        if misses == 2:
            state["next_level"] = max(1, state["level"] - 1)
        text = (f"I counted {len(times)} claps; this pattern needs {len(expected) + 1}. " if not exact_count else f"Timing score: {score}/100. ")
        text += "Try the same pattern again, or start a fresh round."
    return _reply(context, state, text, {**_rhythm_data(state), "matched": passed, "timing_score": score,
                                      "detected_claps": len(times)}, changed=True)

def rhythm_relay(context, tool_name, arguments):
    state = _state(context, "rhythm-relay")
    _rhythm_state(state)
    action = arguments.get("action", "status")
    if action == "stop":
        return _stop(context, state)
    if action == "start":
        _round_ready(state)
        level = state.get("next_level", state.get("level", context.config.get("level", 1)))
        state.update(round=state["round"] + 1, active=True, level=level, outcome="ready", attempts=0,
                     intervals=_rhythm_intervals(level, context.config.get("bpm", 120)),
                     mode=context.config.get("mode", "solo"), last_timing_score=None, misses=0)
        playback, artifacts = _play_rhythm(context, state)
        text = "Copy the gaps between the claps. "
        if state["mode"] == "cooperative":
            text += "Player 1 takes odd-numbered claps; player 2 takes even-numbered claps. "
        text += "Use dashboard taps, or choose Listen to capture claps on Robbi."
        if playback["audio_requested"] and not playback["audio_played"]:
            text += " Robot sound is unavailable; the dashboard can demonstrate the pattern."
        return _reply(context, state, text, {**_rhythm_data(state), **playback}, changed=True, artifacts=artifacts)
    if action == "status":
        return _reply(context, state, "A saved pattern is ready." if state["active"] else "Start a rhythm round.", _rhythm_data(state))
    if not state["active"]:
        return _reply(context, state, "Start a rhythm round first.", {"accepted": False})
    if action == "replay":
        playback, artifacts = _play_rhythm(context, state)
        return _reply(context, state, "Pattern replayed." if playback["audio_played"] else "Use the beat timeline or dashboard demonstration.",
                      {**_rhythm_data(state), **playback}, artifacts=artifacts)
    if state["attempts"] >= 1000:
        return _reply(context, state, "This pattern has reached its attempt limit. Start another round.", {"accepted": False})
    if action == "attempt":
        return _score_rhythm(context, state, _tap_times(arguments.get("tap_times_ms")))
    if action == "listen":
        context.request_permissions("robot.microphone", *(["robot.display"] if context.config.get("screen", True) else []))
        if context.config.get("screen", True):
            _receipt(context, context.robot.display_text, "YOUR TURN\nClap the pattern now", hold_s=8)
        duration = min(8, sum(state["intervals"]) + 2)
        captured = _receipt(context, context.robot.listen_claps, duration_s=duration)
        if captured.get("ok") is not True:
            return _reply(context, state, "Microphone capture is unavailable. Use dashboard taps instead; no attempt was scored.", {**_rhythm_data(state), "captured": False})
        times, actual_duration = captured.get("clap_times_s"), captured.get("duration_s")
        if (not isinstance(times, list) or not _integer(len(times), 0, 32)
                or type(actual_duration) not in {int, float} or not math.isfinite(actual_duration) or not 0 < actual_duration <= 8.5):
            return _reply(context, state, "The microphone returned invalid timing data. No attempt was scored.", {"captured": False})
        if len(times) < 2:
            return _reply(context, state, "Fewer than two claps were detected. Try again or use dashboard taps; no attempt was scored.", {**_rhythm_data(state), "captured": True, "detected_claps": len(times)})
        try:
            milliseconds = _tap_times([v * 1000 if type(v) in {int, float} else v for v in times], maximum=32, bound=actual_duration * 1000)
        except ValidationError:
            return _reply(context, state, "The microphone returned invalid timing data. No attempt was scored.", {"captured": False})
        return _score_rhythm(context, state, milliseconds)
    raise ValidationError("Unknown Rhythm Relay action")
