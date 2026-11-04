from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from robbi.configuration.settings import settings, STATE_DIR
from robbi.services.day_cycle import DayCycle
from robbi.services.autonomy_policy import AutonomyPolicy


def make_day_cycle():
    return DayCycle(lambda: {key: getattr(settings, "day_cycle_" + key, default) for key, default in {
        "enabled": True, "timezone": "Europe/Berlin", "weekday_wake_hour": 7,
        "weekend_wake_hour": 9, "day_hour": 10, "evening_hour": 18, "sleep_hour": 22}.items()})


def make_autonomy_policy(day_cycle):
    def options():
        result = {key: getattr(settings, "autonomy_" + key, default) for key, default in {
            "decide_interval_s": 60, "observe_interval_s": 900, "explore_interval_s": 7200, "max_attempts_per_day": 24,
            "max_model_calls_per_day": 8, "max_explorations_per_day": 2}.items()}
        result["checkin_interval_s"] = settings.daily_loop_min_interval_s
        return result
    return AutonomyPolicy(STATE_DIR / "autonomy.json", day_cycle, options)


@dataclass(slots=True)
class InitiativeProposal:
    kind: str
    score: float
    reason: str
    prompt: str
    may_use_web_search: bool = False
    may_use_vision: bool = False


class InitiativeEngine:
    def __init__(self) -> None:
        self._last_by_kind: dict[str, float] = {}

    def propose(self, state: dict, recent_messages: list[dict], recent_events: list[dict]) -> InitiativeProposal | None:
        level = (settings.autonomy_level or "quiet").strip().lower()
        if not settings.autonomy_enabled or level == "off" or state.get("day_cycle", {}).get("quiet"):
            return None
        now = time.time()
        candidates = [
            self._idle_checkin(now, state, recent_messages),
            self._memory_followup(now, recent_messages),
            self._vision_comment(now, recent_events),
        ]
        candidates = [candidate for candidate in candidates if candidate is not None]
        if not candidates:
            return None
        threshold = {"quiet": 0.72, "normal": 0.58, "bold": 0.45}.get(level, 0.72)
        best = max(candidates, key=lambda item: item.score)
        if best.score < threshold:
            return None
        if now - self._last_by_kind.get(best.kind, 0.0) < max(120.0, settings.autonomy_cooldown_after_user_s):
            return None
        # Durable attempt accounting belongs to AutonomyPolicy, before dispatch.
        # Proposal selection itself is read-only and may be shown in diagnostics.
        return best

    def record_attempt(self, proposal):
        if proposal is not None:
            self._last_by_kind[proposal.kind] = time.time()

    def status(self) -> dict:
        return {"last_by_kind": dict(self._last_by_kind)}

    def proposal_dict(self, proposal: InitiativeProposal | None) -> dict | None:
        return asdict(proposal) if proposal is not None else None

    def _idle_checkin(self, now: float, state: dict, recent_messages: list[dict]) -> InitiativeProposal | None:
        last_user_ts = float(state.get("last_user_ts") or 0.0)
        idle_s = now - last_user_ts if last_user_ts else 9999.0
        if idle_s < max(300.0, settings.autonomy_cooldown_after_user_s):
            return None
        prompt = (
            "Robbi has been quiet for a while. Decide whether to make one short proactive check-in "
            "about planning, todos, or focus. If there is no useful reason, return {\"heartbeat\":\"ok\"}. "
            "If you act, be concise, useful, and dryly sarcastic."
        )
        return InitiativeProposal("idle_checkin", min(0.9, idle_s / 1800.0), "long_idle_period", prompt)

    def _memory_followup(self, now: float, recent_messages: list[dict]) -> InitiativeProposal | None:
        if not recent_messages:
            return None
        recent_text = " ".join(str(item.get("content") or "") for item in recent_messages[-4:]).lower()
        markers = ("todo", "roadmap", "robbi", "project", "remind", "focus", "fix", "implement")
        if not any(marker in recent_text for marker in markers):
            return None
        prompt = (
            "Recent context may contain ongoing project/task information. Decide whether Robbi should "
            "quietly update memory or make one useful proactive suggestion. Prefer memory_ops if durable."
        )
        return InitiativeProposal("memory_followup", 0.62, "recent_project_context", prompt)

    def _vision_comment(self, now: float, recent_events: list[dict]) -> InitiativeProposal | None:
        if not settings.autonomy_allow_vision:
            return None
        for event in reversed(recent_events):
            if event.get("event_type") == "vision.photo_taken":
                return InitiativeProposal(
                    "vision_followup",
                    0.55,
                    "recent_photo_event",
                    "There was a recent camera photo. Only comment if it is useful; otherwise stay quiet.",
                    may_use_vision=True,
                )
        return None


def make_attention():
    from robbi.services.attention import Attention
    return Attention(STATE_DIR / "attention.json")
