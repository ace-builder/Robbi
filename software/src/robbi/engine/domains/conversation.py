from __future__ import annotations
import time
from robbi.actions import validate_action
from robbi.services.background_work import BackgroundAcknowledgement
from robbi.services.conversation import ConversationOptions
from robbi.services.conversation_feed import publish_conversation
from robbi.services.tool_followups import shortcut
from robbi.configuration.settings import settings
from ..logging_utils import trace_event
from ..vision_skills import classify_vision_skill, skill_prompt
from ..activity import activity
from robbi.orchestration import INTERACTIVE, current_intent
from robbi.activity import record_turn

class ConversationDomain:
    @record_turn
    @publish_conversation
    @activity("conversation", INTERACTIVE)
    def think_and_respond(self, text: str, include_photo: bool | None = None, source: str = "ui_text", conversation_id: str = "") -> dict:
        if getattr(self, '_filming_mode', False):
            return {"speak": "Filming mode is active. Use Filming studio or disable filming mode to talk to Robbi."}
        generation, cancel_event = self._action_token()
        request_id = time.time_ns()
        self._register_request(request_id, generation)
        register_conversation = getattr(self, '_register_conversation_request', None)
        if register_conversation is not None:
            register_conversation(request_id, source, conversation_id)
        begin_followups = getattr(self, "_begin_tool_followup_turn", None)
        scope = begin_followups(source, conversation_id, request_id) if begin_followups else None
        owner = current_intent()
        if (cancel_event.is_set() or not self._request_current(request_id)
                or (owner is not None and owner.is_set())):
            return {"cancelled": True}
        # A conversational turn must not shorten a requested image hold.
        self._publish_event("user.text_received", {"text": text, "source": source})
        manage_apps = getattr(self, "_maybe_run_app_management_request", None)
        management_action = manage_apps(text, source=source, request_id=request_id) if manage_apps else None
        if management_action is not None:
            if management_action.get("cancelled") or not self._request_current(request_id):
                return {"cancelled": True}
            executable = self._action_for_source_execution(management_action, source)
            if executable:
                self.execute_action(executable, request_id=request_id)
            if scope is not None:
                self._remember_followup_turn(text, source, management_action, request_id)
            memory = getattr(self, "memory", None)
            if memory is not None:
                memory.add_user_message(text, source=source)
                memory.add_assistant_message(management_action.get("speak", ""), source="robot")
            return management_action
        operation = shortcut(text)
        if operation and scope is not None:
            action = self._resolve_tool_followup({"operation": operation}, user_text=text,
                                                 source=source, request_id=request_id, explicit=True)
            if action.get("cancelled") or not self._request_current(request_id):
                return {"cancelled": True}
            executable = self._action_for_source_execution(action, source)
            if executable:
                self.execute_action(executable, request_id=request_id)
            self._remember_followup_turn(text, source, action, request_id)
            memory = getattr(self, "memory", None)
            if memory is not None:
                memory.add_user_message(text, source=source)
                memory.add_assistant_message(action.get("speak", ""), source="robot")
            return action
        if self.conversation_service.shortcuts_allowed(source):
            debug_action = self.parse_debug_command(text)
            if debug_action is not None:
                executable = self._action_for_source_execution(debug_action, source)
                if executable:
                    result = self.execute_action(executable, request_id=request_id)
                    if scope is not None:
                        self._remember_shortcut_receipt("debug_command", {"request": text, "action": executable}, result,
                                                        source=source, request_id=request_id)
                    return result
                return debug_action
            host = getattr(self, 'app_host', None)
            if host is not None and hasattr(host, 'packages'):
                from robbi.mini_games import local_game_request
                from robbi.expression_catalog import local_expression_request
                game_call = local_expression_request(host, text) or local_game_request(
                    host, text, allow_active=source in {'speech_input', 'ui_text'})
                if game_call is not None:
                    self.behavior.note_interaction()
                    self._touch_activity()
                    result = self._execute_app_tool(game_call, source=source, request_id=request_id)
                    if not self._request_current(request_id) or result.get('cancelled'):
                        return {'cancelled': True}
                    self._remember_app_display(request_id, result)
                    action = {'speak': self._app_result_text(result),
                              'app_result': self._app_result_metadata(game_call, result)}
                    self.memory.add_user_message(text, source=source)
                    self.memory.add_assistant_message(action['speak'], source='robot')
                    self.execute_action(action, request_id=request_id)
                    self._remember_followup_turn(text, source, action, request_id)
                    return action
            app_action = self._maybe_run_special_app_request(text, source=source)
            if app_action is not None:
                if scope is not None:
                    self._remember_shortcut_receipt("legacy_app", {"request": text}, app_action,
                                                    source=source, request_id=request_id)
                return app_action
        elif (text.strip().startswith("/") or self._looks_like_focus_timer_request_v2(text)
              or self._looks_like_focus_timer_stop_request_v2(text)):
            # The debug parser itself mutates volume/mute, and special app
            # shortcuts execute immediately. Check before calling either one.
            self._publish_event("action.denied", {"source": source, "reason": "remote_robot_actions_disabled"})
            return {"speak": "Remote robot actions are disabled. Enable them in Telegram settings to use robot commands."}
        self.behavior.note_interaction()
        self._touch_activity()
        route_background = getattr(self, "_route_background_request", None)
        if route_background is not None:
            deferred = route_background(text, source, request_id, include_photo)
            if deferred is not None:
                return deferred
        options = self.conversation_service.options()
        snapshot = self.behavior.snapshot()
        thinking_face = self.assets.get("faces", "thinking", extensions=(".png",))
        thinking_sound = self._pick_thinking_sound()
        previous_face = snapshot.current_face
        source_provider, source_model = options.target(source)
        metrics = {
            "source": source,
            "provider": source_provider,
            "model": source_model,
            "text_chars": len(text),
            "include_photo_requested": bool(include_photo) if include_photo is not None else None,
            "include_photo_default": options.default_photo(source),
            "include_photo_used": False,
            "photo_bytes": 0,
            "photo_prepare_ms": 0.0,
            "llm_ms": 0.0,
            "action_ms": 0.0,
            "total_ms": 0.0,
            "tts_provider": options.tts_provider,
            "vision_followup_requested": False,
            "vision_followup_prompt": "",
            "vision_followup_photo_bytes": 0,
            "vision_followup_photo_ms": 0.0,
            "vision_followup_llm_ms": 0.0,
            "tool_ms": 0.0,
        }
        started_at = time.perf_counter()
        self._store_pipeline_metrics(request_id, metrics)
        trace_event(
            "pipeline_request",
            request_id=request_id,
            source=source,
            text=text,
            include_photo_requested=include_photo,
            provider=source_provider,
            model=source_model,
        )
        if thinking_face is not None:
            self._set_presentation(face=thinking_face.name, thinking=True, last_command=text)
        if thinking_sound is not None and source == "speech_input":
            self._play_delayed_thinking_sound(thinking_sound, request_id=request_id, delay_s=0.6)
        scoped_recent = {"recent_messages": self._tool_followup_store().messages(scope)} if scope is not None else {}
        feed=getattr(self,'conversation_feed',None)
        if feed is not None:
            restored=feed.context(text,source,conversation_id)
            if restored:scoped_recent={'recent_messages':restored}
        prepared = self.conversation_service.prepare(
            text, source=source, include_photo=include_photo,
            include_daily_state=self._looks_like_daily_request(text), options=options,
            **scoped_recent,
        )
        recent_messages = prepared.recent_messages
        memory_prompt_text = prepared.memory_text
        if scope is not None:
            memory_prompt_text += "\n\n" + self._tool_followup_prompt(source, request_id)
        image_bytes = prepared.image_bytes
        metrics["photo_prepare_ms"] = prepared.photo_prepare_ms
        metrics["photo_bytes"] = len(image_bytes or b"")
        metrics["include_photo_used"] = bool(image_bytes)
        if not self._request_current(request_id):
            return {"cancelled": True}
        attached_vision_skill = classify_vision_skill(text) if image_bytes else ""
        if not image_bytes and source == "speech_input" and self._looks_like_vision_question(text):
            skill = classify_vision_skill(text)
            immediate_action = {
                "face": settings.voice_ready_face or "thinking",
                "speak": "Sure. Let me take a look.",
            }
            metrics["vision_followup_requested"] = True
            metrics["vision_followup_prompt"] = skill_prompt(skill, text)
            action_started_at = time.perf_counter()
            executable_immediate = self._action_for_source_execution(immediate_action, source)
            if executable_immediate:
                self.execute_action(executable_immediate, request_id=request_id)
            if not self._request_current(request_id):
                return {"cancelled": True}
            metrics["action_ms"] = round((time.perf_counter() - action_started_at) * 1000, 1)
            metrics["total_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
            metrics["action_keys"] = sorted(immediate_action.keys())
            self._update_pipeline_metrics(request_id, **metrics)
            trace_event(
                "vision_followup_requested",
                request_id=request_id,
                source=source,
                provider=source_provider,
                model=source_model,
                prompt=metrics["vision_followup_prompt"],
                forced=True,
                immediate_action=immediate_action,
                shortcut=True,
            )
            self.memory.add_user_message(text, source=source)
            followup_error = self._start_take_picture_followup(
                request_id=request_id,
                user_text=text,
                source=source,
                provider=source_provider,
                model=source_model,
                recent_messages=recent_messages,
                memory_text=memory_prompt_text,
                immediate_action=immediate_action,
                take_picture_spec={"prompt": metrics["vision_followup_prompt"], "save_photo": False, "vision_skill": skill},
            )
            return followup_error or immediate_action
        extra_instructions = self._source_extra_instructions(
            source=source,
            include_photo=bool(image_bytes),
            vision_followup=False,
        )
        if attached_vision_skill:
            extra_instructions = f"{extra_instructions} {skill_prompt(attached_vision_skill, text)}"
        llm_started_at = time.perf_counter()
        responsive = getattr(self, "_ask_responsive_conversation", None)
        ask = responsive if responsive is not None else self._ask_llm_with_deadline
        source_args = {"source": source} if responsive is not None else {}
        resume_weather = getattr(self, '_weather_followup_action', None)
        pending_weather = resume_weather(text, source, request_id) if resume_weather else None
        action = pending_weather or ask(
            text,
            request_id=request_id,
            image_bytes=image_bytes,
            provider=source_provider,
            model=source_model,
            recent_messages=recent_messages,
            memory_text=memory_prompt_text,
            soul_text=self._soul_text,
            tools_text=self._tools_prompt_context(text),
            app_catalog_text=self.apps.prompt_catalog(),
            extra_instructions=extra_instructions,
            **source_args,
        )
        metrics["llm_ms"] = round((time.perf_counter() - llm_started_at) * 1000, 1)
        if not self._request_current(request_id):
            return {"cancelled": True}
        if isinstance(action, BackgroundAcknowledgement):
            return action
        from robbi.services.conversation_feed import social_assessment
        social_assessment(self,action)
        validate_action(action)
        defer_action = getattr(self, "_defer_background_action", None)
        if defer_action is not None:
            deferred = defer_action(action, text, source, request_id, prepared, source_provider, source_model)
            if deferred is not None:
                return deferred
        tool_started_at = time.perf_counter()
        action = self._maybe_run_tool_followup(
            action=action,
            user_text=text,
            source=source,
            provider=source_provider,
            model=source_model,
            recent_messages=recent_messages,
            memory_text=memory_prompt_text,
            image_bytes=image_bytes,
            request_id=request_id,
        )
        if not self._request_current(request_id):
            return {"cancelled": True}
        action = self._apply_immediate_runtime_tools(action, source=source, request_id=request_id)
        action = self._sanitize_llm_motion(action, user_text=text)
        metrics["tool_ms"] = round((time.perf_counter() - tool_started_at) * 1000, 1)
        preserve_app_display = self._app_preserves_display(request_id) or self._app_preserves_display()
        restore_face = self.assets.get("faces", previous_face, extensions=(".png",))
        if preserve_app_display:
            self._set_presentation(thinking=False)
        elif restore_face is not None:
            self._set_presentation(face=restore_face.name, thinking=False)
        take_picture_spec = self._parse_take_picture_spec(action)
        forced_take_picture = False
        if take_picture_spec is None and not image_bytes and not preserve_app_display and self._should_force_take_picture_followup(text, source, action):
            take_picture_spec = {"prompt": "", "save_photo": False}
            forced_take_picture = True
        if take_picture_spec is not None and not image_bytes:
            immediate_action = self._strip_take_picture_action(action)
            immediate_action = self._normalize_take_picture_ack_action(immediate_action, forced=forced_take_picture)
            metrics["vision_followup_requested"] = True
            metrics["vision_followup_prompt"] = take_picture_spec["prompt"]
            action_started_at = time.perf_counter()
            self._stop_transient_thinking_audio()
            executable_immediate = self._action_for_source_execution(immediate_action, source)
            if executable_immediate:
                self.execute_action(executable_immediate, request_id=request_id)
            if not self._request_current(request_id):
                return {"cancelled": True}
            metrics["action_ms"] = round((time.perf_counter() - action_started_at) * 1000, 1)
            metrics["total_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
            metrics["action_keys"] = sorted(immediate_action.keys())
            self._update_pipeline_metrics(request_id, **metrics)
            trace_event(
                "vision_followup_requested",
                request_id=request_id,
                source=source,
                provider=source_provider,
                model=source_model,
                prompt=take_picture_spec["prompt"],
                forced=forced_take_picture,
                immediate_action=immediate_action,
            )
            self.memory.add_user_message(text, source=source)
            followup_error = self._start_take_picture_followup(
                request_id=request_id,
                user_text=text,
                source=source,
                provider=source_provider,
                model=source_model,
                recent_messages=recent_messages,
                memory_text=memory_prompt_text,
                immediate_action=immediate_action,
                take_picture_spec=take_picture_spec,
            )
            return followup_error or immediate_action
        action_started_at = time.perf_counter()
        self._stop_transient_thinking_audio()
        executable_action = self._action_for_source_execution(action, source)
        if executable_action:
            self.execute_action(executable_action, request_id=request_id)
        else:
            spoken_text = self._collect_spoken_text(action)
            if spoken_text:
                self._set_presentation(last_speak=spoken_text)
        if not self._request_current(request_id):
            return {"cancelled": True}
        self._remember_interaction(text, source, action)
        if scope is not None:
            self._remember_followup_turn(text, source, action, request_id)
        self._queue_memory_extraction(text, source, action)
        metrics["action_ms"] = round((time.perf_counter() - action_started_at) * 1000, 1)
        metrics["total_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
        metrics["action_keys"] = sorted(action.keys())
        self._update_pipeline_metrics(request_id, **metrics)
        trace_event(
            "pipeline_result",
            request_id=request_id,
            source=source,
            provider=source_provider,
            model=source_model,
            metrics=metrics,
            action=action,
        )
        self._log.info(
            "Response pipeline: request_id=%s source=%s photo=%s photo_bytes=%s photo_prepare_ms=%s llm_ms=%s action_ms=%s total_ms=%s",
            request_id,
            source,
            metrics["include_photo_used"],
            metrics["photo_bytes"],
            metrics["photo_prepare_ms"],
            metrics["llm_ms"],
            metrics["action_ms"],
            metrics["total_ms"],
        )
        return action

    def _action_for_source_execution(self, action: dict, source: str) -> dict:
        return self.conversation_service.executable_action(action, source)

    def _ask_llm_with_deadline(self, *args, timeout_s: float = 40.0, request_id: int | None = None, background: bool = False, **kwargs) -> dict:
        background_context = getattr(getattr(self, "_action_local", None), "background_task", None)
        if background_context is not None:
            kwargs.pop("on_defer", None)
            kwargs.pop("handoff_after_s", None)
            return self._background_provider_ask(*args, context=background_context, **kwargs)
        generation, cancelled = self._action_token()
        return self.conversation_service.ask(
            *args, timeout_s=timeout_s, cancelled=cancelled, background=background,
            is_current=lambda: self._action_current(generation) and self._request_current(request_id), **kwargs,
        )

    @staticmethod
    def _conversation_options() -> ConversationOptions:
        return ConversationOptions(
            speech_provider=settings.speech_input_provider, speech_model=settings.speech_input_model,
            text_provider=settings.ui_text_provider, text_model=settings.ui_text_model,
            attach_speech_photo=settings.attach_photo_for_speech_input,
            attach_text_photo=settings.attach_photo_for_ui_text,
            vision_max_width=settings.vision_max_width, vision_jpeg_quality=settings.vision_jpeg_quality,
            tts_provider=settings.tts_provider, telegram_speak=settings.telegram_speak_remote_messages,
            telegram_actions=settings.telegram_allow_robot_actions,
        )

    def _touch_activity(self) -> None:
        self._last_activity_ts = time.time()

    def _default_photo_mode(self, source: str) -> bool:
        return self.conversation_service.options().default_photo(source)

    def _llm_target_for_source(self, source: str) -> tuple[str, str]:
        return self.conversation_service.options().target(source)

    def _should_attach_photo(self, source: str, include_photo: bool | None) -> bool:
        return self.conversation_service.options().attach_photo(source, include_photo)

    def _source_extra_instructions(self, source: str, include_photo: bool, vision_followup: bool) -> str:
        instructions = self.conversation_service.extra_instructions(source, include_photo, vision_followup)
        if not vision_followup and getattr(self, "_background_enabled", lambda: False)():
            instructions += (
                ' For substantial research, detailed reasoning or other long work, you may return {"background_task": {}}. '
                'The runtime will acknowledge and work on the original user request; do not claim the work is already complete. '
                'Use normal actions for quick local tools and physical requests.'
            )
        return instructions
