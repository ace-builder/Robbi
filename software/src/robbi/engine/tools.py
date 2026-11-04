from __future__ import annotations

from dataclasses import asdict, dataclass
from robbi.motion_contract import DRIVE_SCHEMA, motion_prompt_catalog


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    permissions: list[str]
    latency_class: str = "fast"
    may_run_proactively: bool = False
    requires_confirmation: bool = False
    cooldown_s: float = 0.0

    def prompt_line(self) -> str:
        flags = []
        if self.may_run_proactively:
            flags.append("proactive")
        if self.requires_confirmation:
            flags.append("confirm")
        suffix = f" ({', '.join(flags)})" if flags else ""
        return f"- `{self.name}` [{self.latency_class}]{suffix}: {self.description}"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools = self._default_tools()

    def list_tools(self) -> list[dict]:
        return [asdict(tool) for tool in self._tools]

    def prompt_catalog(self) -> str:
        lines = ["Registered tool calls:"]
        lines.extend(tool.prompt_line() for tool in self._tools)
        lines.append("")
        lines.append("Tool call JSON keys:")
        lines.append("- `web_search`: string query or object `{query, max_results, search_depth, topic, time_range} (topic: general/news/finance; time_range: day/week/month/year or omit)`")
        lines.append("- `memory_search`: string query or object `{query, limit}`")
        lines.append("- `memory_ops`: object with `add`, `upsert`, `update`, `delete`, `search`")
        lines.append("- `take_picture`: true, string prompt, or object `{prompt, save_photo}`")
        lines.append("- `app`: string id or object `{id, input}` for apps like `focus-timer` and `reminder`")
        lines.append("")
        lines.append(motion_prompt_catalog())
        return "\n".join(lines)

    def _default_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec("drive", "Move by centimeters, turn by degrees, follow arcs/sine waves, wiggle or stop. See the runtime movement contract below.", DRIVE_SCHEMA, ["motion"], "fast", False),
            ToolSpec("gesture", "Play a registered measured body expression or dance; available names come from the runtime movement contract.", {"type": "string"}, ["motion"], "fast", False),
            ToolSpec("memory_search", "Search Robbi's durable SQLite memory before answering recall-heavy questions.", {"query": "string", "limit": "integer"}, ["memory_read"], "instant", True),
            ToolSpec("memory_write", "Add or upsert durable memories through memory_ops.", {"kind": "string", "content": "string"}, ["memory_write"], "instant", True),
            ToolSpec("memory_update", "Update an existing memory by id through memory_ops.update.", {"id": "integer"}, ["memory_write"], "instant", True),
            ToolSpec("take_picture", "Take a fresh camera image when vision is needed.", {"prompt": "string", "save_photo": "boolean"}, ["camera"], "fast", False),
            ToolSpec("describe_picture", "Use the vision-capable LLM pass after take_picture.", {"prompt": "string"}, ["camera"], "slow", False),
            ToolSpec("web_search", "Search the web for current or external facts.", {"query": "string"}, ["web_search"], "slow", False),
            ToolSpec("start_focus_timer", "Start the focus timer app.", {"duration_seconds": "number"}, ["sound"], "instant", False),
            ToolSpec("stop_focus_timer", "Stop the focus timer app.", {}, ["sound"], "instant", False),
            ToolSpec("set_reminder", "Schedule a reminder with delay_seconds and message.", {"delay_seconds": "number", "message": "string"}, ["sound"], "instant", False),
            ToolSpec("cancel_reminder", "Cancel the active reminder.", {}, ["sound"], "instant", False),
        ]
