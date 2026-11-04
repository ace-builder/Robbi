from __future__ import annotations
import time
from robbi.configuration.settings import settings
from ..activity import activity
from robbi.orchestration import BACKGROUND

class PresentationDomain:
    def llm_provider_name(self) -> str:
        return settings.ui_text_provider

    def llm_model_name(self) -> str:
        return settings.ui_text_model

    def presentation_state(self) -> dict:
        with self._state_lock:
            return dict(self._presentation_state)

    def pipeline_metrics(self) -> dict:
        with self._state_lock:
            return dict(self._last_pipeline_metrics)

    def warmup_status(self) -> dict:
        with self._state_lock:
            return dict(self._warmup_state)

    def heartbeat_status(self) -> dict:
        with self._state_lock:
            return dict(self._heartbeat_state)

    def capture_photo(self):
        photo = self.camera.save_photo(fresh=True)
        shutter = self.assets.get("sounds", "camerashutter", extensions=(".wav",))
        if photo is not None and shutter is not None and not getattr(self, '_filming_mode', False):
            self.media.play_sound(shutter, wait=False)
        self._publish_event("vision.photo_taken", {"path": str(photo) if photo else "", "ok": photo is not None})
        return photo

    def set_muted(self, muted: bool) -> bool:
        self.behavior.set_muted(muted)
        if muted:
            self.media.stop_audio()
        return self.behavior.snapshot().muted

    def set_volume(self, volume_percent: int) -> int:
        volume = self.media.set_volume(volume_percent)
        self._set_presentation(last_command=f"volume:{volume}")
        return volume

    def set_filming_mode(self, enabled: bool) -> bool:
        if getattr(self, '_filming_service', None) is not None:
            self._filming_service.set_mode(enabled)
            return self._filming_mode
        self._filming_mode = bool(enabled)
        self._set_presentation(last_command=f"filming_mode:{'on' if self._filming_mode else 'off'}")
        return self._filming_mode

    def set_wake_enabled(self, enabled: bool) -> bool:
        wake_enabled = self.microphone.set_wake_enabled(enabled)
        self._set_presentation(last_command=f"wake_word:{'on' if wake_enabled else 'off'}")
        return wake_enabled

    def filming_mode(self) -> bool:
        return self._filming_mode

    def _startup_warmup(self) -> None:
        if self._lifecycle_stop.wait(max(0.0, settings.startup_warmup_delay_s)):
            return
        with self._state_lock:
            self._warmup_state.update({"enabled": True, "running": True, "results": []})
        results: list[dict] = []
        if settings.startup_warmup_chat_enabled:
            seen_targets: set[tuple[str, str]] = set()
            for provider, model in (
                (settings.ui_text_provider, settings.ui_text_model),
                (settings.speech_input_provider, settings.speech_input_model),
            ):
                target = (provider, model)
                if target in seen_targets:
                    continue
                seen_targets.add(target)
                results.append({"kind": "chat", **self.llm.warm_chat(provider, model)})
        else:
            results.append({"kind": "chat", "ok": True, "skipped": True, "reason": "disabled_for_fast_startup"})
        stt_sample = self._warmup_stt_sample()
        if stt_sample:
            results.append({"kind": "stt", **self.llm.warm_stt(stt_sample)})
        results.append({"kind": "tts", **self.llm.warm_tts("ready")})
        with self._state_lock:
            self._warmup_state.update(
                {
                    "enabled": True,
                    "running": False,
                    "completed_at": int(time.time()),
                    "results": results,
                }
            )
        self._finish_startup_presentation()

    def _warmup_stt_sample(self) -> bytes:
        from robbi.configuration.settings import WAKE_REFERENCES_DIR
        candidates = sorted(WAKE_REFERENCES_DIR.glob("*.wav"))
        for candidate in candidates:
            try:
                return candidate.read_bytes()
            except Exception:
                continue
        return b""

    @activity("startup", BACKGROUND)
    def _show_startup_face(self) -> None:
        restarting_face = self.assets.get("faces", "restarting", extensions=(".png",))
        if restarting_face is None:
            restarting_face = self.assets.get("faces", self.behavior.snapshot().current_face, extensions=(".png",))
        if restarting_face is None:
            return
        try:
            self.media.show_face(restarting_face, defer_until_ready=True)
            self._set_presentation(face=restarting_face.name)
        except Exception as exc:
            self._log.warning("Failed to show startup face: %s", exc)

    @activity("startup-ready", BACKGROUND)
    def _finish_startup_presentation(self) -> None:
        happy_face = self.assets.get("faces", "happy", extensions=(".png",))
        if happy_face is not None:
            try:
                self.media.show_face(happy_face, defer_until_ready=True)
                self._set_presentation(face=happy_face.name)
            except Exception as exc:
                self._log.warning("Failed to show ready face: %s", exc)
        self._play_ready_bootup_sound()

    @activity("startup-sound", BACKGROUND)
    def _play_ready_bootup_sound(self) -> None:
        if not self._running or not self.media.health().get('audio', {}).get('available', False):
            return
        bootup = self.assets.get("sounds", "bootup", extensions=(".wav",))
        if bootup is None:
            return
        try:
            self.media.play_sound(bootup, wait=False)
            self._log.info("Startup bootup sound queued: %s", bootup.name)
            self._set_presentation(last_sound=bootup.name)
            with self._state_lock:
                self._warmup_state["ready_sound_played"] = True
        except Exception as exc:
            self._log.warning("Startup bootup sound failed: %s", exc)

    def _set_presentation(self, **updates) -> None:
        with self._state_lock:
            self._presentation_state.update({k: v for k, v in updates.items() if v is not None})

    def _publish_event(self, event_type: str, payload: dict | None = None, source: str = "controller", critical: bool = False) -> None:
        recorder = getattr(self, "recording", None)
        if recorder is not None and event_type.startswith("tool."):
            recorder.emit("tool.result" if "fail" in event_type or "error" in event_type else event_type,
                          {"event": event_type, "source": source, **(payload or {})})
        try:
            self.events.publish(event_type, payload or {}, source=source, critical=critical)
        except Exception:
            pass

    def _store_pipeline_metrics(self, request_id: int, metrics: dict) -> None:
        with self._state_lock:
            self._last_pipeline_request_id = request_id
            self._last_pipeline_metrics = dict(metrics)

    def _update_pipeline_metrics(self, request_id: int | None, **updates) -> None:
        if request_id is None:
            return
        with self._state_lock:
            if self._last_pipeline_request_id != request_id:
                return
            self._last_pipeline_metrics.update({key: value for key, value in updates.items() if value is not None})
