"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

import re

import time

from robbi.app_runtime.permissions import AppPermissionRequired

from robbi.app_runtime.sdk import AppCancelled

from robbi.errors import ValidationError

from .creative_game_data import CASES

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

def _detective_state(state):
    if not state["round"]:
        return
    if (not _integer(state.get("case"), 0, len(CASES) - 1)
            or not _integer(state.get("hints", 0), 0, 2) or not _integer(state.get("wrong", 0), 0, 100)
            or not isinstance(state.get("inspected"), list) or len(state["inspected"]) > 4
            or any(not _integer(v, 0, 3) for v in state["inspected"])
            or len(set(state["inspected"])) != len(state["inspected"])
            or not isinstance(state.get("completed"), list) or len(state["completed"]) > len(CASES)
            or any(not _integer(v, 0, len(CASES) - 1) for v in state["completed"])
            or len(set(state["completed"])) != len(state["completed"])
            or state.get("outcome") not in {"investigating", "solved", "skipped", "stopped"}):
        raise ValidationError("Saved detective case is invalid. Restore a saved app-state backup.")

def _case_data(state):
    if not state["round"]:
        return {"cases_available": len(CASES), "completed_cases": 0}
    case = CASES[state["case"]]
    public = {"case_number": state["case"] + 1, "title": case["title"], "question": case["story"],
              "choices": list(case["choices"]), "clues": [{"id": key, "title": key.title()} for key, _ in case["clues"]],
              "evidence": [{"id": case["clues"][i][0], "text": case["clues"][i][1]} for i in state["inspected"]],
              "hints": list(case["hints"][:state["hints"]]), "wrong_guesses": state["wrong"],
              "completed_cases": len(set(state["completed"])), "cases_available": len(CASES),
              "outcome": state.get("outcome", "investigating")}
    if state.get("outcome") in {"solved", "skipped"}:
        public.update(answer=case["answer"], explanation=case["explanation"])
    return public

def _detective_reply(context, state, text, *, changed=False, accepted=True):
    data = _case_data(state)
    artifacts = []
    if changed and context.config.get("screen", True):
        context.request_permissions("robot.display")
        output = _receipt(context, context.robot.display_text, text[:500], hold_s=60)
        data["displayed"] = output.get("displayed") is True
        data["display_hold_s"] = 60 if data["displayed"] else 0
        if isinstance(output.get("artifact"), dict):
            artifacts.append(output["artifact"])
    return _reply(context, state, text, {**data, "accepted": accepted}, changed=changed, artifacts=artifacts)

def tiny_detective(context, tool_name, arguments):
    state = _state(context, "tiny-detective")
    _detective_state(state)
    action = arguments.get("action", "status")
    if action == "stop":
        return _stop(context, state)
    if action == "start":
        _round_ready(state)
        completed = state.get("completed", [])
        requested = arguments.get("case", 0)
        start = (state.get("case", -1) + 1) % len(CASES)
        ordered = [(start + offset) % len(CASES) for offset in range(len(CASES))]
        selected = requested - 1 if requested else next((i for i in ordered if i not in completed), start)
        if not _integer(selected, 0, len(CASES) - 1):
            raise ValidationError("Choose a case number from 1 to 12, or zero for the next unsolved case.")
        state.update(case=selected, active=True, round=state["round"] + 1, inspected=[], hints=0, wrong=0,
                     completed=completed, outcome="investigating")
        case = CASES[selected]
        return _detective_reply(context, state, f"{case['title']}. {case['story']} Choices: {', '.join(case['choices'])}. Inspect a clue before making a deduction.", changed=True)
    if action == "status":
        return _detective_reply(context, state, CASES[state["case"]]["story"] if state["round"] else "Start a case and inspect its clues.")
    if not state["active"]:
        return _detective_reply(context, state, "Start a case first.", accepted=False)
    case = CASES[state["case"]]
    if action == "inspect":
        clue = _clean(arguments.get("clue", ""))
        choices = {key: i for i, (key, _) in enumerate(case["clues"])}
        choices.update({str(i + 1): i for i in range(len(case["clues"]))})
        if clue not in choices:
            return _detective_reply(context, state, "Choose a clue: " + ", ".join(key for key, _ in case["clues"]) + ".", accepted=False)
        index = choices[clue]
        changed = index not in state["inspected"]
        if changed:
            state["inspected"].append(index)
        return _detective_reply(context, state, case["clues"][index][1], changed=changed)
    if action == "hint":
        changed = state["hints"] < len(case["hints"])
        state["hints"] = min(len(case["hints"]), state["hints"] + 1)
        return _detective_reply(context, state, case["hints"][state["hints"] - 1], changed=changed)
    if action == "skip":
        state.update(active=False, outcome="skipped")
        return _detective_reply(context, state, f"Solution: {case['answer']}. {case['explanation']} Start another case when ready.", changed=True)
    if action == "guess":
        answer = _guess(arguments.get("answer", ""))
        if not answer:
            return _detective_reply(context, state, "Give the exact choice or a short answer.", accepted=False)
        aliases = {_clean(v) for v in [case["answer"], *case.get("aliases", [])]}
        if answer in aliases:
            first_solution = state["case"] not in state["completed"]
            points = max(1, 10 - 2 * state["hints"] - min(4, state["wrong"])) if first_solution else 0
            if first_solution:
                state["completed"].append(state["case"])
            state.update(active=False, outcome="solved", score=state["score"] + points, solved=len(set(state["completed"])))
            return _detective_reply(context, state, f"Solved: {case['answer']}. {case['explanation']} +{points} points.", changed=True)
        state["wrong"] = min(100, state["wrong"] + 1)
        return _detective_reply(context, state, "That does not fit all the evidence. Inspect another clue or ask for a hint.", changed=True, accepted=False)
    raise ValidationError("Unknown Tiny Detective action")
