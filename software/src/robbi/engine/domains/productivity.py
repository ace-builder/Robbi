from __future__ import annotations
import json
import re
import time
from datetime import datetime
from robbi.configuration.settings import TODO_STATE_FILE, settings

class ProductivityDomain:
    def _undo_core_tool(self, call, *, source, request_id=None):
        reference = call.get("undo_ref")
        if not reference or call.get("tool") not in {"create_note", "add_todo", "add_list_item"}:
            return {"ok": False, "text": "This action cannot be automatically undone. No saved data was changed by undo."}
        if request_id is not None and not self._request_current(request_id):
            return {"ok": False, "cancelled": True, "text": "Undo was cancelled."}
        try:
            result = self.productivity_service.undo(reference, source=source)
            limitations = call.get("result", {}).get("limitations", [])
            if result.get("ok") and isinstance(limitations, list):
                result["text"] += " " + " ".join(str(item)[:240] for item in limitations[:8]) if limitations else ""
            return result
        except ValueError as error:
            return {"ok": False, "text": str(error)}

    def memory_search(self, query: str, limit: int = 10) -> dict:
        return {"query": query, "results": self.memory.search(query, limit=limit)}

    def today_state(self, *, touch_memories: bool = True) -> dict:
        reminders = self.reminders.list_items(include_completed=False, limit=12)
        recent_messages = self.memory.recent_messages()
        memory_query = self._daily_memory_query(recent_messages)
        relevant_memories = self.memory.search(memory_query, limit=8, touch=touch_memories)
        return self.daily_loop.build_state(
            todos=self._load_todos(),
            reminders=reminders,
            pending_memories=self.memory.pending_entries(limit=20),
            relevant_memories=relevant_memories,
            recent_messages=recent_messages,
            recent_events=self.events.recent(limit=80),
            heartbeat=self.heartbeat_status(),
        )

    def today_brief(self, source: str = "ui") -> dict:
        state = self.today_state()
        prompt = self._build_daily_brief_prompt(state)
        memory_context = self.memory.prepare_prompt_context(
            self._daily_memory_query(state.get("recent_messages")),
            recent_messages=self.memory.recent_messages(),
            max_chars=3500,
        )
        action = self.llm.ask(
            prompt,
            provider=settings.ui_text_provider,
            model=settings.ui_text_model,
            recent_messages=self.memory.recent_messages(),
            memory_text=f"{memory_context['prompt_text']}\n\nDaily state:\n{json.dumps(state, ensure_ascii=False)}",
            soul_text=self._soul_text,
            tools_text=self._tools_prompt_context(),
            app_catalog_text=self.apps.prompt_catalog(),
            extra_instructions="Return a compact Robbi action JSON for a daily brief. Prefer speak. Do not drive.",
        )
        action = self._sanitize_llm_motion(action, user_text="daily brief")
        return action if isinstance(action, dict) else {"speak": str(action)}

    def productivity_data(self) -> dict:
        return self.productivity_service.snapshot()

    def create_note(self, payload: dict) -> dict:
        return self.productivity_service.create_note(payload)

    def update_note(self, note_id: int, payload: dict) -> dict | None:
        return self.productivity_service.update_note(note_id, payload)

    def delete_note(self, note_id: int) -> bool:
        return self.productivity_service.delete_note(note_id)

    def add_list_item(self, payload: dict) -> dict:
        return self.productivity_service.add_list_item(payload)

    def update_list_item(self, item_id: int, payload: dict) -> dict | None:
        return self.productivity_service.update_list_item(item_id, payload)

    def delete_list_item(self, item_id: int) -> bool:
        return self.productivity_service.delete_list_item(item_id)

    def create_research_note(self, payload: dict) -> dict:
        return self.productivity_service.create_research_note(payload)

    def onboarding_data(self) -> dict:
        return {"draft": self.onboarding.draft(), "profile": self.onboarding.profile()}

    def onboarding_save(self, payload: dict) -> dict:
        return self.onboarding.save_draft(payload or {})

    def onboarding_reset(self) -> dict:
        return self.onboarding.reset_draft()

    def onboarding_apply(self, payload: dict) -> dict:
        result = self.onboarding.apply(payload or {}, self.memory, self.productivity)
        self._publish_event("onboarding.applied", result)
        return result

    def kids_status(self) -> dict:
        return self.kids.status()

    def kids_start(self, payload: dict) -> dict:
        result = self.kids.start(str(payload.get("mode") or "riddle"), str(payload.get("difficulty") or "easy"))
        self._publish_event("app.started", {"id": "kids", "mode": payload.get("mode") or "riddle"})
        return result

    def kids_input(self, payload: dict) -> dict:
        result = self.kids.handle_input(str(payload.get("text") or payload.get("answer") or ""))
        self._publish_event("app.kids_turn", {"mode": self.kids.status().get("mode"), "ok": result.get("ok")})
        return result

    def kids_stop(self) -> dict:
        result = self.kids.stop()
        self._publish_event("app.finished", {"id": "kids"})
        return result

    def _run_research_note_job(self, job_id: int) -> None:
        self.productivity_service.run_research_job(job_id)

    def daily_loop_status(self) -> dict:
        status = self.daily_loop.status()
        try:
            state = self.today_state(touch_memories=False)
            status.update(
                {
                    "open_todo_count": len(state.get("open_todos") or []),
                    "active_reminder_count": len(state.get("active_reminders") or []),
                    "pending_memory_count": len(state.get("pending_memories") or []),
                    "suggested_next_action": state.get("suggested_next_action", ""),
                }
            )
        except Exception as exc:
            status["error"] = str(exc)
        return status

    def _load_todos(self) -> list[dict]:
        return self.productivity.list_items("todo", include_done=True)

    def _save_todos(self, todos: list[dict]) -> None:
        # Compatibility export for older tooling. SQLite is now the source of truth.
        TODO_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        TODO_STATE_FILE.write_text(json.dumps(todos, ensure_ascii=False, indent=2), encoding="utf-8")

    def _format_todos(self, todos: list[dict]) -> str:
        active = [item for item in todos if not item.get("done_at")]
        if not active:
            return "No open todos."
        lines = ["Open todos:"]
        for index, item in enumerate(active, start=1):
            lines.append(f"{index}. {item.get('title')}")
        return "\n".join(lines)

    def _telegram_note_command(self, text: str) -> str:
        note_text = " ".join(str(text or "").strip().split())
        if not note_text:
            return "Usage: /note quick note text"
        note = self.productivity.create_note(raw_text=note_text, summary=note_text[:500], source="telegram")
        return f"Note saved: {note.get('title')}"

    def _telegram_notes_command(self, text: str) -> str:
        notes = self.productivity.list_notes(limit=8)
        if not notes:
            return "No notes yet."
        lines = ["Recent notes:"]
        for note in notes:
            summary = note.get("summary") or note.get("raw_text") or ""
            lines.append(f"- {note.get('id')}: {note.get('title')} - {summary[:120]}")
        return "\n".join(lines)

    def _telegram_shopping_command(self, text: str) -> str:
        command = " ".join(str(text or "").strip().split())
        lower = command.lower()
        if lower.startswith("add "):
            item = self.productivity.add_list_item("shopping", command[4:].strip(" ."), source="telegram")
            return f"Shopping item added: {item.get('title')}"
        if lower.startswith("done "):
            token = lower[5:].strip()
            active = self.productivity.list_items("shopping", include_done=False)
            target = None
            if token.isdigit() and 0 < int(token) <= len(active):
                target = active[int(token) - 1]
            else:
                target = next((item for item in active if token in str(item.get("title", "")).lower()), None)
            if not target:
                return "Shopping item not found."
            self.productivity.update_list_item(int(target["id"]), {"done": True})
            return f"Shopping item done: {target.get('title')}"
        items = self.productivity.list_items("shopping", include_done=False)
        if not items:
            return "Shopping list is empty."
        lines = ["Shopping list:"]
        lines.extend(f"{idx}. {item.get('title')}" for idx, item in enumerate(items, start=1))
        return "\n".join(lines)

    def _daily_memory_query(self, recent_messages: list[dict] | None = None) -> str:
        recent_text = " ".join(str(item.get("content") or "") for item in (recent_messages or [])[-4:])
        return f"today plan todos tasks reminders projects preferences routines Robbi improvements {recent_text}".strip()

    def _build_daily_brief_prompt(self, state: dict) -> str:
        return (
            "Create a compact useful daily brief for the user as Robbi. Include open todos, active reminders, relevant memory/project context, "
            "pending memory review if any, one concrete next action, and one improvement suggestion if useful. "
            "Keep it short, practical, and dryly sarcastic. Return compact JSON.\n\n"
            f"DailyState JSON: {json.dumps(state, ensure_ascii=False)}"
        )

    def _format_today_state_text(self, state: dict) -> str:
        todos = state.get("open_todos") or []
        reminders = state.get("active_reminders") or []
        pending = state.get("pending_memories") or []
        improvements = state.get("improvement_suggestions") or []
        lines = ["Today"]
        lines.append(f"Open todos: {len(todos)}")
        for index, item in enumerate(todos[:6], start=1):
            lines.append(f"{index}. {item.get('title')}")
        lines.append(f"Active reminders: {len(reminders)}")
        for item in reminders[:4]:
            due_ts = int(item.get("due_at") or 0)
            when = datetime.fromtimestamp(due_ts).strftime("%H:%M") if due_ts else "soon"
            lines.append(f"- {when}: {item.get('message', '')}")
        if pending:
            lines.append(f"Pending memories: {len(pending)}")
        lines.append(f"Next: {state.get('suggested_next_action') or 'Pick one useful thing.'}")
        if improvements:
            lines.append("Improvements:")
            lines.extend(f"- {item}" for item in improvements[:3])
        return "\n".join(lines)

    def _looks_like_daily_request(self, text: str) -> bool:
        lowered = str(text or "").strip().lower()
        if not lowered:
            return False
        phrases = (
            "what should i do today",
            "plan my day",
            "daily brief",
            "what is still open",
            "what's still open",
            "what did i want to do",
            "what did i want to remember",
            "what are my todos",
            "what are my tasks",
            "what should i do now",
        )
        return any(phrase in lowered for phrase in phrases)

    def _maybe_extract_todo_from_text(self, text: str, source: str = "") -> dict | None:
        raw = " ".join(str(text or "").strip().split())
        if not raw:
            return None
        lowered = raw.lower()
        if lowered.startswith("/") or lowered.startswith("app:"):
            return None
        if "remind me" in lowered or "reminder" in lowered:
            return None
        patterns = (
            r"\b(?:i need to|i have to|i should|i must)\s+(.+)",
            r"\b(?:todo|to do)\s*[:\-]\s*(.+)",
        )
        title = ""
        for pattern in patterns:
            match = re.search(pattern, raw, flags=re.IGNORECASE)
            if match:
                title = match.group(1).strip(" .,!?:;")
                break
        if not title or len(title) < 4:
            return None
        title = title[:300]
        todos = self._load_todos()
        active_titles = {" ".join(str(item.get("title") or "").lower().split()) for item in todos if not item.get("done_at")}
        normalized_title = " ".join(title.lower().split())
        if normalized_title in active_titles:
            return None
        item = {
            "id": f"todo-{int(time.time() * 1000)}",
            "title": title,
            "created_at": int(time.time()),
            "done_at": None,
            "source": source or "auto",
        }
        item = self.productivity.add_list_item("todo", title, source=source or "auto")
        self.memory.remember(
            {
                "upsert": [
                    {
                        "kind": "task",
                        "content": f"Open todo: {title}",
                        "tags": ["todo", "auto"],
                        "importance": 4,
                        "confidence": 0.75,
                    }
                ]
            },
            source="auto:todo",
        )
        self._publish_event("memory.created", {"source": "auto:todo", "todo": title})
        return item
