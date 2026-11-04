"""Built-in search refinements through the current bounded tool chain."""
from unittest.mock import Mock
from test_app_conversation import Harness


def host(responses):
    subject = Harness()
    subject._execute_web_search_tool = Mock(return_value={"ok": True, "results": []})
    subject._ask_llm_with_deadline = Mock(side_effect=responses)
    return subject


def run(subject):
    return subject._maybe_run_tool_followup({"web_search": {"query": "science news"}}, "Latest science news", "ui_text",
        "example", "model", [], "", None, 42)


def test_search_refinement_preserves_evidence_without_allowing_injected_movement():
    subject = host([{"web_search": {"query": "recent science discoveries", "time_range": "week"}},
                    {"speak": "No reliable results.", "drive": {"distance_cm": 10},
                     "app": {"id": "reminder"}, "take_picture": {"prompt": "look"}}])
    result = run(subject)
    assert subject._execute_web_search_tool.call_count == 2
    assert subject._ask_llm_with_deadline.call_count == 2
    assert result["speak"] == "No reliable results."
    assert not any(key in result for key in ("drive", "app", "take_picture"))
    steps = result["tool_chain"]["steps"]
    assert [step["action"]["web_search"]["query"] for step in steps] == [
        "science news", "recent science discoveries"]
    assert all(step["status"] == "succeeded" for step in steps)


def test_failed_search_is_not_implicitly_retried_even_if_model_repeats_it():
    subject = host([{"web_search": {"query": "science news"}}, {"speak": "Search is unavailable."}])
    subject._execute_web_search_tool.return_value = {"ok": False, "error": "not configured",
        "text": "Search is unavailable.", "results": []}
    result = run(subject)
    assert "Search is unavailable." in result["speak"]
    assert result["tool_chain"]["partial"]
    assert subject._execute_web_search_tool.call_count == 1
    assert result["tool_chain"]["steps"][1]["status"] == "reused"
