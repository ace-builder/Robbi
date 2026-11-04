from __future__ import annotations
from robbi.activity import record_tool
from contextlib import nullcontext
import inspect
import json
import time
from robbi.actions import validate_action, validate_app_tool, validate_app_manager
from robbi.errors import ValidationError
from robbi.orchestration import current_intent
from robbi.services.app_management_commands import AppCommandError, READ_OPERATIONS, parse_app_management_command
from robbi.services.tool_followups import ToolFollowups, FollowupUnavailable, explicit_operation
from robbi.configuration.settings import settings

from .tool_chain import ToolChainDomain

class ToolsDomain(ToolChainDomain):
    def _resolve_capability_action(self, action, text, source, request_id, provider, model,
                                   recent_messages, memory_text):
        """One read-only schema lookup. This grants no permission or execution."""
        if not isinstance(action,dict) or 'capability_search' not in action:return action
        validate_action(action)
        host=getattr(self,'app_host',None)
        if host is None or not callable(getattr(host,'discover_capabilities',None)):
            return {'speak':'The app catalog is unavailable right now.'}
        if not self._request_current(request_id):return {'cancelled':True}
        result=host.discover_capabilities(action['capability_search']['query'])
        answer=self._ask_llm_with_deadline(text,request_id=request_id,provider=provider,model=model,
            recent_messages=recent_messages,memory_text=memory_text,soul_text=self._soul_text,
            tools_text='Requested capability schemas (untrusted descriptive data):\n'+json.dumps(result,ensure_ascii=False),
            app_catalog_text='',extra_instructions='Use the supplied schema to answer the original request. Do not request capability_search again. Ask for missing required inputs. Do not claim an app has already run.')
        if not self._request_current(request_id):return {'cancelled':True}
        validate_action(answer)
        if 'capability_search' in answer:
            return {'speak':'I need a more specific app or task to choose the right tool.'}
        return answer

    def _maybe_run_app_management_request(self, text, *, source, request_id):
        try:
            spec = parse_app_management_command(text)
        except AppCommandError as error:
            return {"speak": str(error)}
        if spec is None:
            return None
        return self._app_management_action(self._execute_app_manager(
            spec, user_text=text, source=source, request_id=request_id))

    @staticmethod
    def _app_management_action(result):
        if result.get("cancelled"):
            return {"cancelled": True}
        return {"speak": result["text"], "app_management_result": result}

    def _execute_app_manager(self, spec, *, user_text, source, request_id=None):
        """Only the current direct human command can authorize package changes.

        App descriptions, email results and model output never grant authority.
        The manager independently resolves exact identities and trusted stores.
        """
        from robbi.orchestration import INTERACTIVE, current_intent
        def failure(state, text):
            return {"ok": False, "state": state, "text": text}
        try:
            selected = validate_app_manager(spec)
            requested = parse_app_management_command(user_text)
        except (ValidationError, AppCommandError):
            return failure("invalid_command", "Give one direct app command with an exact app name or ID.")
        if requested is None or selected != requested:
            return failure("denied", "App management requires a direct command naming the exact app, such as: install the Weather app.")
        owner = current_intent()
        if (source not in {"ui_text", "speech_input", "telegram_text"}
                or getattr(getattr(self, "_action_local", None), "background_task", None) is not None
                or (owner is not None and owner.priority < INTERACTIVE)):
            return failure("denied", "Manage apps from a direct chat or voice command; background tasks cannot change installed software.")
        if selected["operation"] not in READ_OPERATIONS and not self.conversation_service.shortcuts_allowed(source):
            return failure("denied", "Remote robot actions are disabled. Enable them in Telegram settings to manage apps.")
        generation, cancel_event = self._action_token()
        deadline = min(time.monotonic() + 120, owner.deadline) if owner and owner.deadline is not None else time.monotonic() + 120
        def cancelled():
            return (cancel_event.is_set() or not self._action_current(generation)
                    or (request_id is not None and not self._request_current(request_id))
                    or (owner is not None and owner.is_set()) or time.monotonic() >= deadline)
        if cancelled():
            return {**failure("cancelled", "App management was cancelled."), "cancelled": True}
        manager = getattr(self, "app_manager", None)
        if not callable(getattr(manager, "execute", None)):
            result = failure("unavailable", "App management is unavailable in this runtime. Open the App Library to check installation support.")
        else:
            try:
                result = manager.execute(selected, source=source, user_text=user_text,
                                         cancelled=cancelled, deadline=deadline)
                if (not isinstance(result, dict) or type(result.get("ok")) is not bool
                        or not isinstance(result.get("state"), str) or not 0 < len(result["state"]) <= 80
                        or not isinstance(result.get("text"), str) or not result["text"].strip()):
                    raise ValueError("Missing app management receipt")
                # Only passive result fields cross the presentation boundary.
                result = {key: result[key] for key in ("ok", "state", "text", "data") if key in result}
                result["text"] = result["text"][:4000]
                try:
                    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 32768:
                        raise ValueError("Large result")
                    validate_action({"app_management_result": result})
                except (TypeError, ValueError, UnicodeError, ValidationError, RecursionError):
                    result.pop("data", None)
                    result["details_omitted"] = True
            except Exception:
                result = failure("failed", "App management did not return a confirmed result. Check the App Library before repeating the command.")
        if cancelled():
            result = {**failure("cancelled", "The command was interrupted. Check the App Library before repeating it."), "cancelled": True}
        self._remember_tool_call("app_manager", selected, result, source=source, request_id=request_id)
        return result

    def _tool_followup_store(self):
        with getattr(self, "_action_lock", nullcontext()):
            if not hasattr(self, "_tool_followups"):
                self._tool_followups = ToolFollowups()
            return self._tool_followups

    def _tool_scope_source(self, source, request_id=None):
        return self._tool_followup_store().scope(source, request_id)

    def _tool_source_for_request(self, request_id):
        return self._tool_followup_store().request_source(request_id)

    def _begin_tool_followup_turn(self, source, conversation_id, request_id):
        store = self._tool_followup_store()
        scope = store.begin(source, conversation_id, request_id)
        if not self.conversation_service.shortcuts_allowed(source):
            store.clear(scope)
            return scope
        self._sync_dashboard_tool_context(source, request_id)
        store.calls(scope, self._tool_receipt_allowed)
        return scope

    def _sync_dashboard_tool_context(self, source, request_id=None):
        host = getattr(self, "app_host", None)
        if host is None or not callable(getattr(host, "history", None)) or not callable(getattr(host, "invocation", None)):
            return
        store, scope = self._tool_followup_store(), self._tool_scope_source(source, request_id)
        try:
            history = host.history(source=scope)
            if not isinstance(history, list):
                return
            newest = next((item for item in history if isinstance(item, dict) and item.get("kind", "tool") == "tool"), None)
            if newest and newest.get("state") not in {"done", "failed", "cancelled"}:
                # A newer submitted write may still commit. Never undo an older
                # change merely because the newer invocation is still running.
                store.clear(scope)
                return
            if not newest or any(call.get("undo_ref") == newest.get("id") for call in store.calls(scope)):
                return
            # Do not replace a newer core-tool receipt with an older dashboard job.
            existing = store.calls(scope)
            completed = newest.get("completed_at", newest.get("created_at", 0))
            if not isinstance(completed, (int, float)) or not 0 <= time.time() - completed < store.TTL:
                return
            if completed <= store.receipt_floor(scope):
                return
            if existing and completed <= existing[-1].get("wall_time", 0):
                return
            detail = host.invocation(newest["id"], source=scope)
            if not isinstance(detail, dict) or detail.get("source") != scope:
                return
            result = detail.get("result") or {"ok": False, "cancelled": detail.get("state") == "cancelled", "text": "The last app call did not complete successfully."}
            if detail.get("state") in {"failed", "cancelled"}:
                result = {**result, "ok": False, "cancelled": detail.get("state") == "cancelled"}
            self._remember_tool_call("app_tool", {"app": detail["app"], "tool": detail["tool"], "arguments": detail.get("arguments", {})},
                                     result, source=source, request_id=request_id, undo_ref=detail["id"],
                                     original_time=completed, original_request="dashboard:" + detail["id"])
        except Exception:
            # History is optional on legacy/fake hosts and never authorizes I/O.
            return

    def _tool_receipt_allowed(self, call):
        guard = call.get("guard", {})
        current = getattr(self, "_action_current", None)
        if current and guard.get("generation") is not None and not current(guard["generation"]):
            return False
        if call.get("tool") != "app_tool":
            return True
        host = getattr(self, "app_host", None)
        arguments = call.get("arguments")
        if not isinstance(arguments, dict) or not arguments.get("app"):
            return True  # Keep a failed validation receipt as the newest barrier.
        app = arguments.get("app")
        if host is None:
            return False
        try:
            if callable(getattr(host, "enabled", None)) and not host.enabled(app):
                return False
            if call.get("undo_ref") and callable(getattr(host, "invocation", None)):
                receipt = host.invocation(call["undo_ref"])
                saved_image = app == "image-studio" and arguments.get("tool") in {"generate", "show"}
                validity = "artifact_context_valid" if saved_image and isinstance(receipt, dict) and "artifact_context_valid" in receipt else "context_valid"
                if callable(getattr(host, 'project', None)):
                    tool = next((t for t in host.project(app).manifest['tools'] if t['name'] == arguments.get('tool')), None)
                    if tool and tool.get('read_only') and isinstance(receipt, dict) and 'evidence_context_valid' in receipt:
                        validity = 'evidence_context_valid'
                if isinstance(receipt, dict) and receipt.get(validity) is False:
                    return False
            if callable(getattr(host, "permission_snapshot", None)):
                snapshot = host.permission_snapshot(app)
                granted = {item["id"] for item in snapshot.get("permissions", []) if item.get("status") in {"granted", "bypassed"}}
                if not set(guard.get("permissions", [])) <= granted:
                    return False
        except Exception:
            return False
        return True

    def _remember_tool_call(self, tool, arguments, result, *, source, request_id=None, undo_ref=None,
                            original_time=None, original_request=None):
        if source.startswith(("heartbeat", "proactive", "memory_extractor")):
            return None
        store, scope = self._tool_followup_store(), self._tool_scope_source(source, request_id)
        if request_id is not None and not self._request_current(request_id):
            # A late old result must not erase a newer turn in the same scope.
            store.calls(scope, self._tool_receipt_allowed)
            return None
        if isinstance(result, dict) and result.get("cancelled"):
            store.clear(scope)
            return None
        guard = {}
        if callable(getattr(self, "_action_token", None)):
            guard["generation"] = self._action_token()[0]
        if tool == "app_tool":
            receipt = result.get("invocation", {}) if isinstance(result, dict) else {}
            undo_ref = undo_ref or receipt.get("id")
            host = getattr(self, "app_host", None)
            if isinstance(arguments, dict) and callable(getattr(host, "permission_snapshot", None)):
                try:
                    snapshot = host.permission_snapshot(arguments.get("app"))
                    guard["permissions"] = [item["id"] for item in snapshot.get("permissions", []) if item.get("status") in {"granted", "bypassed"}]
                except Exception:
                    pass
        entry = store.record(scope, tool, arguments, result, request_id=original_request or request_id,
                             undo_ref=undo_ref, guard=guard, wall_time=original_time)
        return entry

    def _tool_followup_prompt(self, source, request_id=None):
        return self._tool_followup_store().prompt(self._tool_scope_source(source, request_id), self._tool_receipt_allowed)

    def _weather_followup_action(self, text, source, request_id=None):
        if not self.conversation_service.shortcuts_allowed(source):
            return None
        from robbi.services.location_context import weather_followup
        store, scope = self._tool_followup_store(), self._tool_scope_source(source, request_id)
        calls = store.calls(scope, self._tool_receipt_allowed)
        messages = store.messages(scope)
        last_reply = next((m['content'] for m in reversed(messages) if m['role'] == 'assistant'), '')
        return weather_followup(text, calls[-1], last_reply) if calls else None

    def _remember_followup_turn(self, text, source, action, request_id=None):
        if not isinstance(action, dict) or action.get("cancelled"):
            return
        collect = getattr(self, "_collect_spoken_text", lambda value: str(value.get("speak", "")))
        self._tool_followup_store().remember_turn(self._tool_scope_source(source, request_id), text,
                                                  collect(action))

    def _remember_shortcut_receipt(self, tool, arguments, result, *, source, request_id):
        calls = self._tool_followup_store().calls(self._tool_scope_source(source, request_id))
        if not any(call["request_id"] == request_id for call in calls):
            self._remember_tool_call(tool, arguments, {"ok": not result.get("error") and not result.get("cancelled"), "data": result},
                                     source=source, request_id=request_id)
        self._remember_followup_turn(str(arguments.get("request", "")), source, result, request_id)

    def _resolve_tool_followup(self, spec, *, user_text, source, request_id=None, explicit=False):
        """Resolve one deliberate repeat/undo without a second model/tool loop."""
        operation = spec.get("operation") if isinstance(spec, dict) else None
        if operation not in {"repeat", "undo"}:
            return {"speak": "Please say which tool action you want to repeat or undo."}
        if not self.conversation_service.shortcuts_allowed(source) or source.startswith(("heartbeat", "proactive", "memory_extractor")):
            return {"speak": "Tool actions are disabled for this source."}
        # Model output alone is not permission to replay a previous write.
        requested = explicit_operation(user_text, operation)
        if not explicit and not requested:
            return {"speak": "Please describe the change you want, or explicitly ask me to repeat or undo an action."}
        store, scope = self._tool_followup_store(), self._tool_scope_source(source, request_id)
        try:
            selected = store.select(scope, spec.get("call_id"), self._tool_receipt_allowed)
        except FollowupUnavailable as error:
            return {"speak": str(error)}
        if selected["tool"] == "app_manager":
            return {"speak": "App management cannot use generic repeat or undo. Give the full app command again; use restore with the exact app name to restore a removed app."}
        if operation == "repeat":
            if not selected["arguments_complete"]:
                return {"speak": "The original arguments are no longer complete. Please give the full request again."}
            if selected["tool"] == "app_tool":
                call = selected["arguments"]
                display_again = "display" in user_text.casefold() or "show" in user_text.casefold()
                if display_again and call.get("app") != "image-studio":
                    return {"speak": "There is no recent generated image in this conversation to display."}
                if call.get("app") == "image-studio" and call.get("tool") == "generate":
                    artifacts = selected.get("result", {}).get("artifacts") or []
                    if not artifacts:
                        return {"speak": "That image is no longer available. Please generate a new one."}
                    call = {"app": "image-studio", "tool": "show", "arguments": {
                        "artifact_id": artifacts[0]["id"], "hold_s": call.get("arguments", {}).get("hold_s", 10)}}
                result = self._execute_app_tool(call, source=source, request_id=request_id)
                if result.get("cancelled"):
                    return {"cancelled": True}
                self._remember_app_display(request_id, result)
                return {"speak": self._app_result_text(result), "app_result": self._app_result_metadata(call, result)}
            if selected["tool"] in {"memory_search", "web_search"}:
                method = getattr(self, "_execute_" + selected["tool"] + "_tool")
                result = method(selected["arguments"])
                self._remember_tool_call(selected["tool"], selected["arguments"], result, source=source, request_id=request_id)
                return {"speak": str(result.get("text") or result.get("summary") or result.get("answer") or json.dumps(result, ensure_ascii=False))[:3500]}
            if selected["tool"] in {"create_note", "add_todo", "add_list_item", "create_research_note", "kids_game"}:
                replayed = self._apply_immediate_runtime_tools({selected["tool"]: selected["arguments"]}, source, request_id)
                # The call was already executed; the normal pipeline must not execute it twice.
                replayed.pop(selected["tool"], None)
                return replayed
            repeat = getattr(self, "_repeat_core_tool", None)
            if callable(repeat):
                return repeat(selected, source=source, request_id=request_id)
            return {"speak": "Please give the full request to repeat this action; it cannot be replayed safely from its receipt."}
        if selected["tool"] == "app_tool":
            host = getattr(self, "app_host", None)
            if not selected.get("undo_ref") or not callable(getattr(host, "undo", None)):
                return {"speak": "This action has no available undo receipt. I have not changed anything."}
            generation, cancel_event = self._action_token()
            cancelled = lambda: cancel_event.is_set() or not self._action_current(generation) or not self._request_current(request_id)
            try:
                result = host.undo(selected["undo_ref"], source=scope, cancellation=cancelled, deadline=time.monotonic() + 30)
            except Exception:
                return {"speak": "That action could not be undone. Check its history; I will not undo a different action."}
        else:
            undo = getattr(self, "_undo_core_tool", None)
            if undo is None or not selected.get("undo_ref"):
                return {"speak": "This tool does not provide undo. I have not changed anything."}
            result = undo(selected, source=scope, request_id=request_id)
        if not self._request_current(request_id) or result.get("cancelled"):
            store.clear(scope)
            return {"cancelled": True}
        if result.get("ok"):
            store.mark_undone(scope, selected["id"])
        return {"speak": self._app_result_text(result)}

    def _remember_interaction(self, user_text: str, source: str, action: dict) -> None:
        store = self._tool_followup_store()
        request_id = getattr(store.local, "request_id", None) if getattr(store.local, "source", None) == source else None
        scope = self._tool_scope_source(source, request_id)
        self.memory.add_user_message(user_text, source=source)
        assistant_text = self._collect_spoken_text(action)
        if assistant_text:
            self.memory.add_assistant_message(assistant_text, source="robot")
        memory_result = self.memory.remember((action or {}).get("remember"), source=source)
        memory_ops_result = self.memory.apply_memory_ops((action or {}).get("memory_ops"), source=source)
        changed = any(memory_result.values()) or any(memory_ops_result.values())
        service = getattr(self, "productivity_service", None)
        with service.change_scope(scope) if callable(getattr(service, "change_scope", None)) else nullcontext() as changes:
            todo_item = self._maybe_extract_todo_from_text(user_text, source=source)
        explicit_result = (
            {"added": [], "updated": [], "deleted": [], "search_results": []}
            if changed
            else self.memory.remember(self._explicit_remember_fact(user_text), source=f"explicit:{source}")
        )
        for tool, arguments, receipt in (("remember", (action or {}).get("remember"), memory_result),
                                          ("memory_ops", (action or {}).get("memory_ops"), memory_ops_result)):
            if any(receipt.get(key) for key in ("added", "updated", "deleted")):
                self._remember_tool_call(tool, arguments, {"ok": True, "data": receipt}, source=source, request_id=request_id)
        if todo_item:
            reference = changes[-1].get("id") if isinstance(changes, list) and changes else None
            self._remember_tool_call("add_todo", {"title": todo_item.get("title", "")},
                                     {"ok": True, "data": todo_item, "limitations": ["Related saved memories are not removed by undo."]},
                                     source=source, request_id=request_id, undo_ref=reference)
        if any(explicit_result.get(key) for key in ("added", "updated", "deleted")):
            self._remember_tool_call("remember", self._explicit_remember_fact(user_text), {"ok": True, "data": explicit_result},
                                     source=source, request_id=request_id)
        if changed:
            self._log.info("Memory updated: remember=%s memory_ops=%s", memory_result, memory_ops_result)
            self._publish_event(
                "memory.updated",
                {"source": source, "remember": memory_result, "memory_ops": memory_ops_result, "explicit": explicit_result},
            )
        elif any(explicit_result.values()):
            self._log.info("Memory updated from explicit remember command: %s", explicit_result)
            self._publish_event("memory.updated", {"source": source, "explicit": explicit_result})
        if todo_item:
            self._log.info("Auto todo extracted: %s", todo_item.get("title"))

    def _explicit_remember_fact(self, user_text: str) -> str:
        text = " ".join(str(user_text or "").split()).strip()
        if not text:
            return ""
        lowered = text.lower()
        prefixes = (
            "remember that ",
            "remember: ",
            "please remember that ",
            "please remember ",
            "robbi remember that ",
            "robbi remember ",
        )
        for prefix in prefixes:
            if lowered.startswith(prefix):
                fact = text[len(prefix):].strip(" .")
                return fact[:500]
        return ""

    def _maybe_run_tool_followup(
        self,
        action: dict,
        user_text: str,
        source: str,
        provider: str,
        model: str,
        recent_messages: list[dict],
        memory_text: str,
        image_bytes: bytes | None,
        request_id: int,
        search_refinements: int = 1,
        deferred_display: dict | None = None,
    ) -> dict:
        if not self._request_current(request_id):
            return {"cancelled": True}
        if not isinstance(action, dict):
            return action
        # Presentation metadata is emitted by this bridge, never by the model.
        action = dict(action)
        action.pop("app_result", None)
        action.pop("app_management_result", None)
        action.pop('tool_chain', None)
        action.pop('display_result', None)
        if "app_manager" in action:
            try:
                validate_action(action)
            except ValidationError:
                return {"speak": "Run one app management command at a time with its exact app name."}
            return self._app_management_action(self._execute_app_manager(
                action["app_manager"], user_text=user_text, source=source, request_id=request_id))
        if "tool_followup" in action:
            return self._resolve_tool_followup(action["tool_followup"], user_text=user_text,
                                               source=source, request_id=request_id)
        from robbi.services.tool_chain import selected_calls
        if 'app_tool' in action:
            try:
                validate_app_tool(action['app_tool'])
            except ValidationError:
                result = self._execute_app_tool(action['app_tool'], source=source, request_id=request_id)
                return {'speak': self._app_result_text(result), 'app_result': self._app_result_metadata(action['app_tool'], result)}
        if not selected_calls(action):
            return action
        return self._run_tool_chain(action, user_text, source, provider, model, recent_messages,
                                    memory_text, image_bytes, request_id, deferred_display)

    def _remember_app_display(self, request_id: int | None, result: dict) -> None:
        data = result.get("data")
        if (request_id is None or result.get("ok") is not True or not isinstance(data, dict)
                or data.get("displayed") is not True):
            return
        # Static app images keep their existing request-scoped guard. Brief
        # expressions can keep their final pose through the acknowledgement
        # without creating an image artifact or suppressing idle faces forever.
        hold = data.get('display_hold_s')
        brief = type(hold) in (int, float) and 0 < hold <= 120
        if not result.get('artifacts') and not brief:
            return
        with self._action_lock:
            if not self._request_current(request_id):
                return
            displays = getattr(self, "_app_display_requests", {})
            expiries = getattr(self, '_app_display_expiries', {})
            displays[request_id] = self._action_generation
            expiries[request_id] = time.monotonic() + (hold if brief else 10)
            while len(displays) > 32:
                expiries.pop(next(iter(displays)), None)
                displays.pop(next(iter(displays)))
            self._app_display_requests = displays
            self._app_display_expiries = expiries
            self._app_active_display_request = request_id
            self._app_display_until = expiries[request_id]

    def _app_preserves_display(self, request_id: int | None = None) -> bool:
        # This private state can only be set by a completed host call. Provider
        # JSON and HTTP action metadata cannot opt out of normal presentation.
        with self._action_lock:
            selected = request_id if request_id is not None else getattr(self, "_app_active_display_request", None)
            return (selected is not None and
                    getattr(self, "_app_display_requests", {}).get(selected) == self._action_generation and
                    time.monotonic() < getattr(self, '_app_display_expiries', {}).get(selected, float('inf')))

    @record_tool("app")
    def _execute_app_tool(self, spec, *, source: str, request_id: int | None = None) -> dict:
        result = self._execute_app_tool_actual(spec, source=source, request_id=request_id)
        self._remember_tool_call("app_tool", spec, result, source=source, request_id=request_id)
        return result

    def _execute_app_tool_actual(self, spec, *, source: str, request_id: int | None = None) -> dict:
        """Narrow bridge: validation/source policy precede app code and I/O."""
        generation, cancel_event = self._action_token()
        chain = getattr(self._action_local, "tool_chain", None)
        owner = current_intent()

        def cancelled():
            return (cancel_event.is_set() or not self._action_current(generation) or not self._request_current(request_id)
                    or (chain is not None and chain.is_set()) or (owner is not None and owner.is_set()))

        if cancelled():
            return {"ok": False, "cancelled": True, "text": "App request cancelled."}
        try:
            selected = validate_app_tool(spec)
        except ValidationError as error:
            return {"ok": False, "error": "invalid_arguments", "text": str(error)}
        if source.startswith(("heartbeat", "proactive", "memory_extractor")):
            return {"ok": False, "error": "source_not_allowed", "text": "App tools require a user request."}
        if source == "telegram_text" and not self.conversation_service.shortcuts_allowed(source):
            return {"ok": False, "error": "source_not_allowed", "text": "Remote app tools are disabled in Telegram settings."}
        host = getattr(self, "app_host", None)
        if host is None:
            return {"ok": False, "error": "unavailable", "text": "The app platform is unavailable. Check app diagnostics."}
        try:
            # Opt in only when declared: simple legacy hosts keep their exact API.
            try:
                options = {"include_invocation": True} if "include_invocation" in inspect.signature(host.call).parameters else {}
            except (TypeError, ValueError):
                options = {}
            call = host.call
            # A directly requested first tool may present its result. Further
            # evidence collection and background reads remain output-free.
            reading_evidence = (getattr(self._action_local, 'background_task', None) is not None or
                                any(step.get('tool') != 'capability_search' for step in getattr(chain, 'steps', [])))
            if chain is not None and callable(getattr(host, 'call_readonly', None)):
                project = host.project(selected['app'])
                tool = next((t for t in project.manifest['tools'] if t['name'] == selected['tool']), None)
                if tool and tool.get('read_only') and (reading_evidence or not getattr(project, 'bundled', False)):
                    call = host.call_readonly
            result = call(
                selected["app"], selected["tool"], selected["arguments"],
                source=self._tool_scope_source(source, request_id), cancellation=cancelled,
                deadline=min(time.monotonic() + 120, chain.call_deadline if chain is not None else float('inf'),
                             owner.deadline if owner is not None and owner.deadline is not None else float('inf')), **options,
            )
            if cancelled():
                return {"ok": False, "cancelled": True, "text": "App request cancelled."}
            if not isinstance(result, dict):
                raise ValidationError("App returned an invalid result")
            return result
        except Exception as error:
            if cancelled():
                return {"ok": False, "cancelled": True, "text": "App request cancelled."}
            # A custom host may include an endpoint credential in its exception.
            # Host diagnostics own redaction; conversation events carry a type.
            self._publish_event("tool.failed", {"tool": "app_tool", "app": selected["app"], "error": type(error).__name__})
            return {"ok": False, "error": "app_failed", "text": "The app could not complete this request. Check its configuration and connection."}

    @staticmethod
    def _app_result_text(result: dict) -> str:
        return str(result.get("text") or ("The app completed." if result.get("ok") else "The app could not complete this request."))[:4000]

    @classmethod
    def _app_result_metadata(cls, spec, result: dict) -> dict:
        selected = spec if isinstance(spec, dict) else {}
        metadata = {"app": str(selected.get("app", ""))[:64], "tool": str(selected.get("tool", ""))[:64], "result": result}
        try:
            encoded = json.dumps(metadata, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > 65536:
                raise ValidationError("App result exceeds conversation limit")
            validate_action({"app_result": metadata})
            return metadata
        except (ValidationError, ValueError, TypeError, UnicodeError):
            # AppHost enforces its own result bounds. Keep this integration safe
            # even with a custom host returning an oversized/non-JSON object.
            metadata["result"] = {"ok": bool(result.get("ok")), "text": cls._app_result_text(result), "details_omitted": True}
            return metadata

    @staticmethod
    def _strip_followup_tools(action: dict, *, block_apps: bool = False, preserve_display: bool = False, depth: int = 0) -> dict:
        """The follow-up cannot chain app calls through legacy app/workflows."""
        if depth > 8:
            return {}
        blocked = {"app_tool", "tool_followup", "app_manager", "app_management_result", "web_search", "memory_search", "app_result",
                   "create_note", "add_todo", "add_list_item", "create_research_note", "kids_game",
                   "remember", "memory_ops", "drive", "slam"}
        if block_apps:
            blocked.update({"app", "take_picture", "vision_skill"})
        if preserve_display:
            blocked.update({"face", "speaking_face_pattern", "animation", "filming_sequence", "video", "face_animation", "gesture"})
        result = {key: value for key, value in action.items() if key not in blocked}
        if isinstance(result.get("sequence"), list):
            result["sequence"] = [ToolsDomain._strip_followup_tools(step, block_apps=block_apps,
                                  preserve_display=preserve_display, depth=depth + 1)
                                  for step in result["sequence"][:64] if isinstance(step, dict)]
        return result

    def _call_immediate_tool(self, tool, arguments, callback, source, request_id):
        if not self._request_current(request_id):
            return None
        service = getattr(self, "productivity_service", None)
        scope = self._tool_scope_source(source, request_id)
        journal = getattr(service, "last_change", lambda source: None)
        before = journal(scope)
        with service.change_scope(scope) if callable(getattr(service, "change_scope", None)) else nullcontext() as changes:
            value = callback()
        after = (changes[-1] if changes else None) if isinstance(changes, list) else journal(scope)
        undo_ref = after.get("id") if isinstance(after, dict) and after.get("id") != (before or {}).get("id") else None
        ok = isinstance(value, dict) and value.get("face") != "error" and value.get("ok", True) is not False
        result = {"ok": ok, "data": value, "text": "Tool completed." if ok else "The tool did not complete successfully."}
        self._remember_tool_call(tool, arguments, result, source=source, request_id=request_id, undo_ref=undo_ref)
        return value

    def _apply_immediate_runtime_tools(self, action: dict, source: str, request_id: int | None = None) -> dict:
        if not isinstance(action, dict):
            return action
        result = dict(action)
        created: list[str] = []
        if "create_note" in result:
            note = self._call_immediate_tool("create_note", result["create_note"], lambda: self._tool_create_note(result["create_note"], source=source), source, request_id)
            if note:
                created.append(f"note {note.get('id')}")
        if "add_todo" in result:
            item = self._call_immediate_tool("add_todo", result["add_todo"], lambda: self._tool_add_list_item("todo", result["add_todo"], source=source), source, request_id)
            if item:
                created.append(f"todo {item.get('id')}")
        if "add_list_item" in result:
            item = self._call_immediate_tool("add_list_item", result["add_list_item"], lambda: self._tool_add_list_item("", result["add_list_item"], source=source), source, request_id)
            if item:
                created.append(f"{item.get('list_name')} item {item.get('id')}")
        if "create_research_note" in result:
            note = self._call_immediate_tool("create_research_note", result["create_research_note"], lambda: self._tool_create_research_note(result["create_research_note"], source=source), source, request_id)
            if note:
                created.append(f"research note {note.get('id')}")
        if "kids_game" in result:
            kids_action = self._call_immediate_tool("kids_game", result["kids_game"], lambda: self._tool_kids_game(result["kids_game"]), source, request_id)
            if kids_action:
                result.update({key: value for key, value in kids_action.items() if key in {"speak", "face", "sound", "sequence"}})
        if created:
            self._publish_event("tool.called", {"request_id": request_id, "tools": created, "source": source})
            if not self._has_presentational_content(result):
                result["speak"] = f"Saved {', '.join(created)}."
        return result

    @record_tool("create_note")
    def _tool_create_note(self, spec, source: str) -> dict | None:
        if isinstance(spec, str):
            payload = {"raw_text": spec, "summary": spec[:500], "source": source}
        elif isinstance(spec, dict):
            payload = dict(spec)
            payload.setdefault("source", source)
        else:
            return None
        try:
            return self.create_note(payload)
        except Exception as exc:
            self._publish_event("tool.failed", {"tool": "create_note", "error": str(exc)})
            return None

    @record_tool("add_list_item")
    def _tool_add_list_item(self, list_name: str, spec, source: str) -> dict | None:
        if isinstance(spec, str):
            payload = {"list_name": list_name or "todo", "title": spec, "source": source}
        elif isinstance(spec, dict):
            payload = dict(spec)
            payload.setdefault("list_name", list_name or payload.get("list") or "todo")
            payload.setdefault("source", source)
        else:
            return None
        try:
            return self.add_list_item(payload)
        except Exception as exc:
            self._publish_event("tool.failed", {"tool": "add_list_item", "error": str(exc)})
            return None

    @record_tool("create_research_note")
    def _tool_create_research_note(self, spec, source: str) -> dict | None:
        if isinstance(spec, str):
            payload = {"query": spec, "title": spec[:80], "source": source}
        elif isinstance(spec, dict):
            payload = dict(spec)
            payload.setdefault("source", source)
        else:
            return None
        try:
            return self.create_research_note(payload)
        except Exception as exc:
            self._publish_event("tool.failed", {"tool": "create_research_note", "error": str(exc)})
            return None

    @record_tool("kids_game")
    def _tool_kids_game(self, spec) -> dict | None:
        try:
            if isinstance(spec, str):
                result = self.kids_start({"mode": spec})
            elif isinstance(spec, dict) and spec.get("action") == "stop":
                result = self.kids_stop()
            elif isinstance(spec, dict) and spec.get("answer"):
                result = self.kids_input({"text": spec.get("answer")})
            elif isinstance(spec, dict):
                result = self.kids_start(spec)
            else:
                return None
            return {"speak": result.get("speak") or "Kids mode updated.", "face": result.get("face") or "happy"}
        except Exception as exc:
            self._publish_event("tool.failed", {"tool": "kids_game", "error": str(exc)})
            return {"speak": "Kids mode failed. Tiny clown car, many parts.", "face": "error"}

    @record_tool("memory_search")
    def _execute_memory_search_tool(self, spec) -> dict:
        if isinstance(spec, str):
            query = spec.strip()
            limit = settings.memory_search_limit
        elif isinstance(spec, dict):
            query = str(spec.get("query") or spec.get("text") or "").strip()
            limit = int(spec.get("limit") or settings.memory_search_limit)
        else:
            query = ""
            limit = settings.memory_search_limit
        result = self.memory.search(query, limit=max(1, min(20, limit))) if query else []
        self._publish_event("tool.called", {"tool": "memory_search", "query": query, "count": len(result)})
        return {"query": query, "results": result}

    @record_tool("web_search")
    def _execute_web_search_tool(self, spec) -> dict:
        if isinstance(spec, str):
            query = spec.strip()
            max_results = settings.web_search_max_results
            search_depth = settings.web_search_search_depth
        elif isinstance(spec, dict):
            query = str(spec.get("query") or spec.get("text") or "").strip()
            max_results = spec.get("max_results", settings.web_search_max_results)
            search_depth = str(spec.get("search_depth") or settings.web_search_search_depth).strip()
        else:
            query = ""
            max_results = settings.web_search_max_results
            search_depth = settings.web_search_search_depth
        context = getattr(self._action_local, "tool_chain", None) or getattr(self._action_local, "background_task", None)
        extra = {key: spec[key] for key in ("topic", "time_range") if isinstance(spec, dict) and key in spec}
        if context is not None:
            extra.update(cancellation=context.is_set, deadline=getattr(context, "call_deadline", context.deadline))
        result = self.web_search.search(query, max_results=max_results, search_depth=search_depth, **extra)
        event_type = "tool.called" if result.get("ok") else "tool.failed"
        self._publish_event(event_type, {"tool": "web_search", "query": query, "ok": result.get("ok"), "error": result.get("error", "")})
        return result

    def _queue_memory_extraction(self, user_text: str, source: str, action: dict) -> None:
        if not settings.memory_extraction_enabled:
            return
        if not user_text or source.startswith("memory_extractor"):
            return
        try:
            self._submit_guarded(self._memory_extractor_executor, self._run_memory_extraction, user_text, source, action)
        except RuntimeError as error:
            # Background enrichment is optional. Admission failure must not turn
            # an already delivered answer into a failed user request.
            self._log.info("Skipping memory extraction: %s", error)
            self._publish_event("memory.extraction_skipped", {"source": source, "reason": str(error)})

    def _run_memory_extraction(self, user_text: str, source: str, action: dict) -> None:
        generation, cancelled = self._action_token()
        try:
            if cancelled.is_set() or not self._action_current(generation):
                return
            assistant_text = self._collect_spoken_text(action)
            prompt = (
                "Extract durable memory updates from this interaction. "
                "Return only JSON. If nothing durable, return {\"heartbeat\":\"ok\"}. "
                "Use memory_ops.upsert for stable preferences, project facts, recurring tasks, names, tasks/todos, and robot-relevant context. "
                "Include confidence 0.0-1.0 and importance 1-5. Do not store throwaway chatter or one-off jokes.\n"
                f"User: {user_text}\nRobbi: {assistant_text}"
            )
            memory_context = self.memory.prepare_prompt_context(user_text, recent_messages=self.memory.recent_messages(), max_chars=2500)
            result = self._ask_llm_with_deadline(
                prompt,
                background=True,
                provider=settings.ui_text_provider,
                model=settings.ui_text_model,
                recent_messages=[],
                memory_text=memory_context["prompt_text"],
                soul_text="Extract only durable facts, preferences, project context, tasks, recurring tasks, names, and robot-relevant context.",
                tools_text="Use only memory_ops.upsert/add/update/delete or heartbeat. Extracted memories enter review, so be useful but selective.",
                app_catalog_text="",
                extra_instructions="This is a silent background memory extraction pass. Do not include speak unless required.",
            )
            if cancelled.is_set() or not self._action_current(generation):
                return
            memory_result = self.memory.remember((result or {}).get("remember"), source=f"memory_extractor:{source}")
            memory_ops_result = self.memory.apply_memory_ops((result or {}).get("memory_ops"), source=f"memory_extractor:{source}")
            changed = any(memory_result.values()) or any(memory_ops_result.values())
            self._publish_event(
                "memory.created" if changed else "memory.extraction_skipped",
                {"source": source, "changed": changed, "remember": memory_result, "memory_ops": memory_ops_result},
            )
        except Exception as exc:
            self._log.warning("Background memory extraction failed: %s", exc)
            self._publish_event("tool.failed", {"tool": "memory_extraction", "error": str(exc)})
