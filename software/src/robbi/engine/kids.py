from __future__ import annotations
from .state_io import atomic_text

import json
import random
import time
from pathlib import Path

from robbi.configuration.settings import STATE_DIR


KIDS_STATE_FILE = STATE_DIR / "kids.json"


RIDDLES = [
    {"q": "What has keys but no locks?", "a": "piano", "hint": "It can make music."},
    {"q": "What gets wetter the more it dries?", "a": "towel", "hint": "Bathroom technology. Astonishing."},
    {"q": "What has hands but cannot clap?", "a": "clock", "hint": "It judges your lateness."},
]


QUIZ = [
    {"q": "How many wheels does Robbi have?", "a": "two"},
    {"q": "What color is the sky on a clear day?", "a": "blue"},
    {"q": "Which number comes after seven?", "a": "eight"},
]


class KidsEngine:
    def __init__(self, state_file: Path = KIDS_STATE_FILE) -> None:
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def status(self) -> dict:
        return self._read()

    def start(self, mode: str, difficulty: str = "easy") -> dict:
        mode = self._mode(mode)
        state = {
            "active": True,
            "mode": mode,
            "difficulty": difficulty or "easy",
            "score": 0,
            "round": 0,
            "last_prompt": "",
            "expected_answer": "",
            "child_safe": True,
            "updated_at": int(time.time()),
        }
        result = self._next_prompt(state)
        self._write(state)
        return result

    def handle_input(self, text: str) -> dict:
        state = self._read()
        if not state.get("active"):
            return {"ok": False, "speak": "Kids mode is not running."}
        mode = state.get("mode") or "riddle"
        answer = " ".join(str(text or "").lower().split())
        expected = str(state.get("expected_answer") or "").lower()
        if mode in {"riddle", "quiz"}:
            if expected and expected in answer:
                state["score"] = int(state.get("score") or 0) + 1
                result = {"ok": True, "speak": "Correct. Humanity survives another round.", "face": "joyful"}
                result["next"] = self._next_prompt(state)
            elif "hint" in answer:
                result = {"ok": True, "speak": state.get("hint") or "No hint. Brutal minimalism.", "face": "thinking"}
            else:
                result = {"ok": True, "speak": "Not quite. Try again or ask for a hint.", "face": "mischievous"}
        elif mode == "rps":
            choices = {"rock", "paper", "scissors"}
            user = next((choice for choice in choices if choice in answer), "")
            if not user:
                result = {"ok": True, "speak": "Say rock, paper, or scissors. Revolutionary interface design.", "face": "thinking"}
            else:
                robot = random.choice(sorted(choices))
                outcome = self._rps_outcome(user, robot)
                if outcome == "win":
                    state["score"] = int(state.get("score") or 0) + 1
                result = {
                    "ok": True,
                    "speak": f"I chose {robot}. {outcome}.",
                    "face": "joyful" if outcome == "win" else "grumpy" if outcome == "lose" else "neutral",
                }
        else:
            result = self._story_step(state, answer)
        state["updated_at"] = int(time.time())
        self._write(state)
        return result

    def stop(self) -> dict:
        state = self._read()
        state["active"] = False
        state["updated_at"] = int(time.time())
        self._write(state)
        return {"ok": True, "speak": "Kids mode stopped.", "state": state}

    def _next_prompt(self, state: dict) -> dict:
        mode = state.get("mode") or "riddle"
        state["round"] = int(state.get("round") or 0) + 1
        if mode == "riddle":
            item = random.choice(RIDDLES)
            state["last_prompt"] = item["q"]
            state["expected_answer"] = item["a"]
            state["hint"] = item["hint"]
            return {"ok": True, "speak": item["q"], "face": "thinking", "state": state}
        if mode == "quiz":
            item = random.choice(QUIZ)
            state["last_prompt"] = item["q"]
            state["expected_answer"] = item["a"]
            state["hint"] = "Think small and simple."
            return {"ok": True, "speak": item["q"], "face": "thinking", "state": state}
        if mode == "rps":
            state["last_prompt"] = "Rock, paper, scissors?"
            state["expected_answer"] = ""
            return {"ok": True, "speak": "Rock, paper, scissors. Choose your weapon.", "face": "mischievous", "state": state}
        state["last_prompt"] = "The tiny robot found a glowing button. Press it, inspect it, or ignore it?"
        state["expected_answer"] = ""
        return {"ok": True, "speak": state["last_prompt"], "face": "mischievous", "state": state}

    def _story_step(self, state: dict, answer: str) -> dict:
        if "press" in answer:
            text = "The button opens a secret snack drawer. Suspiciously advanced furniture."
        elif "inspect" in answer:
            text = "It says: do not press. So obviously this is now a moral crisis."
        else:
            text = "Robbi ignores it too. Personal growth through laziness."
        state["last_prompt"] = "Continue the story, choose a new door, or stop?"
        return {"ok": True, "speak": f"{text} {state['last_prompt']}", "face": "happy"}

    def _rps_outcome(self, user: str, robot: str) -> str:
        if user == robot:
            return "draw"
        wins = {("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")}
        return "lose" if (user, robot) in wins else "win"

    def _mode(self, mode: str) -> str:
        mode = str(mode or "").strip().lower().replace("-", "_")
        aliases = {"rock_paper_scissors": "rps", "story_buddy": "story"}
        return aliases.get(mode, mode) if aliases.get(mode, mode) in {"riddle", "story", "rps", "quiz"} else "riddle"

    def _read(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"active": False, "mode": "", "score": 0, "round": 0, "child_safe": True}

    def _write(self, state: dict) -> None:
        atomic_text(self.state_file, json.dumps(state, ensure_ascii=False, indent=2))
