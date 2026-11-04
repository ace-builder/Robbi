from __future__ import annotations
import time
from robbi.configuration.settings import settings
from ..logging_utils import trace_event
from ..vision_skills import classify_vision_skill, normalize_vision_skill, skill_prompt,person_facing_request

class VisionDomain:
    def _parse_take_picture_spec(self, action: dict) -> dict | None:
        if not isinstance(action, dict):
            return None
        value = action.get("take_picture")
        vision_value = action.get("vision_skill")
        if not value:
            if not vision_value:
                return None
            value = True
        prompt = ""
        save_photo = False
        vision_skill = ""
        if isinstance(value, bool):
            if not value:
                return None
        elif isinstance(value, str):
            prompt = value.strip()
        elif isinstance(value, dict):
            prompt = str(value.get("prompt") or "").strip()
            save_photo = bool(value.get("save_photo", False))
            vision_skill = str(value.get("vision_skill") or value.get("skill") or "").strip()
        else:
            return None
        if isinstance(vision_value, str):
            vision_skill = vision_value
        elif isinstance(vision_value, dict):
            vision_skill = str(vision_value.get("skill") or "").strip()
            prompt = prompt or str(vision_value.get("prompt") or "").strip()
        vision_skill = normalize_vision_skill(vision_skill or classify_vision_skill(prompt))
        return {"prompt": prompt, "save_photo": save_photo, "vision_skill": vision_skill}

    def _strip_take_picture_action(self, action: dict) -> dict:
        cleaned = dict(action or {})
        cleaned.pop("take_picture", None)
        cleaned.pop("vision_skill", None)
        return cleaned

    def _normalize_take_picture_ack_action(self, action: dict, forced: bool = False) -> dict:
        normalized = dict(action or {})
        if forced:
            return {
                "face": str(normalized.get("face") or settings.voice_ready_face or "thinking").strip(),
                "speak": "Sure. Let me take a look.",
            }
        if not self._has_presentational_content(normalized):
            normalized = {
                "face": settings.voice_ready_face or "thinking",
                "speak": "Sure. Let me take a look.",
            }
        return normalized

    def _has_presentational_content(self, action: dict) -> bool:
        if not isinstance(action, dict):
            return False
        if any(action.get(key) for key in ("speak", "face", "sound", "animation", "filming_sequence", "video", "drive", "gesture", "face_animation")):
            return True
        sequence = action.get("sequence")
        return isinstance(sequence, list) and any(isinstance(step, dict) for step in sequence)

    def _should_force_take_picture_followup(self, text: str, source: str, action: dict) -> bool:
        return self._looks_like_vision_question(text)

    def _looks_like_vision_question(self, text: str) -> bool:
        if person_facing_request(text):return True
        lowered = (text or "").strip().lower()
        if not lowered:
            return False
        phrases = (
            "what do you see",
            "what can you see",
            "what are you seeing",
            "can you see",
            "take a look",
            "look at",
            "what's on my desk",
            "what is on my desk",
            "what is in front of you",
            "what's in front of you",
            "describe what you see",
            "describe this",
            "what color is",
            "is there",
            "do you see",
        )
        return any(phrase in lowered for phrase in phrases)

    def _action_refuses_vision(self, action: dict) -> bool:
        text = self._collect_spoken_text(action).lower()
        if not text:
            return False
        refusal_markers = (
            "i can't see",
            "i cannot see",
            "i dont have vision",
            "i don't have vision",
            "i donâ€™t have vision",
            "i don't have your camera feed",
            "i donâ€™t have your camera feed",
            "tell me what you're looking at",
            "tell me what youâ€™re looking at",
        )
        return any(marker in text for marker in refusal_markers)

    def _start_take_picture_followup(
        self,
        request_id: int,
        user_text: str,
        source: str,
        provider: str,
        model: str,
        recent_messages: list[dict],
        memory_text: str,
        immediate_action: dict,
        take_picture_spec: dict,
    ) -> dict | None:
        def runner() -> None:
            if not self._request_current(request_id):
                return
            photo_started_at = time.perf_counter()
            capture = getattr(self.camera, 'capture_llm_image_bytes', self.camera.get_llm_image_bytes)
            image_bytes = capture(
                max_width=settings.vision_max_width,
                quality=settings.vision_jpeg_quality,
            )
            if not self._request_current(request_id):
                return
            photo_ms = round((time.perf_counter() - photo_started_at) * 1000, 1)
            self._update_pipeline_metrics(
                request_id,
                vision_followup_photo_ms=photo_ms,
                vision_followup_photo_bytes=len(image_bytes or b""),
            )
            if take_picture_spec.get("save_photo"):
                try:
                    self.capture_photo()
                except Exception as exc:
                    self._log.warning("Failed to save follow-up photo: %s", exc)
            if not image_bytes:
                fallback = {
                    "face": "error",
                    "speak": "I tried to look, but the camera is not ready.",
                }
                executable_fallback = self._action_for_source_execution(fallback, source)
                if executable_fallback:
                    self.execute_action(executable_fallback, request_id=request_id)
                if not self._request_current(request_id):
                    return
                self._remember_followup_result(source, fallback)
                trace_event(
                    "vision_followup_failed",
                    request_id=request_id,
                    source=source,
                    provider=provider,
                    model=model,
                    error="camera_unavailable",
                )
                return
            followup_recent_messages = list(recent_messages or [])
            ack_text = self._collect_spoken_text(immediate_action)
            if ack_text:
                followup_recent_messages.append({"role": "assistant", "content": ack_text})
            vision_skill = normalize_vision_skill(take_picture_spec.get("vision_skill"))
            if vision_skill != "generic_description" or take_picture_spec.get("prompt"):
                followup_prompt = skill_prompt(vision_skill, user_text, take_picture_spec.get("prompt", ""))
            else:
                followup_prompt = self._build_take_picture_followup_prompt(
                    user_text=user_text,
                    take_picture_prompt=take_picture_spec.get("prompt", ""),
                )
            llm_started_at = time.perf_counter()
            followup_action = self._ask_llm_with_deadline(
                followup_prompt,
                request_id=request_id,
                image_bytes=image_bytes,
                provider=provider,
                model=model,
                recent_messages=followup_recent_messages,
                memory_text=memory_text,
                soul_text=self._soul_text,
                tools_text=self._tools_prompt_context(),
                app_catalog_text=self.apps.prompt_catalog(),
                extra_instructions=self._source_extra_instructions(
                    source=source,
                    include_photo=True,
                    vision_followup=True,
                ),
            )
            if not self._request_current(request_id):
                return
            llm_ms = round((time.perf_counter() - llm_started_at) * 1000, 1)
            self._update_pipeline_metrics(request_id, vision_followup_llm_ms=llm_ms)
            followup_action = self._strip_take_picture_action(followup_action)
            followup_action = self._sanitize_llm_motion(followup_action, user_text=user_text)
            if not self._has_presentational_content(followup_action):
                followup_action = {"speak": "I took a look, but I do not have a useful answer yet."}
            trace_event(
                "vision_followup_result",
                request_id=request_id,
                source=source,
                provider=provider,
                model=model,
                llm_ms=llm_ms,
                action=followup_action,
            )
            executable_followup = self._action_for_source_execution(followup_action, source)
            if executable_followup:
                self.execute_action(executable_followup, request_id=request_id)
            if not self._request_current(request_id):
                return
            self._remember_followup_result(source, followup_action)

        try:
            self._submit_guarded(self._research_executor, runner)
        except RuntimeError as error:
            if not self._request_current(request_id):
                return {"cancelled": True}
            self._log.info("Could not queue camera follow-up: %s", error)
            self._publish_event("vision.followup_failed", {"request_id": request_id, "error": str(error)})
            return {"face": "error", "speak": "I am busy and could not queue the camera follow-up. Please try again.", "error": "busy"}
        return None

    def _build_take_picture_followup_prompt(self, user_text: str, take_picture_prompt: str) -> str:
        prompt = take_picture_prompt.strip()
        if prompt.startswith("Vision skill:") or "Vision skill:" in prompt:
            return prompt
        if prompt:
            return (
                "You previously decided that a fresh camera image was needed. "
                f"The user's original request was: {user_text}\n"
                f"Additional guidance for this second pass: {prompt}\n"
                "Answer the original request now using the attached image."
            )
        return (
            "You previously decided that a fresh camera image was needed. "
            f"The user's original request was: {user_text}\n"
            "Answer that original request now using the attached image."
        )

    def _collect_spoken_text(self, action: dict) -> str:
        spoken_parts: list[str] = []
        speak = str((action or {}).get("speak", "")).strip()
        if speak:
            spoken_parts.append(speak)
        sequence = (action or {}).get("sequence")
        if isinstance(sequence, list):
            for step in sequence:
                if not isinstance(step, dict):
                    continue
                speak_part = str(step.get("speak", "")).strip()
                if speak_part:
                    spoken_parts.append(speak_part)
        return " ".join(spoken_parts).strip()

    def _remember_followup_result(self, source: str, action: dict) -> None:
        from robbi.services.conversation_feed import finish_followup
        finish_followup(action)
        assistant_text = self._collect_spoken_text(action)
        if assistant_text:
            self.memory.add_assistant_message(assistant_text, source="robot")
        memory_result = self.memory.remember((action or {}).get("remember"), source=source)
        memory_ops_result = self.memory.apply_memory_ops((action or {}).get("memory_ops"), source=source)
        if any(memory_result.values()) or any(memory_ops_result.values()):
            self._log.info("Memory updated after vision follow-up: remember=%s memory_ops=%s", memory_result, memory_ops_result)
