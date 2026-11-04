from __future__ import annotations
from .state_io import atomic_text

import json
import time
from pathlib import Path

from robbi.configuration.settings import ONBOARDING_DRAFT_FILE, PROFILE_STATE_FILE


class OnboardingStore:
    """Preview-first onboarding state. Applying never touches secrets or wake models."""

    def __init__(self, draft_file: Path = ONBOARDING_DRAFT_FILE, profile_file: Path = PROFILE_STATE_FILE) -> None:
        self.draft_file = Path(draft_file)
        self.profile_file = Path(profile_file)
        self.draft_file.parent.mkdir(parents=True, exist_ok=True)

    def draft(self) -> dict:
        return self._read_json(self.draft_file, self._default_draft())

    def save_draft(self, payload: dict) -> dict:
        draft = self.draft()
        if isinstance(payload, dict):
            draft.update(payload)
        draft["updated_at"] = int(time.time())
        self._write_json(self.draft_file, draft)
        return draft

    def reset_draft(self) -> dict:
        draft = self._default_draft()
        self._write_json(self.draft_file, draft)
        return draft

    def profile(self) -> dict:
        return self._read_json(self.profile_file, {})

    def apply(self, payload: dict, memory_store, productivity_store) -> dict:
        draft = self.save_draft(payload or {})
        now = int(time.time())
        user_name = self._clean(draft.get("user_name"))
        robot_name = self._clean(draft.get("robot_name")) or "Robbi"
        profile = {
            "user_name": user_name,
            "robot_name": robot_name,
            "wake_phrase": "Hey Robbi",
            "wake_phrase_fixed": True,
            "applied_at": now,
            "updated_at": now,
        }
        self._write_json(self.profile_file, profile)

        approved: list[dict] = []
        pending: list[dict] = []
        if user_name:
            approved.append(
                {
                    "kind": "person",
                    "content": f"The primary user is {user_name}.",
                    "tags": ["onboarding", "profile"],
                    "importance": 5,
                    "confidence": 0.95,
                    "review_status": "approved",
                }
            )
        if robot_name:
            approved.append(
                {
                    "kind": "robot_state",
                    "content": f"Robbi's display name is {robot_name}. The wake phrase remains Hey Robbi.",
                    "tags": ["onboarding", "profile"],
                    "importance": 4,
                    "confidence": 0.95,
                    "review_status": "approved",
                }
            )
        for key, kind in (
            ("help_with", "preference"),
            ("tone_preferences", "preference"),
            ("rules", "preference"),
            ("location", "environment"),
            ("current_focus", "task"),
        ):
            value = self._clean(draft.get(key))
            if value:
                pending.append(
                    {
                        "kind": kind,
                        "content": value,
                        "tags": ["onboarding"],
                        "importance": 4,
                        "confidence": 0.8,
                        "review_status": "pending",
                    }
                )
        for task in self._as_strings(draft.get("open_tasks")):
            try:
                productivity_store.add_list_item("todo", task, source="onboarding")
            except Exception:
                pass
            pending.append(
                {
                    "kind": "task",
                    "content": f"Open task: {task}",
                    "tags": ["onboarding", "todo"],
                    "importance": 4,
                    "confidence": 0.75,
                    "review_status": "pending",
                }
            )
        if approved:
            memory_store.remember({"upsert": approved}, source="onboarding:approved")
        if pending:
            memory_store.remember({"upsert": pending}, source="onboarding:pending")
        return {
            "profile": profile,
            "approved_memories": len(approved),
            "pending_memories": len(pending),
            "todos_added": len(self._as_strings(draft.get("open_tasks"))),
        }

    def _default_draft(self) -> dict:
        now = int(time.time())
        return {
            "user_name": "",
            "robot_name": "Robbi",
            "wake_phrase": "Hey Robbi",
            "wake_phrase_fixed": True,
            "help_with": "",
            "tone_preferences": "",
            "rules": "",
            "location": "",
            "current_focus": "",
            "open_tasks": [],
            "interview_notes": "",
            "created_at": now,
            "updated_at": now,
        }

    def _read_json(self, path: Path, default: dict) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else dict(default)
        except Exception:
            return dict(default)

    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2))

    def _clean(self, value) -> str:
        return " ".join(str(value or "").split()).strip()

    def _as_strings(self, value) -> list[str]:
        if isinstance(value, str):
            raw = [part.strip() for part in value.replace("\r", "\n").split("\n")]
        elif isinstance(value, list):
            raw = [str(part or "").strip() for part in value]
        else:
            raw = []
        return [part[:300] for part in raw if part]
