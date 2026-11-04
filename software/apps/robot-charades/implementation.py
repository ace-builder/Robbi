"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

import random

import re

import time

from robbi.app_runtime.permissions import AppPermissionRequired

from robbi.app_runtime.sdk import AppCancelled

from robbi.errors import ValidationError

from .creative_game_data import CHARADES

_RNG = random.SystemRandom()

def _clean(value):
    return " ".join(re.sub(r"[^\w\s]", " ", str(value).casefold()).split())

def _guess(value):
    value = _clean(value)
    return re.sub(r"^(?:my guess is|i guess|i think it is|i think|you are|it is|it s|guess)\s+", "", value)

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
    data = dict(data or {})
    # A neutral app-owned card stays visible after the transient performance.
    # Never put the hidden expression, scene, or asset name on this card.
    if (state["round"] and context.config.get("output", "both") in {"screen", "both"}
            and (changed or "performance_available" in data)):
        context.request_permissions("robot.display")
        prompt = "Your guess?" if state["active"] else "Round complete"
        card_text = f"{prompt}\nRound {state['round']} · Score {state['score']}\nHints used: {state.get('hints', 0)}"
        receipt = _receipt(context, context.robot.display_card, card_text,
                           title="Robot Charades", hold_s=120)
        data["displayed"] = receipt.get("displayed") is True
        data["display_hold_s"] = 120 if data["displayed"] else 0
        if isinstance(receipt.get("artifact"), dict):
            artifacts = [*(artifacts or []), receipt["artifact"]]
    return {"ok": True, "text": text, "data": {**_public(state), **data}, **({"artifacts": artifacts} if artifacts else {})}

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

def _charades_state(state):
    if not state["round"]:
        return
    if (not _integer(state.get("card"), 0, len(CHARADES) - 1) or not _integer(state.get("hints", 0), 0, 2)
            or not _integer(state.get("wrong", 0), 0, 100) or not isinstance(state.get("seen"), list)
            or len(state["seen"]) > len(CHARADES) or any(not _integer(v, 0, len(CHARADES) - 1) for v in state["seen"])
            or len(set(state["seen"])) != len(state["seen"]) or state["card"] not in state["seen"]
            or state.get("outcome") not in {"guessing", "solved", "skipped", "stopped"}):
        raise ValidationError("Saved charades round is invalid. Restore a saved app-state backup.")

def _charades_data(state):
    if not state["round"]:
        return {"cards_available": len(CHARADES)}
    answer, aliases, category, asset, gesture, scene, hints = CHARADES[state["card"]]
    data = {"category": category, "scene": scene, "hints": list(hints[:state["hints"]]),
            "wrong_guesses": state["wrong"], "outcome": state.get("outcome", "guessing"), "cards_available": len(CHARADES)}
    if state.get("outcome") in {"solved", "skipped"}:
        data["answer"] = answer
    return data

def _perform(context, state):
    card = CHARADES[state["card"]]
    mode = context.config.get("output", "both")
    screen, sound = mode in {"screen", "both"}, mode in {"sound", "both"}
    motion = context.config.get("motion", False) and card[4] is not None
    context.request_permissions(*(["robot.display"] if screen or motion else []),
                                *(["robot.audio"] if sound else []), *(["robot.motion"] if motion else []))
    shown, played, moved = False, False, False
    if screen:
        receipt = _receipt(context, context.robot.face_animation, card[3], fps=10, loops=1)
        frames = receipt.get("frames_displayed", 0)
        shown = receipt.get("ok") is True and (receipt.get("displayed") is True or _integer(frames, 1, 10000))
    if sound:
        played = _receipt(context, context.robot.play_sound, card[3]).get("ok") is True
    if motion:
        moved = _receipt(context, context.robot.gesture, card[4], with_motion=True, with_audio=False).get("ok") is True
    return {"displayed": shown, "audio_played": played, "motion_requested": motion, "motion_completed": moved,
            "performance_available": shown or played or moved, "text_mode": mode == "text"}

def robot_charades(context, tool_name, arguments):
    state = _state(context, "robot-charades")
    _charades_state(state)
    action = arguments.get("action", "status")
    if action == "stop":
        return _stop(context, state)
    if action == "start":
        _round_ready(state)
        category = context.config.get("category", "mixed")
        eligible = [i for i, card in enumerate(CHARADES) if category == "mixed" or card[2] == category]
        seen = state.get("seen", [])
        unseen = [i for i in eligible if i not in seen]
        if not unseen:
            seen, unseen = [], eligible
        selected = _RNG.choice(unseen)
        state.update(card=selected, seen=[*seen, selected], hints=0, wrong=0, active=True,
                     round=state["round"] + 1, outcome="guessing")
        performance = _perform(context, state)
        text = f"Guess the {CHARADES[selected][2]}. {CHARADES[selected][5]}"
        if not performance["performance_available"] and not performance["text_mode"]:
            text += " Robot output is unavailable; use this text scene or retry Perform."
        return _reply(context, state, text, {**_charades_data(state), **performance}, changed=True)
    if action == "status":
        return _reply(context, state, "Guess the expression, ask for a hint, or replay the performance." if state["active"] else "Start a charades round.", _charades_data(state))
    if not state["active"]:
        return _reply(context, state, "Start a charades round first.", {**_charades_data(state), "accepted": False})
    card = CHARADES[state["card"]]
    if action == "perform":
        return _reply(context, state, card[5], {**_charades_data(state), **_perform(context, state)})
    if action == "hint":
        changed = state["hints"] < 2
        state["hints"] = min(2, state["hints"] + 1)
        return _reply(context, state, card[6][state["hints"] - 1], _charades_data(state), changed=changed)
    if action == "skip":
        state.update(active=False, outcome="skipped")
        return _reply(context, state, f"The answer was {card[0]}. Start another round when ready.", _charades_data(state), changed=True)
    if action == "guess":
        answer = _guess(arguments.get("answer", ""))
        if not answer:
            return _reply(context, state, "Give a short guess for the feeling or action.", {**_charades_data(state), "accepted": False})
        if answer in {_clean(v) for v in (card[0], *card[1])}:
            points = max(1, 5 - state["hints"] - min(3, state["wrong"]))
            state.update(active=False, outcome="solved", score=state["score"] + points, solved=state["solved"] + 1)
            return _reply(context, state, f"Correct: {card[0]}! +{points} points. Start another round when ready.", _charades_data(state), changed=True)
        state["wrong"] = min(100, state["wrong"] + 1)
        return _reply(context, state, "Not quite. Watch or listen again, or ask for a hint.", {**_charades_data(state), "accepted": False}, changed=True)
    raise ValidationError("Unknown Robot Charades action")
