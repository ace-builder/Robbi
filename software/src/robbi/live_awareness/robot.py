"""Adapter to existing robot services. No hardware ownership or command dispatch."""

import os

from robbi.configuration.settings import settings

from .session import LiveSession


def make_live_awareness(robot):
    def gate():
        if getattr(robot, '_conversation_lab_active', False):
            return 'Conversation Lab is using the microphone'
        if not robot._running:
            return "Robot is not running"
        if robot._filming_mode:
            return "Filming mode is active"
        if robot.autonomy_policy.is_paused():
            return (
                "Autonomy is paused; resume explicitly before starting live awareness"
            )
        if robot.day_cycle.snapshot().get("quiet"):
            return "Quiet hours are active"
        if robot.focus_timer.is_active():
            return "Focus timer is active"
        return ""

    def audio_allowed():
        if not settings.voice_enabled or settings.mic_sample_rate != 16000:
            return (
                False,
                "Live audio requires the existing microphone at 16 kHz with voice enabled",
            )
        if robot._voice_processing or robot.media.is_audio_playing():
            return False, "Paused for Robbi's speech/conversation"
        return True, ""

    def attach(tap):
        # A failed microphone is optional; video-only sessions still work.
        if hasattr(robot.microphone, "live_audio_tap"):
            robot.microphone.live_audio_tap = tap

    def observation(value, react):
        robot.attention.observe_live(value, react=react,
                                     allow_gestures=bool(live.status().get("options", {}).get("gestures")))
        wake = getattr(robot, "_attention_wake", None)
        if wake is not None and react:
            wake.set()

    def clear():
        robot.attention.clear_live()
        robot.attention.intentions.interrupt("Live awareness ended", source="live")
        if getattr(robot, "_live_reaction_active", False):
            intent = getattr(robot, "_autonomy_intent", None)
            if intent is not None:
                intent.cancel("Live awareness ended")

    live = LiveSession(
        openai_key=lambda: os.getenv("OPENAI_API_KEY") or getattr(settings, "openai_api_key", ""),
        key=lambda: (
            os.getenv("GEMINI_API_KEY") or getattr(settings, "gemini_api_key", "")
        ),
        frame=lambda: robot.camera.get_image_snapshot(
            max_width=480, quality=60, max_age_s=1.5
        ),
        gate=gate,
        audio_allowed=audio_allowed,
        attach_audio=attach,
        on_report=observation,
        clear_evidence=clear,
    )

    return live
