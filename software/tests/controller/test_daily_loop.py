from robot import daily_loop as daily_loop_module
from robot.daily_loop import DailyLoop


def test_daily_state_aggregates_todos_memory_and_failures(tmp_path):
    loop = DailyLoop(state_file=tmp_path / "daily_loop.json")

    state = loop.build_state(
        todos=[{"title": "order M3 screws"}, {"title": "done", "done_at": 123}],
        reminders=[{"message": "stand up"}],
        pending_memories=[{"id": "mem-1", "content": "maker likes oat milk."}],
        relevant_memories=[{"id": "mem-2", "content": "Robbi project uses ToF."}],
        recent_messages=[{"role": "user", "content": "plan my day"}],
        recent_events=[{"event_type": "tool.failed", "payload": {"tool": "tts"}, "ts": 1}],
        heartbeat={"enabled": True},
    )

    assert len(state["open_todos"]) == 1
    assert state["open_todos"][0]["title"] == "order M3 screws"
    assert len(state["pending_memories"]) == 1
    assert state["recent_failures"][0]["event_type"] == "tool.failed"
    assert "order M3 screws" in state["suggested_next_action"]


def test_daily_loop_proactive_budget_and_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_loop_module.settings, "daily_loop_enabled", True)
    monkeypatch.setattr(daily_loop_module.settings, "daily_loop_mode", "active")
    monkeypatch.setattr(daily_loop_module.settings, "daily_loop_max_proactive_per_day", 1)
    monkeypatch.setattr(daily_loop_module.settings, "daily_loop_min_interval_s", 60.0)
    monkeypatch.setattr(daily_loop_module.settings, "daily_loop_todo_followup_enabled", True)
    monkeypatch.setattr(daily_loop_module.settings, "daily_loop_memory_review_nudge_enabled", True)
    loop = DailyLoop(state_file=tmp_path / "daily_loop.json")
    state = {
        "open_todos": [{"title": "finish roadmap"}],
        "active_reminders": [],
        "pending_memories": [],
        "recent_failures": [],
    }

    proposal = loop.propose(state)
    assert proposal is not None
    assert proposal.kind == "todo_followup"

    loop.record_proactive(proposal)
    assert loop.propose(state) is None


def test_daily_loop_manual_mode_stays_quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_loop_module.settings, "daily_loop_enabled", True)
    monkeypatch.setattr(daily_loop_module.settings, "daily_loop_mode", "manual")
    loop = DailyLoop(state_file=tmp_path / "daily_loop.json")

    assert loop.propose({"open_todos": [{"title": "x"}]}) is None
