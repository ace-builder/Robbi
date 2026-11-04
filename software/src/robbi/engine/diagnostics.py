"""Composition of existing feature diagnostic producers, independent of transport.

Callbacks resolve components at sampling time so a recovered device is used.
Cached diagnostics never participate in motor or other control decisions.
"""
from dataclasses import asdict

from robbi.diagnostics import DiagnosticSampler, DiagnosticSection


def controller_diagnostics(controller, settings, *, fast_s=2, summary_s=15, catalog_s=60):
    return DiagnosticSampler({
        'behavior': DiagnosticSection(lambda: asdict(controller.behavior.snapshot()), fast_s),
        'presentation': DiagnosticSection(lambda: controller.presentation_state(), fast_s),
        'camera': DiagnosticSection(lambda: controller.camera.health(), fast_s),
        'tof': DiagnosticSection(lambda: controller.tof.status(), fast_s),
        'imu': DiagnosticSection(lambda: controller.imu.status(), fast_s),
        'dynamics': DiagnosticSection(lambda: controller.dynamics.status() if hasattr(controller, 'dynamics') else {}, fast_s),
        'motion': DiagnosticSection(lambda: controller.motion.status(), fast_s),
        'slam': DiagnosticSection(lambda: controller.slam.status(), fast_s),
        'llm_available': DiagnosticSection(lambda: controller.llm.available(settings.ui_text_provider), fast_s),
        'llm': DiagnosticSection(lambda: {
            'provider': controller.llm_provider_name(), 'model': controller.llm_model_name(),
            'tts': settings.tts_provider, 'ui_text_uses_camera_default': settings.attach_photo_for_ui_text,
            'ui_text_provider': settings.ui_text_provider, 'ui_text_model': settings.ui_text_model,
            'speech_input_provider': settings.speech_input_provider,
            'speech_input_model': settings.speech_input_model,
        }, fast_s),
        'performance': DiagnosticSection(lambda: controller.pipeline_metrics(), fast_s),
        'audio': DiagnosticSection(lambda: {
            'volume_percent': controller.media.volume(), 'playing': controller.media.is_audio_playing(),
        }, fast_s),
        'microphone': DiagnosticSection(lambda: controller.microphone.health(), fast_s),
        'wake_word': DiagnosticSection(lambda: controller.microphone.wake_dataset_status(), catalog_s),
        'voice': DiagnosticSection(lambda: {
            'processing': controller._voice_processing, 'wake_enabled': controller.microphone.wake_enabled(),
        }, fast_s),
        'warmup': DiagnosticSection(lambda: controller.warmup_status(), fast_s),
        'heartbeat': DiagnosticSection(lambda: controller.heartbeat_status(), fast_s),
        'background_work': DiagnosticSection(lambda: controller.background_work_snapshot(), fast_s),
        'day_cycle': DiagnosticSection(lambda: controller.day_cycle.snapshot(), fast_s),
        'daily_loop': DiagnosticSection(lambda: controller.daily_loop_status(), summary_s),
        'productivity': DiagnosticSection(lambda: controller.productivity.summary(), summary_s),
        'profile': DiagnosticSection(lambda: controller.onboarding.profile(), summary_s),
        'kids': DiagnosticSection(lambda: controller.kids.status(), fast_s),
        'autonomy': DiagnosticSection(lambda: controller.autonomy_status(), fast_s),
        'live_awareness': DiagnosticSection(lambda: controller.live_awareness.indicator() if hasattr(controller, 'live_awareness') else {}, fast_s),
        'events': DiagnosticSection(lambda: controller.events.stats(), summary_s),
        'tools': DiagnosticSection(lambda: controller.tools.list_tools(), catalog_s),
        'web_search': DiagnosticSection(lambda: {
            'provider': settings.web_search_provider, 'configured': bool(settings.tavily_api_key),
            'available': controller.web_search.available(),
        }, fast_s),
        'filming_mode': DiagnosticSection(lambda: controller.filming_mode(), fast_s),
        'focus_timer': DiagnosticSection(lambda: controller.focus_timer.status(), fast_s),
        'reminders': DiagnosticSection(lambda: controller.reminders.status(), fast_s),
        'telegram': DiagnosticSection(lambda: controller.telegram.status(), fast_s),
        'memory': DiagnosticSection(lambda: controller.memory.stats(), summary_s),
        'ui': DiagnosticSection(lambda: {'debug_mode': settings.ui_debug_mode}, fast_s),
        'assets': DiagnosticSection(lambda: {
            'faces': controller.assets.names('faces'), 'sounds': controller.assets.names('sounds'),
            'videos': controller.assets.names('videos'), 'animations': controller.assets.names('animations'),
            'filming_sequences': controller.assets.names('filming_sequences'),
        }, catalog_s),
        'apps': DiagnosticSection(lambda: controller.apps.list_apps(), catalog_s),
        'components': DiagnosticSection(lambda: controller.health_snapshot(), fast_s),
        'media': DiagnosticSection(lambda: controller.media.health(), fast_s),
        'provider_health': DiagnosticSection(lambda: controller.llm.status(), fast_s),
    })
