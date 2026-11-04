"""Conversation input policy, prompt preparation and bounded provider requests.

This layer knows nothing about hardware presentation or the robot controller.
The caller supplies narrow ports and an immutable settings snapshot. Cancellation
ends the wait promptly; provider adapters must still bound their own network I/O.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass
import logging
import math
import time
from typing import Protocol
from robbi.services.prompt_context import recent_context, daily_context
from robbi.actions import validate_action
from robbi.errors import BusyError


class ConversationMemory(Protocol):
    def recent_messages(self) -> list[dict]: ...
    def prepare_prompt_context(self, text: str, *, recent_messages: list[dict]) -> dict: ...


class CaptureImage(Protocol):
    def __call__(self, *, max_width: int, quality: int) -> bytes | None: ...


class ConversationProvider(Protocol):
    def ask(self, text: str, **kwargs) -> dict: ...


class RequestScheduler(Protocol):
    def submit(self, callback: Callable, /, *args, **kwargs) -> Future: ...


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ConversationOptions:
    speech_provider: str = "openai"
    speech_model: str = ""
    text_provider: str = "openai"
    text_model: str = ""
    attach_speech_photo: bool = False
    attach_text_photo: bool = False
    vision_max_width: int = 640
    vision_jpeg_quality: int = 75
    tts_provider: str = "local"
    telegram_speak: bool = False
    telegram_actions: bool = False

    def target(self, source: str) -> tuple[str, str]:
        if source == "speech_input":
            return self.speech_provider, self.speech_model
        return self.text_provider, self.text_model

    def default_photo(self, source: str) -> bool:
        return self.attach_speech_photo if source == "speech_input" else self.attach_text_photo

    def attach_photo(self, source: str, requested: bool | None) -> bool:
        return self.default_photo(source) if requested is None else bool(requested)


@dataclass(frozen=True, slots=True)
class PreparedConversation:
    recent_messages: list[dict]
    memory_text: str
    image_bytes: bytes | None
    photo_prepare_ms: float


class ConversationService:
    def __init__(
        self, *, memory: ConversationMemory, capture_image: CaptureImage,
        provider: ConversationProvider, scheduler: RequestScheduler,
        options: Callable[[], ConversationOptions],
        daily_state: Callable[[], dict],
        publish_event: Callable[[str, dict], None],
        background_scheduler: RequestScheduler | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._memory = memory
        self._capture_image = capture_image
        self._provider = provider
        self._scheduler = scheduler
        self._background_scheduler = background_scheduler if background_scheduler is not None else scheduler
        self.options = options
        self._daily_state = daily_state
        self._publish = publish_event
        self._clock = clock
        self._log = logging.getLogger(__name__)

    def prepare(
        self, text: str, *, source: str, include_photo: bool | None,
        include_daily_state: bool = False, options: ConversationOptions | None = None,
        recent_messages: list[dict] | None = None,
    ) -> PreparedConversation:
        """Prepare this turn's context through the supplied memory/camera ports."""
        selected = options if options is not None else self.options()
        recent = self._memory.recent_messages() if recent_messages is None else list(recent_messages)
        recent = recent_context(recent)
        bundle = self._memory.prepare_prompt_context(text, recent_messages=recent)
        memory_text = bundle["prompt_text"]
        if include_daily_state:
            daily = dict(self._daily_state())
            if recent_messages is not None:
                # The durable assistant state is shared, ephemeral conversation
                # text and raw events are private to the current source/session.
                daily["recent_messages"] = recent
                for key in ("recent_events", "recent_activity", "recent_failures", "pending_memories"):
                    daily.pop(key, None)
            memory_text += "\n\nDaily assistant state:\n" + daily_context(daily)
        image_bytes = None
        photo_ms = 0.0
        if selected.attach_photo(source, include_photo):
            started = self._clock()
            image_bytes = self._capture_image(
                max_width=selected.vision_max_width, quality=selected.vision_jpeg_quality,
            )
            photo_ms = round((self._clock() - started) * 1000, 1)
        return PreparedConversation(recent, memory_text, image_bytes, photo_ms)

    def ask(
        self, *args, timeout_s: float = 40.0,
        background: bool = False,
        cancelled: CancellationSignal | None = None,
        is_current: Callable[[], bool] | None = None,
        handoff_after_s: float | None = None,
        on_defer: Callable[[Future, float], dict] | None = None, **kwargs,
    ) -> dict:
        """One bounded attempt; cancelled/expired queued requests never enter I/O.

        Polling is confined to active requests (20 Hz), not idle robot loops.
        We never replay an attempt: the provider may already have processed it.
        """
        timeout_s = float(timeout_s)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Conversation deadline must be finite and positive")
        deadline = self._clock() + timeout_s
        handoff_at = (self._clock() + max(0, float(handoff_after_s))
                      if handoff_after_s is not None and on_defer is not None else None)
        future = None

        def current() -> bool:
            return not (cancelled is not None and cancelled.is_set()) and (is_current is None or is_current())

        def invoke() -> dict:
            if not current():
                return {"cancelled": True}
            if self._clock() >= deadline:
                raise FutureTimeout("Request expired before provider execution")
            return self._provider.ask(*args, **kwargs)

        try:
            if not current():
                return {"cancelled": True}
            scheduler = self._background_scheduler if background else self._scheduler
            future = scheduler.submit(invoke)
            while True:
                if not current():
                    future.cancel()
                    return {"cancelled": True}
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise FutureTimeout("Language model request deadline exceeded")
                if handoff_at is not None and self._clock() >= handoff_at and not future.done():
                    # Transfer the same Future only after durable admission.
                    return on_defer(future, deadline)
                try:
                    result = future.result(timeout=min(0.05, remaining))
                except FutureTimeout:
                    if not future.done():
                        continue
                    # A provider may itself raise TimeoutError; do not keep
                    # spinning on an already completed failed future.
                    result = future.result()
                if not current():
                    return {"cancelled": True}
                if self._clock() >= deadline:
                    raise FutureTimeout("Language model response arrived after its deadline")
                return result
        except Exception as error:
            if future is not None:
                future.cancel()
            if not current():
                return {"cancelled": True}
            if isinstance(error, BusyError):
                kind, reason = "llm.busy", "capacity"
                message = "I am busy right now. Please try again shortly."
            elif isinstance(error, FutureTimeout):
                kind, reason = "llm.timeout", "timeout"
                message = "The language model did not respond in time. Please try again."
            else:
                kind, reason = "llm.failed", "provider_error"
                message = "I could not reach the language model. Check the connection and provider settings."
            self._log.warning("Language model request %s: %s", reason, error)
            try:
                self._publish(kind, {"error": str(error), "reason": reason, "timeout_s": timeout_s, "background": background})
            except Exception:
                self._log.exception("Could not publish language model failure")
            return {"face": "error", "speak": message, "error": reason}

    def executable_action(self, action: dict, source: str) -> dict:
        if source != "telegram_text":
            return action
        options = self.options()
        validate_action(action)
        executable = dict(action or {})
        if not options.telegram_speak:
            for key in ("speak", "sound", "face", "animation", "filming_sequence", "video", "sequence", "gesture", "face_animation"):
                executable.pop(key, None)
        if not options.telegram_actions:
            def without_robot_actions(step: dict) -> dict:
                # Named workflows can contain motor commands; pruning just the
                # top level would still let sequences/apps bypass permission.
                filtered = {key: value for key, value in step.items()
                            if key not in {"drive", "slam", "app", "app_tool", "app_manager", "tool_followup", "animation", "filming_sequence", "gesture"}}
                if isinstance(filtered.get("sequence"), list):
                    filtered["sequence"] = [without_robot_actions(child) for child in filtered["sequence"]]
                return filtered
            executable = without_robot_actions(executable)
        return executable

    def shortcuts_allowed(self, source: str) -> bool:
        return source != "telegram_text" or self.options().telegram_actions

    @staticmethod
    def extra_instructions(source: str, include_photo: bool, vision_followup: bool) -> str:
        parts = []
        if source == "speech_input":
            parts.append("This request came from speech input. Favor a natural spoken flow and avoid asking the user to press UI controls.")
        if not include_photo and not vision_followup:
            parts.append("If a good answer depends on what Robbi can currently see and no image is attached yet, prefer take_picture instead of guessing or claiming you cannot see.")
        if vision_followup:
            parts.append("A fresh camera image is attached now. Answer the original request using that image. Do not ask to take another picture unless the image is unusable.")
        parts.append(
            "Do not include drive during normal answers. Only drive if the user explicitly asks Robbi to move, drive, turn, roll, dance, or do a physical motion. "
            "For personality reactions prefer face_animation or a short registered happy-wiggle/tiny-no gesture; "
            "never improvise long forward/backward movement as a casual reaction. Registered dance gestures remain bounded by their encoder envelope and safety checks."
        )
        return " ".join(parts)
