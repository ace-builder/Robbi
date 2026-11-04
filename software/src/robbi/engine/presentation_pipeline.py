from __future__ import annotations

import time
from concurrent.futures import Future

from robbi.configuration.settings import settings
from .logging_utils import trace_event
from .presentation_waits import wait_for_playback
from robbi.orchestration import intent_scope
from contextlib import nullcontext


class PresentationPipelineMixin:
    def _prefetch_sequence_tts(self, sequence: list) -> dict[int, Future]:
        futures: dict[int, Future] = {}
        for index, step in enumerate(sequence):
            if not isinstance(step, dict):
                continue
            speak = str(step.get("speak") or "").strip()
            if not speak:
                continue
            futures[index] = self._tts_prefetch_executor.submit(self._prepare_tts_audio, speak)
        return futures

    def _speak_text(self, text: str, request_id: int | None = None, wait: bool = False):
        prepared = self._prepare_tts_audio(text)
        return self._play_prepared_tts(prepared, request_id=request_id, wait=wait)

    def _speak_text_with_overlay(
        self,
        text: str,
        face_pattern=None,
        request_id: int | None = None,
        wait: bool = False,
    ):
        prepared = self._prepare_tts_audio(text)
        return self._play_prepared_tts_with_overlay(prepared, face_pattern, request_id=request_id, wait=wait)

    def _prepare_tts_audio(self, text: str) -> dict:
        generation, cancelled = self._action_token()
        followup = getattr(self, "_prepare_spoken_followup", lambda: None)()
        if followup is not None:
            followup = dict(followup, reply=text[-1200:])
        started_at = time.perf_counter()
        provider = settings.tts_provider
        model = settings.elevenlabs_model if provider == "elevenlabs" else settings.tts_model
        trace_event(
            "tts_request",
            provider=provider,
            model=model,
            voice=settings.tts_voice,
            text_chars=len(text),
        )
        try:
            wav = self.llm.synthesize(text)
            synth_ms = round((time.perf_counter() - started_at) * 1000, 1)
            if not wav:
                self._log.warning("TTS returned no audio bytes")
            trace_event(
                "tts_result",
                provider=provider,
                model=model,
                voice=settings.tts_voice,
                ok=bool(wav),
                synthesis_ms=synth_ms,
                audio_bytes=len(wav or b""),
            )
            return {
                "generation": generation,
                "_spoken_followup": followup,
                "text": text,
                "wav": wav,
                "tts_synthesis_ms": synth_ms,
                "tts_audio_bytes": len(wav or b""),
            }
        except Exception as exc:
            synth_ms = round((time.perf_counter() - started_at) * 1000, 1)
            self._log.warning("TTS playback failed: %s", exc)
            trace_event(
                "tts_result",
                provider=provider,
                model=model,
                voice=settings.tts_voice,
                ok=False,
                synthesis_ms=synth_ms,
                audio_bytes=0,
                error=str(exc),
            )
            return {
                "generation": generation,
                "text": text,
                "wav": b"",
                "tts_error": str(exc),
                "tts_synthesis_ms": synth_ms,
                "tts_audio_bytes": 0,
            }

    def _play_prepared_tts(self, prepared: dict, request_id: int | None = None, wait: bool = False):
        if not prepared or not self._request_current(request_id):
            return None
        if prepared.get('generation') is not None and not self._action_current(prepared['generation']):
            return None
        wav = prepared.get("wav") or b""
        recorder = getattr(self, "recording", None)
        if not wav:
            if recorder is not None:
                recorder.emit("speech.output", {"text": prepared.get("text", ""), "status": "unavailable",
                              "error": prepared.get("tts_error")}, turn_id=recorder.turn_for_request(request_id))
            self._update_pipeline_metrics(
                request_id,
                tts_text_chars=len(str(prepared.get("text", ""))),
                tts_synthesis_ms=prepared.get("tts_synthesis_ms"),
                tts_audio_bytes=0,
                tts_error=prepared.get("tts_error"),
            )
            return None
        dispatch_started_at = time.perf_counter()
        if not self._request_current(request_id) or (prepared.get('generation') is not None and not self._action_current(prepared['generation'])):
            return None
        handle = self.media.speak_wav_bytes(wav, wait=False, cancel_event=self._action_token()[1])
        if recorder is not None:
            recorder.emit("speech.output", {"text": prepared.get("text", ""), "status": "dispatched"},
                          turn_id=recorder.turn_for_request(request_id), media=[(wav, "audio/wav")])
        track = getattr(self, "_track_spoken_playback", None)
        if track is not None:
            track(handle, context=prepared.get("_spoken_followup"))
        if wait and handle is not None:
            wait_for_playback(handle, self._action_token()[1])
        self._update_pipeline_metrics(
            request_id,
            tts_text_chars=len(str(prepared.get("text", ""))),
            tts_synthesis_ms=prepared.get("tts_synthesis_ms"),
            tts_dispatch_ms=round((time.perf_counter() - dispatch_started_at) * 1000, 1),
            tts_audio_bytes=len(wav),
        )
        return handle

    def _play_prepared_tts_with_overlay(
        self,
        prepared: dict,
        face_pattern=None,
        request_id: int | None = None,
        wait: bool = False,
    ):
        if (not prepared or not prepared.get('wav') or not self._request_current(request_id)
                or (prepared.get('generation') is not None and not self._action_current(prepared['generation']))):
            # Keep existing failure metrics, without acquiring or preempting output.
            return self._play_prepared_tts(prepared, request_id=request_id, wait=wait)
        orchestrator = getattr(self, 'orchestrator', None)
        overlay = time.monotonic() >= getattr(self, '_app_display_until', 0)
        lease = orchestrator.acquire(('audio', 'display') if overlay else ('audio',),
                                     cancel_event=self._action_token()[1]) if orchestrator is not None else None
        try:
            with intent_scope(lease.intent) if lease else nullcontext():
                handle = self._play_prepared_tts(prepared, request_id=request_id, wait=False)
                if lease:
                    # Transfer the display reservation to the actual overlay worker.
                    self._start_speaking_face_overlay(handle, face_pattern, _lease=lease)
                    lease = None
                else:
                    self._start_speaking_face_overlay(handle, face_pattern)
                if wait and handle is not None:
                    wait_for_playback(handle, self._action_token()[1])
        finally:
            if lease:
                lease.release()
        return handle

    def _start_speaking_face_overlay(self, handle, face_pattern=None, period_s: float = 0.32, _lease=None) -> None:
        if time.monotonic() < getattr(self, "_app_display_until", 0):
            if _lease:
                _lease.release()
            return
        generation, cancelled = self._action_token()
        pattern = self._resolve_speaking_face_pattern(face_pattern)
        if handle is None or len(pattern) < 2:
            if _lease:
                _lease.release()
            return
        if getattr(self, "_filming_mode", False):
            if _lease:
                _lease.release()
            return
        if getattr(self, "focus_timer", None) is not None and self.focus_timer.is_active():
            if _lease:
                _lease.release()
            return

        def runner() -> None:
            index = 0
            final_face = pattern[-1]
            while True:
                if cancelled.is_set() or (_lease is not None and _lease.is_set()):
                    return
                if time.monotonic() < getattr(self, "_app_display_until", 0):
                    return
                if getattr(self, "_filming_mode", False):
                    break
                if getattr(self, "focus_timer", None) is not None and self.focus_timer.is_active():
                    break
                face_path = pattern[index % len(pattern)]
                self.media.show_face(face_path)
                self.behavior.set_current_face(face_path.name)
                self._set_presentation(face=face_path.name, thinking=False)
                index += 1
                if handle.wait(timeout=max(0.15, float(period_s))) is not None:
                    break
            if (not cancelled.is_set() and not (_lease is not None and _lease.is_set()) and time.monotonic() >= getattr(self, "_app_display_until", 0)
                    and not getattr(self, "_filming_mode", False)
                    and not (getattr(self, "focus_timer", None) is not None and self.focus_timer.is_active())):
                self.media.show_face(final_face)
                self.behavior.set_current_face(final_face.name)
                self._set_presentation(face=final_face.name, thinking=False)

        def leased_runner():
            try:
                runner()
            finally:
                if _lease:
                    _lease.release()
        try:
            future = self._submit_guarded(self._overlay_executor, leased_runner)
            if _lease and future is not None:
                # Guarded submissions may be cancelled before leased_runner begins.
                future.add_done_callback(lambda _: _lease.release())
        except RuntimeError:
            if _lease:
                _lease.release()
            pass

    def _resolve_speaking_face_pattern(self, face_pattern=None) -> list:
        raw_items: list[str] = []
        if isinstance(face_pattern, str):
            raw_items = [item.strip() for item in face_pattern.replace(",", "/").split("/") if item.strip()]
        elif isinstance(face_pattern, list):
            raw_items = [str(item).strip() for item in face_pattern if str(item).strip()]
        elif isinstance(face_pattern, dict):
            raw_items = [str(item).strip() for item in face_pattern.get("faces", []) if str(item).strip()]
        if not raw_items:
            current = self.behavior.snapshot().current_face
            raw_items = [current, "speaking"]
        if "speaking" not in [item.lower().removesuffix(".png") for item in raw_items]:
            raw_items.insert(1, "speaking")
        resolved = []
        for name in raw_items[:6]:
            face = self.assets.get("faces", name, extensions=(".png",))
            if face is not None and face not in resolved:
                resolved.append(face)
        return resolved

    def _play_video_action(self, video_path, audio_path) -> None:
        generation, cancelled = self._action_token()
        previous_face = self.behavior.snapshot().current_face
        try:
            self.media.play_video(video_path, audio_path, cancel_event=cancelled)
        finally:
            face_path = self.assets.get("faces", previous_face, extensions=(".png",))
            if face_path is not None and not cancelled.is_set():
                self.media.show_face(face_path)
                self.behavior.set_current_face(face_path.name)
                self._set_presentation(face=face_path.name, playing_video=None)
