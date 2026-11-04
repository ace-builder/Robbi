"""Controller adapters for durable work; computation never owns foreground outputs."""
from __future__ import annotations

import copy
import re
import time

from robbi.actions import validate_action, validate_app_tool
from robbi.errors import BusyError, ValidationError
from robbi.orchestration import BACKGROUND, INTERACTIVE, Intent, intent_scope
from robbi.services.background_work import BackgroundWorkOptions, BackgroundAcknowledgement
from robbi.activity import turn_scope
from robbi.configuration.settings import settings
from ..lifecycle import UnavailableComponent


class BackgroundWorkDomain:
    PROGRESS_DELAY_S = 5.0

    @staticmethod
    def _news_request(text):
        return bool(re.search(r"\b(latest|current|today.?s|recent|aktuelle|heutige|neueste)\b.{0,45}\b(news|headlines|events|nachrichten|schlagzeilen)\b|\b(news|headlines|nachrichten)\b.{0,25}\b(today|right now|heute)\b", text.casefold()))

    @staticmethod
    def _background_work_options():
        defaults = BackgroundWorkOptions()
        return BackgroundWorkOptions(**{
            name: getattr(settings, "background_work_" + name, getattr(defaults, name))
            for name in defaults.__dataclass_fields__
        })

    def background_work_snapshot(self):
        service = getattr(self, "background_work", None)
        if isinstance(service, UnavailableComponent):
            return {"enabled": False, "active": 0, "tasks": [], "error": service.error}
        return service.snapshot(include_results=False) if service is not None else {"enabled": False, "active": 0, "tasks": []}

    def cancel_background_work(self, task_id):
        return self.background_work.cancel(task_id)

    def show_background_result(self, task_id, artifact_id=None):
        """Explicit dashboard action; no background screen replacement occurs."""
        task = self.background_work.get(task_id)
        metadata = (task.get("result") or {}).get("app_result") or {}
        app_id = metadata.get("app")
        artifacts = (metadata.get("result") or {}).get("artifacts") or []
        selected = next((item for item in artifacts if artifact_id is None or item.get("id") == artifact_id), None)
        if not app_id or not selected:
            raise ValidationError("This task has no retained image artifact")
        host = self.app_host
        project = host.project(app_id)
        if not project.bundled or app_id != "image-studio":
            raise ValidationError("Open this artifact in its app to review display options")
        token = self._action_token()
        with intent_scope(Intent("background-result-view", INTERACTIVE, cancelled=token[1])):
            return host.call(app_id, "show", {"artifact_id": selected["id"]}, source="ui_text",
                             cancellation=token[1].is_set, deadline=time.monotonic() + 10)

    @staticmethod
    def _long_work_request(text):
        text = " ".join(text.casefold().split())
        # Physical requests keep the ordinary immediate safety/action path.
        if re.search(r"\b(drive|move|turn|roll|dance|explore|fahr|drehe|bewege)\b", text):
            return False
        if BackgroundWorkDomain._news_request(text):
            return True
        if re.search(r"\b(search (?:the )?(?:web|internet)|look (?:this|that|it) up|look up|web search|suche im internet)\b", text):
            return True
        return bool(re.search(
            r"\b(in the background|in background|im hintergrund|take your time|research|investigate|recherchiere|deep dive|in detail|detailed analysis)\b"
            r"|\b(compare|vergleiche)\b.{8,}\b(options|alternatives|versus|with|and|und)\b"
            r"|\b(draw|generate|create|zeichne|generiere)\b.{0,60}\b(image|picture|diagram|pictogram|bild|diagramm)\b", text))

    def _background_enabled(self):
        service = getattr(self, "background_work", None)
        return service is not None and not isinstance(service, UnavailableComponent) and self._background_work_options().enabled

    def _route_background_request(self, text, source, request_id, include_photo):
        if not self._background_enabled():
            return None
        normalized = " ".join(text.casefold().strip(" .?!").split())
        if normalized in {"what are you working on", "background tasks", "what is running in the background"}:
            snapshot = self.background_work.snapshot(include_results=False)
            active = [row for row in snapshot["tasks"] if row["status"] in {"queued", "running"}]
            answer = {"speak": (f"I have {len(active)} background task(s): " + "; ".join(row["text"][:100] for row in active)) if active else "There are no background tasks running.", "background_work": snapshot}
            return self._background_immediate_answer(answer, text, source, request_id)
        match = re.fullmatch(r"cancel task ([a-f0-9]{32})", normalized)
        if match:
            try:
                result = self.background_work.cancel(match.group(1))
                answer = {"speak": "Task cancelled.", "background_task": result}
            except KeyError:
                answer = {"speak": "I could not find that background task."}
            return self._background_immediate_answer(answer, text, source, request_id)
        # The first model turn chooses tools and supplies its acknowledgement.
        return None

    def _background_prompt_kwargs(self, prepared, provider, model, query=""):
        return dict(provider=provider, model=model, image_bytes=prepared.image_bytes,
                    recent_messages=prepared.recent_messages, memory_text=prepared.memory_text,
                    soul_text=self._soul_text, tools_text=self._tools_prompt_context(query),
                    app_catalog_text=self.apps.prompt_catalog(),
                    extra_instructions="Complete this requested background task. Use permitted search/app tools when needed. Return text and artifacts only; never drive, start legacy apps, or request another background task.")

    def _background_provider_ask(self, text, context, **kwargs):
        context.checkpoint()
        kwargs["request_timeout_s"] = max(.01, min(300, kwargs.get("request_timeout_s", 300), context.deadline - time.monotonic()))
        result = self.llm.ask(text, **kwargs)
        context.checkpoint()
        return result

    def _in_background_context(self, context, generation, callback):
        previous = getattr(self._action_local, "token", None)
        prior_task = getattr(self._action_local, "background_task", None)
        self._action_local.token = (generation, context)
        self._action_local.background_task = context
        request_id = time.time_ns()
        self._register_request(request_id, generation)
        scope=getattr(self._action_local,'background_scope',None)
        if scope is not None:
            self._tool_followup_store().bind_request(request_id,scope[0],scope[1])
        try:
            context.checkpoint()
            with intent_scope(Intent("background:" + context.id, BACKGROUND, cancelled=context)):
                return callback(request_id)
        finally:
            self._action_local.token = previous
            self._action_local.background_task = prior_task

    def _admit_background(self, text, source, provider, model, work, cancel_event, request_id, acknowledgement=""):
        work=self._background_scoped_work(work,text,source,request_id)
        try:
            task = self.background_work.submit(text=text, source=source, provider=provider, model=model, work=work, cancellation=cancel_event, reply_requested=source in {"ui_text", "speech_input", "voice"})
        except BusyError as error:
            return self._background_immediate_answer({"speak": str(error), "error": "busy"}, text, source, request_id)
        self._track_background_turn(task, source, request_id)
        message = str(acknowledgement or "")[:500]
        # The worker can finish before admission returns. Do not queue stale speech.
        if self.background_work.get(task['id'])['status'] not in {'queued', 'running'}:
            message = ''
        result = BackgroundAcknowledgement(speak=message, background_task=task)
        return self._background_immediate_answer(result, text, source, request_id)

    def _track_background_turn(self, task, source, request_id, *, overdue=False):
        turns = getattr(self, '_background_turns', None)
        if turns is None:
            turns = self._background_turns = {}
        getter = getattr(self, '_tool_scope_source', None)
        turns[task['id']] = dict(source=source, scope=getter(source, request_id) if getter else source,
                                progress_due=time.monotonic() + (0 if overdue else self.PROGRESS_DELAY_S), progress_started=False)
        while len(turns) > 32:
            turns.pop(next(iter(turns)))

    def _background_scoped_work(self, work, text, source, request_id):
        getter=getattr(self,'_tool_followup_store',None)
        if getter is None:return work
        store=getter()
        scope=store.scope(source,request_id)
        def run(context):
            previous=getattr(self._action_local,'background_scope',None)
            self._action_local.background_scope=(source,scope)
            try:
                result=work(context)
                context.checkpoint()
                if isinstance(result,dict) and result.get('speak') and not result.get('cancelled'):
                    store.remember_turn(scope,'','Background result for '+text[:240]+': '+result['speak'])
                return result
            finally:
                self._action_local.background_scope=previous
        return run

    def _background_immediate_answer(self, answer, text, source, request_id):
        """Fast local acknowledgement/status path; speech remains asynchronous."""
        message = answer["speak"]
        try:
            self._set_presentation(thinking=False, last_speak=message)
            action = self._action_for_source_execution({"speak": message}, source) if message else {}
            if action:
                task = answer.get('background_task')
                if task and isinstance(answer, BackgroundAcknowledgement):
                    # This guard is inherited by asynchronous TTS/playback too.
                    guard = Intent('background-acknowledgement', INTERACTIVE,
                        cancelled=lambda: self.background_work.get(task['id'])['status'] not in {'queued', 'running'})
                    with intent_scope(guard):
                        if not guard.is_set():
                            self.execute_action(action, request_id=request_id)
                else:
                    self.execute_action(action, request_id=request_id)
        except Exception:
            try:
                self._publish_event("presentation.skipped", {"feature": "background_reply"})
            except Exception:
                pass
        try:
            self.memory.add_user_message(text, source=source)
            self.memory.add_assistant_message(message, source="robot")
            getter=getattr(self,'_tool_followup_store',None)
            if getter is not None:
                store=getter()
                store.remember_turn(store.scope(source,request_id),text,message)
        except Exception:
            try:
                self._publish_event("memory.extraction_skipped", {"reason": "background reply could not be remembered"})
            except Exception:
                pass
        return answer

    def _ask_responsive_conversation(self, text, *, source, request_id, **kwargs):
        if not self._background_enabled():
            return self._ask_llm_with_deadline(text, request_id=request_id, **kwargs)
        generation, cancel_event = self._action_token()
        selected = self._background_work_options()
        kwargs.setdefault("request_timeout_s", selected.timeout_s)

        def adopt(future, deadline):
            # Automatically deferred chat is ephemeral. Explicit research tasks
            # use the separate long-work path and survive subsequent chat turns.
            durable = [False]
            explicit = [False]
            chat_expires = time.monotonic() + 30.0
            reply_owner = Intent('deferred-conversation', INTERACTIVE,
                cancelled=lambda: cancel_event.is_set() or
                    (not explicit[0] and not self._request_current(request_id)) or
                    (not durable[0] and time.monotonic() >= chat_expires),
                deadline=deadline)
            def work(context):
                def run(task_request_id):
                    action = context.wait_future(future, deadline)
                    # Once the model actually selects expensive tool work, it
                    # follows the same lifetime as a promptly planned task.
                    durable[0] = bool(isinstance(action, dict) and (
                        any(key in action for key in ('web_search', 'memory_search', 'capability_search', 'background_task')) or
                        ('app_tool' in action and self._background_app_eligible(action['app_tool']))))
                    explicit[0] = isinstance(action, dict) and 'background_task' in action
                    return self._finish_background_action(action, text, source, task_request_id, kwargs, context)
                return self._in_background_context(context, generation, run)
            # Let ConversationService cancel its queued future if admission fails.
            task = self.background_work.submit(text=text, source=source, provider=kwargs.get("provider", ""),
                model=kwargs.get("model", ""), work=self._background_scoped_work(work,text,source,request_id),
                cancellation=reply_owner, reply_requested=source in {"ui_text", "speech_input", "voice"})
            self._track_background_turn(task, source, request_id, overdue=True)
            return self._background_ack_for_adopted(task, text, source, request_id)

        return self._ask_llm_with_deadline(text, request_id=request_id, timeout_s=selected.timeout_s,
            handoff_after_s=self.PROGRESS_DELAY_S, on_defer=adopt, **kwargs)

    def _background_ack_for_adopted(self, task, text, source, request_id):
        # A slow first answer has no model-authored acknowledgement yet. The
        # progress lane writes one without blocking or reissuing the original.
        message = ""
        return self._background_immediate_answer(
            BackgroundAcknowledgement(speak=message, background_task=task), text, source, request_id)

    def _background_app_eligible(self, spec):
        try:
            selected = validate_app_tool(spec)
            host = getattr(self, "app_host", None)
            if host is None:
                return False
            project = host.project(selected["app"])
            tool = next((t for t in project.manifest.get('tools', []) if t['name'] == selected['tool']), None)
            if tool and tool.get('read_only'):
                return True
            permissions = set(project.manifest["permissions"])
            if "robot.motion" in permissions:
                return False
            if permissions & {"robot.display", "robot.speak", "robot.audio"}:
                return bool(project.bundled and selected["app"] == "image-studio" and selected["tool"] == "generate")
            return "network" in permissions
        except (ValueError, RuntimeError, KeyError):
            return False

    def _defer_background_action(self, action, text, source, request_id, prepared, provider, model):
        validate_action(action)
        if not self._background_enabled():
            return None
        if any(key in action for key in ("drive", "slam", "app", "animation", "filming_sequence", "video", "sequence")):
            return None
        if "app_tool" in action and not self._background_app_eligible(action["app_tool"]):
            return None
        if not any(key in action for key in ("app_tool", "web_search", "memory_search", "capability_search", "background_task")):
            return None
        generation, cancel_event = self._action_token()
        selected = self._background_work_options()
        target_provider = selected.provider or provider
        target_model = selected.model or model
        kwargs = self._background_prompt_kwargs(prepared, target_provider, target_model, text)

        def work(context):
            def run(task_request_id):
                initial = (self._background_provider_ask(text, context, **kwargs)
                           if "background_task" in action else action)
                return self._finish_background_action(initial, text, source, task_request_id, kwargs, context)
            return self._in_background_context(context, generation, run)
        if 'background_task' not in action:
            cancel_event = Intent('conversation-tool-chain', INTERACTIVE,
                cancelled=lambda event=cancel_event: event.is_set() or not self._request_current(request_id))
        return self._admit_background(text, source, target_provider, target_model, work, cancel_event, request_id,
                                      acknowledgement=action.get('speak', ''))

    def _finish_background_action(self, action, text, source, request_id, kwargs, context):
        # Provider JSON cannot manufacture delivery instructions or runtime receipts.
        action = {key: value for key, value in action.items()
                  if key not in {'app_result', 'app_management_result', 'tool_chain', 'display_result'}}
        context.checkpoint()
        validate_action(action)
        action = copy.deepcopy(action)
        presentation = None
        # Tool outputs cannot schedule delayed physical actions. The app SDK also
        # checks its existing declaration and owner permission before any I/O.
        if "app_tool" in action:
            selected = validate_app_tool(action["app_tool"])
            host = getattr(self, "app_host", None)
            project = host.project(selected["app"]) if host is not None else None
            tool = next((t for t in project.manifest.get('tools', []) if t['name'] == selected['tool']), None) if project else None
            permissions = (set(project.manifest["permissions"]) if project is not None else set()) if not (tool and tool.get('read_only')) else set()
            if "robot.motion" in permissions:
                return {"speak": "This app can move Robbi and must be run in the foreground.", "error": "foreground_required"}
            outputs = permissions & {"robot.display", "robot.speak", "robot.audio"}
            if outputs:
                if project.bundled and selected["app"] == "image-studio" and selected["tool"] == "generate":
                    selected = copy.deepcopy(selected)
                    if selected['arguments'].get('show_on_screen', True):
                        presentation = {'hold_s': selected['arguments'].get('hold_s', 10)}
                    selected["arguments"]["show_on_screen"] = False
                    action["app_tool"] = selected
                else:
                    return {"speak": "This app uses robot outputs and must be run in the foreground.", "error": "foreground_required"}
        if not action.get("error"):
            action = self._maybe_run_tool_followup(action=action, user_text=text, source=source,
                provider=kwargs.get("provider", ""), model=kwargs.get("model", ""),
                recent_messages=kwargs.get("recent_messages", []), memory_text=kwargs.get("memory_text", ""),
                image_bytes=kwargs.get("image_bytes"), request_id=request_id, deferred_display=presentation)
        if action.get('cancelled'):
            context.partial_result = action
        context.checkpoint()
        result = {"speak": self._collect_spoken_text(action)[:12000] or "The background task finished. Open its result to review it."}
        for key in ('tool_chain', 'decision', 'display_result'):
            if key in action:
                result[key] = action[key]
        if action.get('decision') == 'stop':
            result['speak'] = self._collect_spoken_text(action)
        if action.get("app_result"):
            result["app_result"] = action["app_result"]
            app_result = action["app_result"].get("result", {})
            if app_result.get("ok") is False and not action.get("tool_chain"):
                result["error"] = "app_needs_attention"
            elif (presentation and app_result.get('artifacts') and not result.get('display_result')
                  and action['app_result'].get('app') == 'image-studio'):
                result['display_result'] = {**presentation, 'artifact_id': app_result['artifacts'][0]['id']}
        if action.get("error"):
            result["error"] = str(action["error"])[:200]
        return result

    def drain_background_work_delivery(self):
        """Nonblocking supervisor hook: queue speech only when outputs are idle."""
        service = getattr(self, "background_work", None)
        if service is not None and not isinstance(service, UnavailableComponent):
            self._queue_background_progress()
        if service is None or isinstance(service, UnavailableComponent) or getattr(self, "_voice_processing", False) or time.time() - self._last_activity_ts < 3:
            return
        try:
            behavior = self.behavior.snapshot()
            if not behavior.awake and time.time() - self._last_activity_ts > 300:
                return
            if self.media.is_audio_playing() or self.media.is_video_playing() or self._filming_mode or self.focus_timer.is_active():
                return
            if self.microphone.voice_state() not in {"idle", "disabled"}:
                return
            if self.orchestrator.snapshot().get("resources"):
                return
            audio_available = not behavior.muted and self.media.health().get("audio", {}).get("available")
        except Exception:
            return
        tasks = service.pending_delivery(include_results=False)
        if not tasks:
            if audio_available:
                self._speak_background_progress()
            return
        inflight = getattr(self, "_background_delivery_inflight", None)
        if inflight is None:
            inflight = self._background_delivery_inflight = set()
        retry_after = getattr(self, "_background_delivery_retry", None)
        if retry_after is None:
            retry_after = self._background_delivery_retry = {}
        task = next((row for row in tasks if row["id"] not in inflight and time.monotonic() >= retry_after.get(row["id"], 0)), None)
        if task is None:
            return
        task = service.get(task["id"])
        if task["source"] == "telegram_text" or ((task.get("result") or {}).get("decision") == "stop" and not (task.get("result") or {}).get("speak")):
            # No unsolicited remote send or accidental local speech. Telegram
            # tasks remain accessible from task status and the local dashboard.
            service.mark_delivered(task["id"], delivery="retained")
            return
        text = str((task.get("result") or {}).get("speak") or ("The task could not finish. " + task.get("error", "") if task["status"] == "failed" else "Open the task card to read it."))
        message = text[:1200]
        generation, cancelled = self._action_token()
        request_id = time.time_ns()
        with turn_scope(task.get("recording_turn_id")):
            self._register_request(request_id, generation)
        turn = getattr(self, '_background_turns', {}).get(task['id'])
        if turn and callable(getattr(self, '_tool_followup_store', None)):
            self._tool_followup_store().bind_request(request_id, turn['source'], turn['scope'])
        intent = Intent("background-result:" + task["id"], BACKGROUND,
                        cancelled=lambda: cancelled.is_set() or not service.delivery_current(task["id"]))
        inflight.add(task["id"])
        try:
            with intent_scope(intent), turn_scope(task.get("recording_turn_id")):
                future = self._submit_guarded(self._audio_executor, self._present_background_result,
                    task, message, request_id, audio_available)
        except RuntimeError:
            inflight.discard(task["id"])
            return

        def delivered(done):
            try:
                handle = done.result()
                if (handle is True or (handle is not None and handle.wait(timeout=0) == 0)) and not cancelled.is_set() and not intent.is_set():
                    service.mark_delivered(task["id"])
                    self.memory.add_assistant_message(message, source="background_result")
                    self._publish_event("background.announced", {"task_id": task["id"]})
                else:
                    retry_after[task["id"]] = time.monotonic() + 30
            except Exception:
                retry_after[task["id"]] = time.monotonic() + 30
            finally:
                inflight.discard(task["id"])
                retained = {row["id"] for row in service.snapshot(include_results=False)["tasks"]}
                for old in tuple(retry_after):
                    if old not in retained:
                        retry_after.pop(old, None)
        future.add_done_callback(delivered)

    def _present_background_result(self, task, message, request_id, audio_available):
        """Present retained artifacts only on the idle output lane, then speak."""
        if not self.background_work.delivery_current(task['id']):
            return None
        display = (task.get('result') or {}).get('display_result')
        state = getattr(self, '_background_turns', {}).get(task['id'], {})
        if display and not state.get('display_presented'):
            call = {'app': 'image-studio', 'tool': 'show', 'arguments': display}
            result = self._execute_app_tool(call, source=task['source'], request_id=request_id)
            if result.get('cancelled'):
                return None
            self._remember_app_display(request_id, result)
            if result.get('ok'):
                state['display_presented'] = True
            if not result.get('ok'):
                message = 'Your image is saved, but I could not display it. ' + str(result.get('text', ''))[:300]
        if not self.background_work.delivery_current(task['id']):
            return None
        return self._speak_text(message, request_id, True) if audio_available else True

    def _queue_background_progress(self):
        """At most one bounded, tool-free model call per slow task. No timers."""
        executor = getattr(self, '_llm_executor', None)
        if executor is None:
            return
        service = self.background_work
        for task_id, state in list(getattr(self, '_background_turns', {}).items()):
            if state['progress_started'] or time.monotonic() < state['progress_due']:
                continue
            state['progress_started'] = True
            try:
                task = service.get(task_id)
                if task['status'] not in {'queued', 'running'} or not service.delivery_current(task_id):
                    continue
                def write_progress(task=task):
                    if service.get(task['id'])['status'] not in {'queued', 'running'}:
                        return
                    action = self.llm.ask(task['text'], provider=task['provider'], model=task['model'],
                        request_timeout_s=3, recent_messages=[], memory_text='', soul_text=self._soul_text,
                        tools_text='', app_catalog_text='', extra_instructions=
                        'This request is already running and has taken over five seconds. Do not answer it or call tools. '
                        'Return only a speak JSON field: one short natural progress sentence in the user language, '
                        'specific to the request. Say you are still working and will return with the result. '
                        'Do not claim completion, invent progress or repeat a generic canned acknowledgement.')
                    if isinstance(action, dict) and not action.get('error') and isinstance(action.get('speak'), str):
                        service.set_progress(task['id'], action['speak'])
                executor.submit(write_progress)
            except (KeyError, BusyError, RuntimeError):
                pass  # Optional feedback must never block the actual task.

    def _speak_background_progress(self):
        service = self.background_work
        for task_id, state in list(getattr(self, '_background_turns', {}).items()):
            if state.get('progress_spoken'):
                continue
            try:
                task = service.get(task_id)
                if (task['source'] not in {'ui_text', 'speech_input', 'voice'}
                        or not task.get('progress') or task['status'] not in {'queued', 'running'}):
                    continue
                state['progress_spoken'] = True
                generation, cancelled = self._action_token()
                request_id = time.time_ns()
                self._register_request(request_id, generation)
                def stale(task_id=task_id):
                    try:
                        return not service.delivery_current(task_id) or service.get(task_id)['status'] not in {'queued', 'running'}
                    except KeyError:
                        return True
                with intent_scope(Intent('task-progress:' + task_id, BACKGROUND, cancelled=stale)):
                    self._submit_guarded(self._audio_executor, self._speak_text, task['progress'], request_id, True)
                return
            except (KeyError, BusyError, RuntimeError):
                continue
