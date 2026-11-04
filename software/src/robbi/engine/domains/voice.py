from __future__ import annotations
import random
import re
import time
from contextvars import ContextVar
from pathlib import Path
from robbi.errors import BusyError
from robbi.activity import record_speech_input
from robbi.orchestration import current_intent, intent_scope, BACKGROUND
from ..activity import activity
from robbi.configuration.settings import ASSETS_DIR, settings
from ..logging_utils import trace_event

_FOLLOWUP_ALLOWED = ContextVar("robbi_spoken_followup_allowed", default=True)

class VoiceDomain:
    def _active_game_session(self):
        getter = getattr(getattr(self, 'app_host', None), 'game_session', None)
        session = getter() if callable(getter) else None
        return session if isinstance(session, dict) else None

    def _game_display_active(self):
        return bool(self._active_game_session() and time.monotonic() < getattr(self, '_app_display_until', 0))

    def _game_listening_cue(self, active):
        callback = getattr(self.media, 'set_app_listening', None)
        if callable(callback):
            callback(bool(active))

    def _game_turn_completed(self, session, source):
        self.microphone.cancel_followup()
        if session is None:
            self._game_listening_cue(False)
        elif not source.startswith('speech_input'):
            # Dashboard games also own a bounded turn window. Voice games wait
            # for their spoken acknowledgement to finish before opening it.
            self._maybe_arm_followup_conversation()

    def wake_reference_status(self) -> dict:
        return self.microphone.wake_dataset_status()

    def _can_listen_for_voice(self) -> bool:
        if getattr(self, '_conversation_lab_active', False):
            return False
        if getattr(self, '_filming_mode', False):
            return self.microphone.wake_wait_active() and not self.media.is_audio_playing()
        return (not self._voice_processing) and (not self.media.is_audio_playing())

    def _transcribe_microphone_audio(self, purpose: str, wav_bytes: bytes) -> str:
        started_at = time.perf_counter()
        transcript = self.llm.transcribe(
            wav_bytes,
            provider=settings.stt_provider,
            model=settings.stt_model,
            language=settings.stt_language,
        )
        # Unaddressed background speech must not enter logs, the feed or memory.
        if purpose == "followup":
            return transcript
        self._log.info(
            "Voice transcription: purpose=%s chars=%s duration_ms=%s text=%r",
            purpose,
            len(transcript),
            round((time.perf_counter() - started_at) * 1000, 1),
            transcript,
        )
        trace_event(
            "stt_result",
            purpose=purpose,
            provider=settings.stt_provider,
            model=settings.stt_model,
            text=transcript,
        )
        self._publish_event("voice.transcribed", {"purpose": purpose, "text": transcript, "chars": len(transcript)})
        return transcript

    def _accept_spoken_followup(self, transcript: str, reply: str) -> bool:
        from robbi.services.spoken_followup import accepted, candidate_reason
        if not self._can_open_spoken_followup():
            return False
        game = self._active_game_session()
        if game:
            from robbi.mini_games import clean, game_turn_request
            # A bounded game answer is routed locally, never through a model
            # guessing the meaning/type of a bare number. Unrelated speech is
            # not admitted as a general command by this special window.
            return clean(transcript) in {'stop', 'cancel', 'stopp', 'abbrechen'} or game_turn_request(game['app'], transcript) is not None
        if candidate_reason(transcript, reply):
            return False
        verdict = self.llm.classify_followup(
            transcript, reply, provider=settings.speech_input_provider,
            model=settings.speech_input_model)
        return accepted(verdict) and self._can_open_spoken_followup()

    @activity("listening-cue", BACKGROUND)
    def _handle_wake_phrase_detected(self, transcript: str) -> None:
        if getattr(self, '_filming_mode', False):
            return
        _, cancelled = self._action_token()
        if cancelled.is_set():
            return
        self._log.info("Wake phrase detected: %s", transcript)
        self._touch_activity()
        with self._conversation_lock:
            self._conversation_turns = 0
            self._voice_empty_retries = 0
        self._publish_event("voice.wake_detected", {"transcript": transcript})
        if self._game_display_active():
            self._game_listening_cue(True)
            return
        ready_face = self.assets.get("faces", settings.voice_ready_face, extensions=(".png",))
        ready_sound = self._pick_listening_sound()
        if ready_sound is None:
            ready_sound = self.assets.get("sounds", settings.voice_ready_sound, extensions=(".wav",))
        if cancelled.is_set():
            return
        if ready_face is not None:
            self.media.show_face(ready_face)
            # Display I/O cannot be preempted; avoid further effects/state
            # changes if Stop arrived while that optional driver was working.
            if cancelled.is_set():
                return
            self.behavior.set_current_face(ready_face.name)
            self._set_presentation(face=ready_face.name, thinking=True, last_command=f"wake:{transcript}")
        if ready_sound is not None:
            ready_sound_duration = self.media.duration_s(ready_sound) or 0.0
            if cancelled.is_set():
                return
            if ready_sound_duration > 0:
                self.microphone.extend_post_wake_block(ready_sound_duration + 0.2)
            self.media.play_sound(ready_sound, wait=False, cancel_event=cancelled)
            if not cancelled.is_set():
                self._set_presentation(last_sound=ready_sound.name)

    @activity("listening-finished", BACKGROUND)
    def _handle_voice_listening_finished(self) -> None:
        _, cancelled = self._action_token()
        if cancelled.is_set():
            return
        if self._game_display_active():
            self._game_listening_cue(False)
            return
        done_face = self.assets.get("faces", "happy", extensions=(".png",))
        if done_face is None:
            done_face = self.assets.get("faces", "neutral", extensions=(".png",))
        if done_face is not None and not cancelled.is_set():
            self.media.show_face(done_face)
            if cancelled.is_set():
                return
            self.behavior.set_current_face(done_face.name)
            self._set_presentation(face=done_face.name, thinking=False)

    def _handle_followup_listening_state(self, active: bool) -> None:
        """Schedule the visual cue without blocking microphone processing."""
        revision = getattr(self, "_followup_cue_revision", 0) + 1
        self._followup_cue_revision = revision
        self._publish_event("voice.followup_started" if active else "voice.followup_ended", {})
        try:
            self._submit_guarded(self._overlay_executor, self._render_followup_listening_cue, active, revision)
        except (BusyError, RuntimeError):
            self._log.debug('Listening indicator skipped while presentation queue is busy')

    @activity("followup-listening-cue", BACKGROUND)
    def _render_followup_listening_cue(self, active: bool, revision: int) -> None:
        if (revision != getattr(self, "_followup_cue_revision", 0)
                or active != self.microphone.followup_listening()):
            return
        if self._game_display_active():
            self._game_listening_cue(active)
            return
        if not active:
            self._game_listening_cue(False)
        previous = getattr(self, "_followup_cue_face", None)
        protected = (getattr(self, "_filming_mode", False) or self.focus_timer.is_active()
                     or self.media.is_audio_playing() or self.media.is_video_playing()
                     or time.monotonic() < getattr(self, "_app_display_until", 0))
        if protected:
            return
        if not active and (previous is None or self._presentation_state.get("face") != previous):
            return  # A newer presentation owns the display now.
        _, cancelled = self._action_token()
        face = self.assets.get("faces", "thinking" if active else "happy", extensions=(".png",))
        if face is None:
            face = self.assets.get("faces", settings.voice_ready_face if active else "neutral", extensions=(".png",))
        if face is None or cancelled.is_set():
            return
        if self.media.show_face(face, cancel_event=cancelled) is False or cancelled.is_set():
            return
        self._followup_cue_face = face.name if active else None
        self.behavior.set_current_face(face.name)
        self._set_presentation(face=face.name, thinking=active)

    def _pick_listening_sound(self) -> Path | None:
        listening_dir = ASSETS_DIR / "sounds" / "listening"
        if not listening_dir.exists():
            return None
        candidates = [
            path
            for path in sorted(listening_dir.glob("*.wav"))
            if path.is_file() and not path.name.startswith("._")
        ]
        if not candidates:
            return None
        return random.choice(candidates)

    def _pick_thinking_sound(self) -> Path | None:
        thinking_dir = ASSETS_DIR / "sounds" / "thinking"
        if thinking_dir.exists():
            candidates = [
                path
                for path in sorted(thinking_dir.glob("*.wav"))
                if path.is_file() and not path.name.startswith("._")
            ]
            if candidates:
                return random.choice(candidates)
        return self.assets.get("sounds", "thinking", extensions=(".wav",))

    def _play_delayed_thinking_sound(self, sound_path: Path, request_id: int | None, delay_s: float = 0.6) -> None:
        generation, cancelled = self._action_token()
        parent=current_intent()
        def runner() -> None:
            if cancelled.wait(max(0.0, float(delay_s))) or not self._request_current(request_id):
                return
            presentation = self.presentation_state()
            if not presentation.get("thinking"):
                return
            if request_id is not None:
                with self._state_lock:
                    if self._last_pipeline_request_id != request_id:
                        return
            # Cosmetic audio must not leave the foreground conversation
            # holding only the speaker while it tries to take over a display.
            from robbi.orchestration import Intent,BACKGROUND,intent_scope
            thinking=Intent('thinking-sound',BACKGROUND,cancelled=lambda:
                cancelled.is_set() or (parent is not None and parent.is_set())
                or not self._request_current(request_id) or not self.presentation_state().get('thinking'),
                deadline=time.monotonic()+30)
            try:
                with intent_scope(thinking):
                    self.media.play_sound(sound_path, wait=False, cancel_event=thinking)
            except (BusyError,RuntimeError):
                # A response or another owner won the speaker. Skip the cue.
                return
            if not cancelled.is_set():
                self._set_presentation(last_sound=sound_path.name)

        try:
            self._submit_guarded(self._effect_executor, runner)
        except (BusyError, RuntimeError) as exc:
            # Cosmetic feedback must not abort a conversation before its
            # provider request when an effects lane/shared budget is occupied.
            reason = "busy" if isinstance(exc, BusyError) else "stopping"
            self._log.info("Thinking sound skipped: %s", reason)
            self._publish_event("presentation.optional_skipped", {
                "kind": "thinking_sound", "reason": reason, "request_id": request_id,
            })

    def _stop_transient_thinking_audio(self) -> None:
        update=getattr(self,'_set_presentation',None)
        if callable(update):update(thinking=False)
        owner = current_intent()
        if owner is not None and getattr(self, 'orchestrator', None) is not None:
            self.media.stop_audio(owner=owner)
        elif getattr(self, 'orchestrator', None) is None:
            # Compatibility for isolated legacy controllers, with no other owners.
            self.media.stop_audio()

    def _can_run_tts_keepwarm(self) -> bool:
        if not settings.startup_warmup_enabled or not settings.voice_enabled:
            return False
        if settings.tts_provider != "local":
            return False
        if self._voice_processing:
            return False
        if self.media.is_audio_playing():
            return False
        if time.time() - self._last_activity_ts < 15.0:
            return False
        try:
            voice_state = self.microphone.voice_state()
        except Exception:
            voice_state = "idle"
        return voice_state == "idle"

    def _warm_tts_async(self, reason: str = "manual", min_interval_s: float = 10.0) -> None:
        if settings.tts_provider != "local":
            return
        now = time.time()
        with self._tts_warm_lock:
            if self._tts_warm_inflight:
                return
            if now - self._last_tts_warm_at < min_interval_s:
                return
            self._tts_warm_inflight = True
            self._last_tts_warm_at = now

        def runner() -> None:
            started_at = time.perf_counter()
            ok = False
            result: dict = {}
            try:
                result = self.llm.warm_tts("hm")
                ok = bool(result.get("ok"))
                self._log.info(
                    "Local TTS warm: reason=%s ok=%s provider=%s model=%s duration_ms=%s",
                    reason,
                    ok,
                    result.get("provider"),
                    result.get("model"),
                    round((time.perf_counter() - started_at) * 1000, 1),
                )
            except Exception as exc:
                self._log.warning("Local TTS warm failed: reason=%s error=%s", reason, exc)
            finally:
                trace_event(
                    "tts_warm",
                    reason=reason,
                    ok=ok,
                    provider=result.get("provider") if result else settings.tts_provider,
                    model=result.get("model") if result else settings.tts_model,
                    duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
                )
                with self._tts_warm_lock:
                    self._tts_warm_inflight = False

        try:
            self._maintenance_executor.submit(runner)
        except Exception:
            with self._tts_warm_lock:
                self._tts_warm_inflight = False

    @record_speech_input
    def _handle_voice_command_transcript(self, transcript: str, wav_bytes: bytes) -> None:
        clean_transcript = (transcript or "").strip()
        if " ".join(clean_transcript.casefold().split()).strip(" .,!?:;") in {"stop", "cancel", "stopp", "abbrechen"}:
            # Stop bypasses the voice/provider queue. Each independent stop is
            # attempted even if another optional component is unavailable.
            self._handle_spoken_stop()
            return
        if getattr(self, '_filming_mode', False):
            return
        if not clean_transcript:
            with self._conversation_lock:
                retry = (settings.conversation_followup_enabled
                         and getattr(self, '_voice_empty_retries', 0) < 1)
                self._voice_empty_retries = 1
                if not retry:
                    self._conversation_turns = 0
            if not retry:
                # Invalidate pending playback callbacks too: a late TTS reply
                # must not reopen this exhausted listening window.
                self.microphone.cancel_followup()
                self._set_presentation(thinking=False, last_command="voice:(finished)")
                self._publish_event("voice.followup_ended", {"reason": "not_understood"})
                return
            retry_text = "I didn't catch that. Try again."
            retry_face = self.assets.get("faces", "happy", extensions=(".png",))
            if retry_face is not None and not self._game_display_active():
                self.media.show_face(retry_face)
                self.behavior.set_current_face(retry_face.name)
                self._set_presentation(face=retry_face.name, thinking=False)
            self._set_presentation(last_command="voice:(empty)", last_speak=retry_text)
            self._speak_text(retry_text, wait=False)
            return

        with self._conversation_lock:
            self._voice_empty_retries = 0

        def runner() -> None:
            self._voice_processing = True
            try:
                self._log.info("Voice command transcript: %s", clean_transcript)
                self._touch_activity()
                self._set_presentation(last_command=f"voice:{clean_transcript}")
                if self._is_conversation_end_phrase(clean_transcript):
                    with self._conversation_lock:
                        self._conversation_turns = 0
                    self.microphone.cancel_followup()
                    marker = _FOLLOWUP_ALLOWED.set(False)
                    try:
                        self.execute_action({"speak": "Okay.", "face": "happy"})
                    finally:
                        _FOLLOWUP_ALLOWED.reset(marker)
                    return
                with self._conversation_lock:
                    self._conversation_turns += 1
                self.think_and_respond(clean_transcript, source="speech_input")
            except Exception as error:
                self._log.exception('Voice request failed')
                self._publish_event('voice.request_failed',{'error_type':type(error).__name__})
                raise
            finally:
                self._voice_processing = False
                if self._active_game_session():
                    # Also supports silent/muted game turns. The microphone
                    # still waits for actual speaker output to become quiet.
                    self._maybe_arm_followup_conversation()

        self._submit_guarded(self._voice_executor, runner)

    def _handle_spoken_stop(self):
        for name, callback in (("actions", self.cancel_actions),
                               ("motion", lambda: self.motion.stop()),
                               ("mapping", lambda: self.slam.stop()),
                               ("audio", lambda: self.media.stop_audio()),
                               ("attention", lambda: self.microphone.cancel_followup())):
            try:
                callback()
            except Exception:
                self._log.exception("Spoken Stop could not stop %s", name)
        with self._conversation_lock:
            self._conversation_turns = 0
        # The old input token was cancelled above. Only the acknowledgement
        # belongs to the new epoch, and it must not reopen attention.
        previous = getattr(self._action_local, "token", None)
        marker = _FOLLOWUP_ALLOWED.set(False)
        self._action_local.token = None
        try:
            with intent_scope(None):
                self.execute_action({"speak": "Stopped."})
        except Exception:
            self._log.warning("Spoken Stop acknowledgement could not be played", exc_info=True)
        finally:
            self._action_local.token = previous
            _FOLLOWUP_ALLOWED.reset(marker)

    def _conversation_end_phrases(self) -> list[str]:
        raw = settings.conversation_end_phrases
        if isinstance(raw, str):
            parts = re.split(r"[,;|]", raw)
        elif isinstance(raw, list):
            parts = raw
        else:
            parts = []
        return [" ".join(str(part or "").lower().split()) for part in parts if str(part or "").strip()]

    def _is_conversation_end_phrase(self, transcript: str) -> bool:
        text = " ".join(str(transcript or "").lower().split()).strip(" .,!?:;")
        if not text:
            return False
        return any(text == phrase or text.endswith(f" {phrase}") for phrase in self._conversation_end_phrases())

    def _prepare_spoken_followup(self):
        """Capture attention identity BEFORE provider work, without starting a timer."""
        if not _FOLLOWUP_ALLOWED.get() or not (settings.conversation_followup_enabled or self._active_game_session()):
            return None
        try:
            microphone = self.microphone
            token = microphone.followup_token()
        except (AttributeError, RuntimeError):
            return None
        generation, cancelled = self._action_token()
        return {"microphone": microphone, "token": token, "generation": generation,
                "cancelled": cancelled, "owner": current_intent()}

    def _track_spoken_playback(self, handle, *, context=None):
        # Sound effects and video never call this hook. Notification registration
        # remains constant-time even for delayed synthesis or long playback.
        if handle is None or context is None or not hasattr(handle, "add_done_callback"):
            return
        def completed(playback):
            if playback.returncode == 0:
                self._maybe_arm_followup_conversation(context=context)
        handle.add_done_callback(completed)

    def _can_open_spoken_followup(self):
        return ((settings.conversation_followup_enabled or self._active_game_session()) and not self._filming_mode
                and not self.focus_timer.is_active() and not self.media.is_video_playing()
                and self._can_listen_for_voice())

    def _maybe_arm_followup_conversation(self, *, context=None) -> bool:
        """Request one listening window; the microphone opens it when safe."""
        game = self._active_game_session()
        if not settings.conversation_followup_enabled and not game:
            return False
        context = context or self._prepare_spoken_followup()
        if context is None or self.microphone is not context["microphone"]:
            return False

        def cancelled():
            owner = context["owner"]
            return (context["cancelled"].is_set() or not self._action_current(context["generation"])
                    or (owner is not None and owner.is_set())
                    or (game is not None and self._active_game_session() != game))

        if cancelled():
            return False
        with self._conversation_lock:
            turns = self._conversation_turns
            limit = max(0, int(settings.conversation_max_turns))
            if limit and turns >= limit and not game:
                self._conversation_turns = 0
                self.microphone.cancel_followup()
                return False
        timeout = 60. if game else settings.conversation_followup_timeout_s
        if game and getattr(self, '_app_display_until', 0) > time.monotonic():
            # End the indicator before a shorter game-screen hold expires.
            timeout = min(timeout, max(1., self._app_display_until-time.monotonic()-3.))
        queued = self.microphone.request_followup(
            timeout, token=context["token"],
            cancelled=cancelled, ready=self._can_open_spoken_followup,
            reply=context.get("reply", ""), output_active=self.media.is_audio_playing,
            **({'game': True} if game else {}))
        if queued:
            self._publish_event("voice.followup_requested", {
                "timeout_s": timeout, "turns": turns, "game": game['app'] if game else None})
        return queued
