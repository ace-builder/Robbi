from __future__ import annotations
from .state_io import atomic_text

import json
import time
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from robbi.configuration.settings import DAILY_LOOP_STATE_FILE, settings


@dataclass(slots=True)
class DailyState:
    ts: int
    open_todos: list[dict]
    active_reminders: list[dict]
    pending_memories: list[dict]
    relevant_memories: list[dict]
    recent_messages: list[dict]
    recent_failures: list[dict]
    heartbeat: dict
    suggested_next_action: str
    improvement_suggestions: list[str]


@dataclass(slots=True)
class DailyProposal:
    kind: str
    reason: str
    prompt: str
    score: float


class DailyLoop:
    def __init__(self, state_file: Path = DAILY_LOOP_STATE_FILE, *, day_cycle=None) -> None:
        self._state_file = state_file
        self.day_cycle = day_cycle
        self._lock = threading.RLock()
        self._last_error = ""
        self._state = self._load_state()

    def build_state(
        self,
        *,
        todos: list[dict],
        reminders: list[dict],
        pending_memories: list[dict],
        relevant_memories: list[dict],
        recent_messages: list[dict],
        recent_events: list[dict],
        heartbeat: dict,
    ) -> dict:
        open_todos = [item for item in todos if isinstance(item, dict) and not item.get("done_at")]
        active_reminders = [item for item in reminders if isinstance(item, dict)]
        failures = self._recent_failures(recent_events)
        state = DailyState(
            ts=int(time.time()),
            open_todos=open_todos[:20],
            active_reminders=active_reminders[:12],
            pending_memories=[item for item in pending_memories if isinstance(item, dict)][:20],
            relevant_memories=[item for item in relevant_memories if isinstance(item, dict)][:12],
            recent_messages=[item for item in recent_messages if isinstance(item, dict)][-10:],
            recent_failures=failures[:10],
            heartbeat=dict(heartbeat or {}),
            suggested_next_action=self._suggest_next_action(open_todos, active_reminders, pending_memories, failures),
            improvement_suggestions=self._improvement_suggestions(failures, pending_memories),
        )
        return {**asdict(state), "day_cycle": self.day_cycle.snapshot() if self.day_cycle else {}}

    def propose(self, daily_state: dict, *, skip_reason: str = "") -> DailyProposal | None:
        mode = str(settings.daily_loop_mode or "manual").strip().lower()
        if not settings.daily_loop_enabled or mode == "manual" or skip_reason or (self.day_cycle and self.day_cycle.snapshot()["quiet"]):
            return None
        now = time.time()
        self._reset_day_if_needed(now)
        if int(self._state.get("proactive_count") or 0) >= max(0, settings.daily_loop_max_proactive_per_day):
            return None
        if now - float(self._state.get("last_proactive_ts") or 0.0) < max(60.0, settings.daily_loop_min_interval_s):
            return None

        pending_count = len(daily_state.get("pending_memories") or [])
        todo_count = len(daily_state.get("open_todos") or [])
        reminder_count = len(daily_state.get("active_reminders") or [])
        failures = daily_state.get("recent_failures") or []

        candidates: list[DailyProposal] = []
        if settings.daily_loop_memory_review_nudge_enabled and pending_count:
            candidates.append(
                DailyProposal(
                    kind="memory_review",
                    reason=f"{pending_count} pending memories need review",
                    prompt=(
                        "Robbi has pending extracted memories. Decide whether to briefly ask the user to review them. "
                        "Mention that /review or the memory page can approve them. If not useful now, return heartbeat ok."
                    ),
                    score=0.78,
                )
            )
        if settings.daily_loop_todo_followup_enabled and todo_count:
            candidates.append(
                DailyProposal(
                    kind="todo_followup",
                    reason=f"{todo_count} open todos",
                    prompt=(
                        "Robbi has open todos for the user. Make one short useful coaching prompt, or stay quiet if it would be annoying. "
                        "Prefer asking which task to tackle next or suggesting the smallest next action."
                    ),
                    score=0.7 if mode == "active" else 0.58,
                )
            )
        if reminder_count:
            candidates.append(
                DailyProposal(
                    kind="reminder_followup",
                    reason=f"{reminder_count} active reminders",
                    prompt="Robbi has active reminders. Only follow up if a concise planning nudge is useful.",
                    score=0.62,
                )
            )
        if failures:
            candidates.append(
                DailyProposal(
                    kind="self_improvement",
                    reason="recent runtime failures detected",
                    prompt=(
                        "Robbi noticed recent runtime failures. Stay quiet unless a short useful self-improvement note helps the user. "
                        "Prefer memory_ops for durable repeated failures."
                    ),
                    score=0.55,
                )
            )
        if not candidates:
            return None
        threshold = 0.55 if mode == "active" else 0.72
        best = max(candidates, key=lambda item: item.score)
        return best if best.score >= threshold else None

    def record_proactive(self, proposal: DailyProposal | dict | None) -> None:
        if proposal is None:
            return
        with self._lock:
            now = time.time()
            self._reset_day_if_needed(now)
            self._state["last_proactive_ts"] = now
            self._state["proactive_count"] = int(self._state.get("proactive_count") or 0) + 1
            self._state["last_kind"] = proposal.kind if isinstance(proposal, DailyProposal) else proposal.get("kind", "")
            self._save_state()

    def status(self) -> dict:
        self._reset_day_if_needed(time.time())
        return {
            "enabled": settings.daily_loop_enabled,
            "mode": settings.daily_loop_mode,
            "max_proactive_per_day": settings.daily_loop_max_proactive_per_day,
            "min_interval_s": settings.daily_loop_min_interval_s,
            "proactive_count": int(self._state.get("proactive_count") or 0),
            "last_proactive_ts": int(float(self._state.get("last_proactive_ts") or 0)),
            "last_kind": self._state.get("last_kind", ""),
            "day_cycle": self.day_cycle.snapshot() if self.day_cycle else {},
            "storage_error": self._last_error,
        }

    def _suggest_next_action(
        self,
        todos: list[dict],
        reminders: list[dict],
        pending_memories: list[dict],
        failures: list[dict],
    ) -> str:
        if todos:
            title = str(todos[0].get("title") or "").strip()
            if title:
                return f"Start with: {title}"
        if reminders:
            return "Check the next reminder and decide whether it still matters."
        if pending_memories:
            return "Review pending memories so Robbi does not confidently remember nonsense. A bold innovation."
        if failures:
            return "Check recent runtime failures before adding more shiny chaos."
        return "No urgent task. Pick one useful thing before Robbi invents one."

    def _improvement_suggestions(self, failures: list[dict], pending_memories: list[dict]) -> list[str]:
        suggestions: list[str] = []
        if pending_memories:
            suggestions.append(f"Review {len(pending_memories)} pending memories.")
        failure_types = {str(item.get("event_type") or item.get("type") or "") for item in failures}
        if "tool.failed" in failure_types:
            suggestions.append("Inspect recent failed tools and decide whether a config or prompt needs adjustment.")
        if "llm_error" in failure_types:
            suggestions.append("Check LLM provider health if model failures repeat.")
        if failures and not suggestions:
            suggestions.append("Review recent failures if the same issue repeats.")
        return suggestions[:4]

    def _recent_failures(self, events: list[dict]) -> list[dict]:
        markers = {"tool.failed", "llm_error", "memory.extraction_failed"}
        failures: list[dict] = []
        for event in reversed(events or []):
            event_type = str(event.get("event_type") or "")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            text = json.dumps(payload, ensure_ascii=False).lower()
            if event_type in markers or "failed" in event_type or "timeout" in text or "error" in text:
                failures.append({"event_type": event_type, "payload": payload, "ts": event.get("ts")})
        return failures

    def _reset_day_if_needed(self, now: float) -> None:
        day = self.day_cycle.snapshot(now)["date"] if self.day_cycle else time.strftime("%Y-%m-%d", time.localtime(now))
        with self._lock:
            if not self._state.get("day") or self._state["day"] < day:
                self._state = {"day": day, "proactive_count": 0, "last_proactive_ts": 0.0, "last_kind": ""}

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as error:
            self._last_error = str(error)
            return {}

    def _save_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_text(self._state_file, json.dumps(self._state, indent=2, ensure_ascii=False))
            self._last_error = ""
        except Exception as error:
            self._last_error = str(error)
