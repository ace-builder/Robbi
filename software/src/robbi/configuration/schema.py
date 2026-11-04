"""Canonical field metadata; importing this module performs no file/device I/O.

Keep YAML paths and flat engine attribute names stable. Profile files are explicit
overrides; field defaults, environment bindings and editable metadata live here.
"""

from dataclasses import dataclass, field, fields
import copy
import math
from .autonomy import AutonomousSettings
from .normalization import normalize_provider, normalize_wake_engine, normalize_reasoning_effort


def option(
    default,
    path,
    env,
    cast,
    *,
    normalize=None,
    edit=None,
    bounds=None,
    legacy=(),
    fallback=None,
    secret=False,
):
    return field(
        default=default,
        metadata=dict(
            path=path,
            env=env,
            cast=cast,
            normalize=normalize,
            edit=edit,
            bounds=bounds,
            legacy=legacy,
            fallback=fallback,
            secret=secret,
        ),
    )


def alias(definition):
    return field(default=definition.default, metadata=definition.metadata)


# Field table: one declaration owns each setting and its metadata.
# fmt: off
@dataclass(slots=True)
class Settings(AutonomousSettings):
    runtime_calibration_confirmed: bool = option(False, 'runtime.calibration_confirmed', 'ROBBI_CALIBRATION_CONFIRMED', bool)
    network_request_timeout_s: float = option(15.0, 'network.request_timeout_s', 'ROBBI_REQUEST_TIMEOUT_S', float, edit=float, bounds=(1, 120))
    network_connect_timeout_s: float = option(3.0, 'network.connect_timeout_s', 'ROBBI_CONNECT_TIMEOUT_S', float, edit=float, bounds=(0.1, 30))
    network_cooldown_s: float = option(5.0, 'network.cooldown_s', 'ROBBI_NETWORK_COOLDOWN_S', float, edit=float, bounds=(0, 300))
    network_max_cooldown_s: float = option(120.0, 'network.max_cooldown_s', 'ROBBI_NETWORK_MAX_COOLDOWN_S', float, edit=float, bounds=(1, 600))
    host: str = option('0.0.0.0', 'server.host', 'ROBBI_HOST', str)
    port: int = option(8081, 'server.port', 'ROBBI_PORT', int, bounds=(1024, 65535))
    log_level: str = option('INFO', 'server.log_level', 'ROBBI_LOG_LEVEL', str)
    hot_reload: bool = option(False, 'server.hot_reload', 'ROBBI_HOT_RELOAD', bool)
    alsa_device: str = option('plughw:1,0', 'audio.alsa_device', 'ROBBI_ALSA_DEVICE', str)
    mic_device: str = option('plughw:CARD=Device,DEV=0', 'audio.mic_device', 'ROBBI_MIC_DEVICE', str)
    mic_sample_rate: int = option(16000, 'audio.mic_sample_rate', 'ROBBI_MIC_SAMPLE_RATE', int, bounds=(8000, 48000))
    stt_provider: str = option('openai', 'stt.provider', 'ROBBI_STT_PROVIDER', str, normalize=normalize_provider, edit=normalize_provider)
    stt_model: str = option('scribe_v2', 'stt.model', 'ROBBI_STT_MODEL', str, edit=lambda value: str(value).strip())
    stt_language: str = option('', 'stt.language', 'ROBBI_STT_LANGUAGE', str, edit=lambda value: str(value).strip())
    openai_api_key: str = option('', None, 'OPENAI_API_KEY', str, secret=True)
    openai_base_url: str = option('', 'providers.openai.base_url', 'OPENAI_BASE_URL', str)
    alibaba_api_key: str = option('', None, 'ALIBABA_API_KEY', str, secret=True)
    alibaba_base_url: str = option('https://coding-intl.dashscope.aliyuncs.com/v1', 'providers.alibaba.base_url', 'ALIBABA_BASE_URL', str)
    openrouter_api_key: str = option('', None, 'OPENROUTER_API_KEY', str, secret=True)
    openrouter_base_url: str = option('https://openrouter.ai/api/v1', 'providers.openrouter.base_url', 'OPENROUTER_BASE_URL', str)
    openrouter_referer: str = option('', 'providers.openrouter.http_referer', 'OPENROUTER_HTTP_REFERER', str)
    openrouter_title: str = option('Robbi', 'providers.openrouter.title', 'OPENROUTER_X_TITLE', str)
    openrouter_reasoning_effort: str = option('', 'providers.openrouter.reasoning_effort', 'OPENROUTER_REASONING_EFFORT', str, normalize=normalize_reasoning_effort, edit=normalize_reasoning_effort)
    gemini_api_key: str = option('', None, 'GEMINI_API_KEY', str, secret=True)
    gemini_base_url: str = option('https://generativelanguage.googleapis.com/v1beta', 'providers.gemini.base_url', 'GEMINI_BASE_URL', str)
    local_api_key: str = option('local', None, 'LOCAL_API_KEY', str, secret=True)
    local_llm_base_url: str = option('http://127.0.0.1:1234/v1', 'providers.local.llm_base_url', 'LOCAL_LLM_BASE_URL', str, edit=lambda value: str(value).strip())
    local_stt_base_url: str = option('http://127.0.0.1:8002/v1', 'providers.local.stt_base_url', 'LOCAL_STT_BASE_URL', str, edit=lambda value: str(value).strip())
    local_tts_base_url: str = option('http://127.0.0.1:8003/v1', 'providers.local.tts_base_url', 'LOCAL_TTS_BASE_URL', str, edit=lambda value: str(value).strip())
    pickup_reactions_enabled: bool = option(True, 'dynamics.pickup_reactions_enabled', 'ROBBI_PICKUP_REACTIONS_ENABLED', bool)
    elevenlabs_api_key: str = option('', None, 'ELEVENLABS_API_KEY', str, secret=True)
    elevenlabs_base_url: str = option('https://api.elevenlabs.io/v1', 'providers.elevenlabs.base_url', 'ELEVENLABS_BASE_URL', str)
    tavily_api_key: str = option('', None, 'TAVILY_API_KEY', str, secret=True)
    telegram_bot_token: str = option('', None, 'TELEGRAM_BOT_TOKEN', str, secret=True)
    chat_provider: str = option('openai', 'llm.chat_provider', 'ROBBI_CHAT_PROVIDER', str, normalize=normalize_provider, legacy=(('llm.provider', 'ROBBI_LLM_PROVIDER'),))
    llm_provider: str = alias(chat_provider)
    sarcasm_level: int = option(4, 'llm.sarcasm_level', 'ROBBI_SARCASM_LEVEL', int, edit=int, bounds=(1, 5))
    chat_model: str = option('gpt-6-luna', 'llm.chat_model', 'ROBBI_CHAT_MODEL', str, legacy=(('llm.model', 'ROBBI_MODEL'),))
    ui_text_provider: str = option('openai', 'llm.ui_text_provider', 'ROBBI_UI_TEXT_PROVIDER', str, normalize=normalize_provider, edit=normalize_provider, fallback='llm.chat_provider')
    ui_text_model: str = option('gpt-6-luna', 'llm.ui_text_model', 'ROBBI_UI_TEXT_MODEL', str, edit=lambda value: str(value).strip(), fallback='llm.chat_model')
    speech_input_provider: str = option('openai', 'llm.speech_input_provider', 'ROBBI_SPEECH_INPUT_PROVIDER', str, normalize=normalize_provider, edit=normalize_provider, fallback='llm.chat_provider')
    speech_input_model: str = option('gpt-6-luna', 'llm.speech_input_model', 'ROBBI_SPEECH_INPUT_MODEL', str, edit=lambda value: str(value).strip(), fallback='llm.chat_model')
    attach_photo_for_ui_text: bool = option(False, 'llm.attach_photo_for_ui_text', 'ROBBI_ATTACH_PHOTO_FOR_UI_TEXT', bool)
    attach_photo_for_speech_input: bool = option(False, 'llm.attach_photo_for_speech_input', 'ROBBI_ATTACH_PHOTO_FOR_SPEECH_INPUT', bool)
    vision_max_width: int = option(512, 'llm.vision_max_width', 'ROBBI_VISION_MAX_WIDTH', int)
    vision_jpeg_quality: int = option(60, 'llm.vision_jpeg_quality', 'ROBBI_VISION_JPEG_QUALITY', int)
    tts_provider: str = option('openai', 'tts.provider', 'ROBBI_TTS_PROVIDER', str, normalize=normalize_provider, edit=normalize_provider)
    tts_model: str = option('tts-1', 'tts.model', 'ROBBI_TTS_MODEL', str, edit=lambda value: str(value).strip())
    tts_voice: str = option('alloy', 'tts.voice', 'ROBBI_TTS_VOICE', str, edit=lambda value: str(value).strip())
    elevenlabs_model: str = option('eleven_v4', 'tts.elevenlabs_model', 'ELEVENLABS_TTS_MODEL', str, edit=lambda value: str(value).strip())
    elevenlabs_voice_id: str = option('', 'tts.elevenlabs_voice_id', 'ELEVENLABS_VOICE_ID', str, edit=lambda value: str(value).strip())
    elevenlabs_output_format: str = option('pcm_16000', 'tts.elevenlabs_output_format', 'ELEVENLABS_OUTPUT_FORMAT', str)
    recent_context_messages: int = option(10, 'memory.recent_context_messages', 'ROBBI_RECENT_CONTEXT_MESSAGES', int, edit=int, bounds=(1, 200))
    conversation_history_messages: int = option(100, 'memory.history_messages', 'ROBBI_HISTORY_MESSAGES', int, edit=int, bounds=(20, 500))
    mood_enabled: bool = option(True, 'personality.mood_enabled', 'ROBBI_MOOD_ENABLED', bool, edit=bool)
    mood_grudge_hours: float = option(18., 'personality.grudge_hours', 'ROBBI_GRUDGE_HOURS', float, edit=float, bounds=(1, 48))
    mood_recovery_hours: float = option(12., 'personality.recovery_hours', 'ROBBI_MOOD_RECOVERY_HOURS', float, edit=float, bounds=(1, 72))
    recent_context_chars: int = option(2400, 'memory.recent_context_chars', 'ROBBI_RECENT_CONTEXT_CHARS', int)
    memory_prompt_max_chars: int = option(6000, 'memory.prompt_max_chars', 'ROBBI_MEMORY_PROMPT_MAX_CHARS', int)
    memory_search_limit: int = option(8, 'memory.search_limit', 'ROBBI_MEMORY_SEARCH_LIMIT', int)
    memory_export_limit: int = option(200, 'memory.export_limit', 'ROBBI_MEMORY_EXPORT_LIMIT', int)
    memory_extraction_enabled: bool = option(True, 'memory.extraction_enabled', 'ROBBI_MEMORY_EXTRACTION_ENABLED', bool, edit=bool)
    memory_export_markdown_enabled: bool = option(True, 'memory.export_markdown_enabled', 'ROBBI_MEMORY_EXPORT_MARKDOWN_ENABLED', bool, edit=bool)
    events_enabled: bool = option(True, 'events.enabled', 'ROBBI_EVENTS_ENABLED', bool, edit=bool)
    events_max_queue_size: int = option(500, 'events.max_queue_size', 'ROBBI_EVENTS_MAX_QUEUE_SIZE', int, edit=int, bounds=(10, 5000))
    autonomy_enabled: bool = option(False, 'autonomy.enabled', 'ROBBI_AUTONOMY_ENABLED', bool, edit=bool)
    autonomy_level: str = option('quiet', 'autonomy.level', 'ROBBI_AUTONOMY_LEVEL', str, edit=lambda value: str(value).strip().lower())
    autonomy_min_interval_s: float = option(60.0, 'autonomy.min_interval_s', 'ROBBI_AUTONOMY_MIN_INTERVAL_S', float, edit=float, bounds=(10, 86400))
    autonomy_max_interval_s: float = option(300.0, 'autonomy.max_interval_s', 'ROBBI_AUTONOMY_MAX_INTERVAL_S', float, edit=float, bounds=(10, 86400))
    autonomy_cooldown_after_user_s: float = option(120.0, 'autonomy.cooldown_after_user_s', 'ROBBI_AUTONOMY_COOLDOWN_AFTER_USER_S', float, edit=float, bounds=(10, 86400))
    autonomy_allow_vision: bool = option(False, 'autonomy.allow_vision', 'ROBBI_AUTONOMY_ALLOW_VISION', bool, edit=bool)
    autonomy_allow_web_search: bool = option(False, 'autonomy.allow_web_search', 'ROBBI_AUTONOMY_ALLOW_WEB_SEARCH', bool, edit=bool)
    daily_loop_enabled: bool = option(True, 'daily_loop.enabled', 'ROBBI_DAILY_LOOP_ENABLED', bool, edit=bool)
    daily_loop_mode: str = option('active', 'daily_loop.mode', 'ROBBI_DAILY_LOOP_MODE', str, edit=lambda value: str(value).strip().lower())
    daily_loop_max_proactive_per_day: int = option(3, 'daily_loop.max_proactive_per_day', 'ROBBI_DAILY_LOOP_MAX_PROACTIVE_PER_DAY', int, edit=int, bounds=(0, 20))
    daily_loop_min_interval_s: float = option(7200.0, 'daily_loop.min_interval_s', 'ROBBI_DAILY_LOOP_MIN_INTERVAL_S', float, edit=float, bounds=(60, 86400))
    daily_loop_todo_followup_enabled: bool = option(True, 'daily_loop.todo_followup_enabled', 'ROBBI_DAILY_LOOP_TODO_FOLLOWUP_ENABLED', bool, edit=bool)
    daily_loop_memory_review_nudge_enabled: bool = option(True, 'daily_loop.memory_review_nudge_enabled', 'ROBBI_DAILY_LOOP_MEMORY_REVIEW_NUDGE_ENABLED', bool, edit=bool)
    daily_loop_telegram_brief_enabled: bool = option(True, 'daily_loop.telegram_brief_enabled', 'ROBBI_DAILY_LOOP_TELEGRAM_BRIEF_ENABLED', bool, edit=bool)
    conversation_followup_enabled: bool = option(True, 'conversation.followup_enabled', 'ROBBI_CONVERSATION_FOLLOWUP_ENABLED', bool, edit=bool)
    conversation_followup_timeout_s: float = option(5.0, 'conversation.followup_timeout_s', 'ROBBI_CONVERSATION_FOLLOWUP_TIMEOUT_S', float, edit=float, bounds=(2, 60))
    conversation_max_turns: int = option(0, 'conversation.max_turns', 'ROBBI_CONVERSATION_MAX_TURNS', int, edit=int, bounds=(0, 200))
    conversation_end_phrases: str = option("stop,that's all,bye,cancel", 'conversation.end_phrases', 'ROBBI_CONVERSATION_END_PHRASES', str, edit=lambda value: str(value).strip())
    web_search_provider: str = option('disabled', 'web_search.provider', 'ROBBI_WEB_SEARCH_PROVIDER', str, edit=lambda value: str(value).strip().lower())
    web_search_search_depth: str = option('basic', 'web_search.search_depth', 'ROBBI_WEB_SEARCH_DEPTH', str, edit=lambda value: str(value).strip().lower())
    web_search_max_results: int = option(4, 'web_search.max_results', 'ROBBI_WEB_SEARCH_MAX_RESULTS', int, edit=int, bounds=(1, 10))
    startup_warmup_enabled: bool = option(True, 'startup_warmup.enabled', 'ROBBI_STARTUP_WARMUP_ENABLED', bool)
    startup_warmup_delay_s: float = option(1.5, 'startup_warmup.delay_s', 'ROBBI_STARTUP_WARMUP_DELAY_S', float)
    startup_warmup_chat_enabled: bool = option(False, 'startup_warmup.chat_enabled', 'ROBBI_STARTUP_WARMUP_CHAT_ENABLED', bool)
    heartbeat_enabled: bool = option(True, 'heartbeat.enabled', 'ROBBI_HEARTBEAT_ENABLED', bool, edit=bool)
    heartbeat_initial_delay_s: float = option(45.0, 'heartbeat.initial_delay_s', 'ROBBI_HEARTBEAT_INITIAL_DELAY_S', float, edit=float, bounds=(0, 3600))
    heartbeat_min_interval_s: float = option(60.0, 'heartbeat.min_interval_s', 'ROBBI_HEARTBEAT_MIN_INTERVAL_S', float)
    heartbeat_max_interval_s: float = option(300.0, 'heartbeat.max_interval_s', 'ROBBI_HEARTBEAT_MAX_INTERVAL_S', float)
    heartbeat_recent_user_quiet_s: float = option(120.0, 'heartbeat.recent_user_quiet_s', 'ROBBI_HEARTBEAT_RECENT_USER_QUIET_S', float, edit=float, bounds=(0, 86400))
    heartbeat_idle_cooldown_s: float = option(180.0, 'heartbeat.idle_cooldown_s', 'ROBBI_HEARTBEAT_IDLE_COOLDOWN_S', float)
    heartbeat_max_prompt_chars: int = option(5000, 'heartbeat.prompt_max_chars', 'ROBBI_HEARTBEAT_PROMPT_MAX_CHARS', int)
    apps_enabled: bool = option(True, 'apps.enabled', 'ROBBI_APPS_ENABLED', bool)
    app_hub_url: str = option('https://ai-tier.com/apphub', 'app_hub.url', 'ROBBI_APP_HUB_URL', str, edit=lambda value: str(value).strip().rstrip('/'))
    ui_debug_mode: bool = option(False, 'ui.debug_mode', 'ROBBI_UI_DEBUG_MODE', bool, edit=bool)
    telegram_enabled: bool = option(False, 'telegram.enabled', 'ROBBI_TELEGRAM_ENABLED', bool, edit=bool)
    telegram_allowed_chat_ids: str = option('', 'telegram.allowed_chat_ids', 'ROBBI_TELEGRAM_ALLOWED_CHAT_IDS', str, edit=lambda value: str(value).strip())
    telegram_poll_interval_s: float = option(1.0, 'telegram.poll_interval_s', 'ROBBI_TELEGRAM_POLL_INTERVAL_S', float, edit=float, bounds=(0.2, 30))
    telegram_notify_reminders: bool = option(True, 'telegram.notify_reminders', 'ROBBI_TELEGRAM_NOTIFY_REMINDERS', bool, edit=bool)
    telegram_notify_proactive: bool = option(True, 'telegram.notify_proactive', 'ROBBI_TELEGRAM_NOTIFY_PROACTIVE', bool, edit=bool)
    telegram_allow_commands: bool = option(True, 'telegram.allow_commands', 'ROBBI_TELEGRAM_ALLOW_COMMANDS', bool, edit=bool)
    telegram_allow_photos: bool = option(False, 'telegram.allow_photos', 'ROBBI_TELEGRAM_ALLOW_PHOTOS', bool, edit=bool)
    telegram_allow_voice: bool = option(True, 'telegram.allow_voice', 'ROBBI_TELEGRAM_ALLOW_VOICE', bool, edit=bool)
    telegram_send_audio_replies: bool = option(False, 'telegram.send_audio_replies', 'ROBBI_TELEGRAM_SEND_AUDIO_REPLIES', bool, edit=bool)
    telegram_allow_robot_actions: bool = option(False, 'telegram.allow_robot_actions', 'ROBBI_TELEGRAM_ALLOW_ROBOT_ACTIONS', bool, edit=bool)
    telegram_speak_remote_messages: bool = option(False, 'telegram.speak_remote_messages', 'ROBBI_TELEGRAM_SPEAK_REMOTE_MESSAGES', bool, edit=bool)
    camera_enabled: bool = option(True, 'camera.enabled', 'ROBBI_CAMERA_ENABLED', bool)
    camera_width: int = option(1280, 'camera.width', 'ROBBI_CAMERA_WIDTH', int, bounds=(64, 1920))
    camera_height: int = option(720, 'camera.height', 'ROBBI_CAMERA_HEIGHT', int, bounds=(64, 1080))
    camera_fps: float = option(5.0, 'camera.fps', 'ROBBI_CAMERA_FPS', float, bounds=(0.2, 15))
    camera_warmup_s: float = option(2.0, 'camera.warmup_s', 'ROBBI_CAMERA_WARMUP_S', float, bounds=(0, 10))
    camera_tuning_file: str = option('', 'camera.tuning_file', 'ROBBI_CAMERA_TUNING_FILE', str)
    camera_edge_tint_fix_enabled: bool = option(False, 'camera.edge_tint_fix.enabled', 'ROBBI_CAMERA_EDGE_TINT_FIX_ENABLED', bool)
    camera_edge_tint_fix_start: float = option(0.62, 'camera.edge_tint_fix.start', 'ROBBI_CAMERA_EDGE_TINT_FIX_START', float)
    camera_edge_tint_fix_exponent: float = option(2.0, 'camera.edge_tint_fix.exponent', 'ROBBI_CAMERA_EDGE_TINT_FIX_EXPONENT', float)
    camera_edge_tint_fix_green_boost: float = option(0.18, 'camera.edge_tint_fix.green_boost', 'ROBBI_CAMERA_EDGE_TINT_FIX_GREEN_BOOST', float)
    camera_edge_tint_fix_red_reduction: float = option(-1.0, 'camera.edge_tint_fix.red_reduction', 'ROBBI_CAMERA_EDGE_TINT_FIX_RED_REDUCTION', float)
    camera_edge_tint_fix_blue_reduction: float = option(-1.0, 'camera.edge_tint_fix.blue_reduction', 'ROBBI_CAMERA_EDGE_TINT_FIX_BLUE_REDUCTION', float)
    camera_edge_tint_fix_rb_reduction: float = option(0.12, 'camera.edge_tint_fix.rb_reduction', 'ROBBI_CAMERA_EDGE_TINT_FIX_RB_REDUCTION', float)
    voice_enabled: bool = option(True, 'voice.enabled', 'ROBBI_VOICE_ENABLED', bool)
    voice_wake_engine: str = option('auto', 'voice.wake_engine', 'ROBBI_VOICE_WAKE_ENGINE', str, normalize=normalize_wake_engine, edit=normalize_wake_engine)
    voice_wake_phrase: str = option('hey robbi', 'voice.wake_phrase', 'ROBBI_VOICE_WAKE_PHRASE', str)
    voice_ready_sound: str = option('', 'voice.ready_sound', 'ROBBI_VOICE_READY_SOUND', str)
    voice_ready_face: str = option('listen', 'voice.ready_face', 'ROBBI_VOICE_READY_FACE', str)
    voice_chunk_ms: int = option(100, 'voice.chunk_ms', 'ROBBI_VOICE_CHUNK_MS', int, bounds=(20, 200))
    voice_pre_roll_ms: int = option(500, 'voice.pre_roll_ms', 'ROBBI_VOICE_PRE_ROLL_MS', int)
    voice_min_rms: int = option(260, 'voice.min_rms', 'ROBBI_VOICE_MIN_RMS', int)
    voice_noise_multiplier: float = option(1.9, 'voice.noise_multiplier', 'ROBBI_VOICE_NOISE_MULTIPLIER', float)
    voice_start_chunks: int = option(3, 'voice.start_chunks', 'ROBBI_VOICE_START_CHUNKS', int)
    voice_silence_ms: int = option(650, 'voice.silence_ms', 'ROBBI_VOICE_SILENCE_MS', int, edit=int, bounds=(200, 5000))
    voice_command_start_chunks: int = option(1, 'voice.command_start_chunks', 'ROBBI_VOICE_COMMAND_START_CHUNKS', int, edit=int, bounds=(1, 10))
    voice_command_silence_ms: int = option(550, 'voice.command_silence_ms', 'ROBBI_VOICE_COMMAND_SILENCE_MS', int, edit=int, bounds=(150, 5000))
    voice_command_wait_s: float = option(8.0, 'voice.command_wait_s', 'ROBBI_VOICE_COMMAND_WAIT_S', float, edit=float, bounds=(1, 60))
    voice_ready_tone_block_s: float = option(0.15, 'voice.ready_tone_block_s', 'ROBBI_VOICE_READY_TONE_BLOCK_S', float, edit=float, bounds=(0, 5))
    voice_min_wake_s: float = option(0.45, 'voice.min_wake_s', 'ROBBI_VOICE_MIN_WAKE_S', float)
    voice_wake_phrase_max_s: float = option(3.0, 'voice.wake_phrase_max_s', 'ROBBI_VOICE_WAKE_PHRASE_MAX_S', float)
    voice_min_wake_speech_chunks: int = option(4, 'voice.min_wake_speech_chunks', 'ROBBI_VOICE_MIN_WAKE_SPEECH_CHUNKS', int)
    voice_command_max_s: float = option(6.0, 'voice.command_max_s', 'ROBBI_VOICE_COMMAND_MAX_S', float, edit=float, bounds=(1, 120))
    voice_min_command_s: float = option(0.45, 'voice.min_command_s', 'ROBBI_VOICE_MIN_COMMAND_S', float)
    voice_min_command_speech_chunks: int = option(2, 'voice.min_command_speech_chunks', 'ROBBI_VOICE_MIN_COMMAND_SPEECH_CHUNKS', int, edit=int, bounds=(1, 20))
    voice_cooldown_s: float = option(2.0, 'voice.cooldown_s', 'ROBBI_VOICE_COOLDOWN_S', float)
    voice_empty_wake_cooldown_s: float = option(1.2, 'voice.empty_wake_cooldown_s', 'ROBBI_VOICE_EMPTY_WAKE_COOLDOWN_S', float)
    voice_reject_wake_cooldown_s: float = option(0.6, 'voice.reject_wake_cooldown_s', 'ROBBI_VOICE_REJECT_WAKE_COOLDOWN_S', float)
    voice_local_wake_method: str = option('mfcc', 'voice.local_wake_method', 'ROBBI_VOICE_LOCAL_WAKE_METHOD', str)
    voice_local_wake_threshold: float = option(0.22, 'voice.local_wake_threshold', 'ROBBI_VOICE_LOCAL_WAKE_THRESHOLD', float, edit=float, bounds=(0.01, 0.5))
    voice_local_wake_buffer_s: float = option(1.8, 'voice.local_wake_buffer_s', 'ROBBI_VOICE_LOCAL_WAKE_BUFFER_S', float, bounds=(0.2, 4))
    voice_local_wake_slide_s: float = option(0.35, 'voice.local_wake_slide_s', 'ROBBI_VOICE_LOCAL_WAKE_SLIDE_S', float, bounds=(0.05, 2))
    voice_local_wake_max_references: int = option(8, 'voice.local_wake_max_references', 'ROBBI_VOICE_LOCAL_WAKE_MAX_REFERENCES', int, edit=int, bounds=(1, 32))
    voice_local_wake_max_reference_s: float = option(4.0, 'voice.local_wake_max_reference_s', 'ROBBI_VOICE_LOCAL_WAKE_MAX_REFERENCE_S', float, edit=float, bounds=(0.2, 10))
    voice_local_wake_max_reference_bytes: int = option(2097152, 'voice.local_wake_max_reference_bytes', 'ROBBI_VOICE_LOCAL_WAKE_MAX_REFERENCE_BYTES', int, edit=int, bounds=(4096, 8388608))
    voice_local_wake_max_total_feature_frames: int = option(1600, 'voice.local_wake_max_total_feature_frames', 'ROBBI_VOICE_LOCAL_WAKE_MAX_TOTAL_FEATURE_FRAMES', int, edit=int, bounds=(100, 6400))
    voice_local_wake_min_active_rms: int = option(180, 'voice.local_wake_min_active_rms', 'ROBBI_VOICE_LOCAL_WAKE_MIN_ACTIVE_RMS', int)
    voice_reference_record_s: float = option(2.2, 'voice.reference_record_s', 'ROBBI_VOICE_REFERENCE_RECORD_S', float)
    voice_nanowake_model: str = option('', 'voice.nanowake_model', 'ROBBI_VOICE_NANOWAKE_MODEL', str)
    voice_nanowake_threshold: float = option(0.65, 'voice.nanowake_threshold', 'ROBBI_VOICE_NANOWAKE_THRESHOLD', float, edit=float, bounds=(0.01, 1))
    voice_nanowake_patience: int = option(2, 'voice.nanowake_patience', 'ROBBI_VOICE_NANOWAKE_PATIENCE', int, edit=int, bounds=(1, 10))
    voice_nanowake_vad_threshold: float = option(0.35, 'voice.nanowake_vad_threshold', 'ROBBI_VOICE_NANOWAKE_VAD_THRESHOLD', float)
    cleanup_days: int = option(90, 'retention.cleanup_days', 'ROBBI_CLEANUP_DAYS', int)
    enable_lcd: bool = option(True, 'hardware.enable_lcd', 'ROBBI_ENABLE_LCD', bool)
    enable_motors: bool = option(False, 'hardware.enable_motors', 'ROBBI_ENABLE_MOTORS', bool)
    enable_servos: bool = option(False, 'hardware.enable_servos', 'ROBBI_ENABLE_SERVOS', bool)
    drive_default_duration_s: float = option(0.7, 'motion.drive_default_duration_s', 'ROBBI_DRIVE_DEFAULT_DURATION_S', float)
    motion_encoder_enabled: bool = option(True, 'motion.encoder_enabled', 'ROBBI_MOTION_ENCODER_ENABLED', bool, edit=bool)
    motion_default_step_cm: float = option(30, 'motion.default_step_cm', 'ROBBI_MOTION_DEFAULT_STEP_CM', float, edit=float, bounds=(1, 100))
    motion_default_turn_deg: float = option(90, 'motion.default_turn_deg', 'ROBBI_MOTION_DEFAULT_TURN_DEG', float, edit=float, bounds=(1, 360))
    motion_wheel_diameter_cm: float = option(6.2, 'motion.wheel_diameter_cm', 'ROBBI_MOTION_WHEEL_DIAMETER_CM', float, edit=float, bounds=(0.1, 100))
    motion_wheel_base_cm: float = option(9.8, 'motion.wheel_base_cm', 'ROBBI_MOTION_WHEEL_BASE_CM', float, edit=float, bounds=(1, 100))
    motion_encoder_counts_per_rev: int = option(2800, 'motion.encoder_counts_per_rev', 'ROBBI_MOTION_ENCODER_COUNTS_PER_REV', int, edit=int, bounds=(1, 100000))
    motion_encoder_tolerance_counts: int = option(40, 'motion.encoder_tolerance_counts', 'ROBBI_MOTION_ENCODER_TOLERANCE_COUNTS', int, edit=int, bounds=(1, 500))
    motion_turn_max_duty: float = option(100.0, 'motion.turn_max_duty', 'ROBBI_MOTION_TURN_MAX_DUTY', float, edit=float, bounds=(0, 100))
    motion_turn_start_duty: float = option(100.0, 'motion.turn_start_duty', 'ROBBI_MOTION_TURN_START_DUTY', float, edit=float, bounds=(0, 100))
    motion_turn_tolerance_counts: int = option(12, 'motion.turn_tolerance_counts', 'ROBBI_MOTION_TURN_TOLERANCE_COUNTS', int, edit=int, bounds=(1, 100))
    motion_imu_heading_enabled: bool = option(True, 'motion.imu_heading_enabled', 'ROBBI_MOTION_IMU_HEADING_ENABLED', bool, edit=bool)
    motion_imu_gyro_weight: float = option(0.98, 'motion.imu_gyro_weight', 'ROBBI_MOTION_IMU_GYRO_WEIGHT', float, edit=float, bounds=(0.5, 1))
    motion_imu_turn_tolerance_deg: float = option(2.0, 'motion.imu_turn_tolerance_deg', 'ROBBI_MOTION_IMU_TURN_TOLERANCE_DEG', float, edit=float, bounds=(0.5, 10))
    motion_pwm_max_duty: float = option(100.0, 'motion.pwm_max_duty', 'ROBBI_MOTION_PWM_MAX_DUTY', float, edit=float, bounds=(0, 100))
    motion_pwm_min_duty: float = option(50, 'motion.pwm_min_duty', 'ROBBI_MOTION_PWM_MIN_DUTY', float, edit=float, bounds=(0, 100))
    motion_pwm_ramp_min: float = option(0.35, 'motion.pwm_ramp_min', 'ROBBI_MOTION_PWM_RAMP_MIN', float, edit=float, bounds=(0.1, 1))
    motion_sync_kp: float = option(0.35, 'motion.sync_kp', 'ROBBI_MOTION_SYNC_KP', float, edit=float, bounds=(0, 5))
    motion_curve_pwm_slew_per_s: float = option(200, 'motion.curve_pwm_slew_per_s', 'ROBBI_MOTION_CURVE_PWM_SLEW_PER_S', float, edit=float, bounds=(20, 1000))
    motion_curve_trim_gain: float = option(0.8, 'motion.curve_trim_gain', 'ROBBI_MOTION_CURVE_TRIM_GAIN', float, edit=float, bounds=(0, 3))
    motion_curve_cross_track_gain: float = option(1.0, 'motion.curve_cross_track_gain', 'ROBBI_MOTION_CURVE_CROSS_TRACK_GAIN', float, edit=float, bounds=(0, 2))
    motion_curve_start_assist: bool = option(True, 'motion.curve_start_assist', 'ROBBI_MOTION_CURVE_START_ASSIST', bool, edit=bool)
    motion_pid_kp: float = option(0.125, 'motion.pid_kp', 'ROBBI_MOTION_PID_KP', float, edit=float, bounds=(0, 2))
    motion_pid_ki: float = option(0.0, 'motion.pid_ki', 'ROBBI_MOTION_PID_KI', float, edit=float, bounds=(0, 1))
    motion_pid_kd: float = option(0.0, 'motion.pid_kd', 'ROBBI_MOTION_PID_KD', float, edit=float, bounds=(0, 1))
    motion_pid_integral_limit: float = option(300.0, 'motion.pid_integral_limit', 'ROBBI_MOTION_PID_INTEGRAL_LIMIT', float, edit=float, bounds=(0, 10000))
    motion_stop_margin_per_duty: float = option(0.5, 'motion.stop_margin_per_duty', 'ROBBI_MOTION_STOP_MARGIN_PER_DUTY', float, edit=float, bounds=(0, 20))
    motion_stop_margin_ratio: float = option(0.1, 'motion.stop_margin_ratio', 'ROBBI_MOTION_STOP_MARGIN_RATIO', float, edit=float, bounds=(0, 0.5))
    motion_control_interval_s: float = option(0.01, 'motion.control_interval_s', 'ROBBI_MOTION_CONTROL_INTERVAL_S', float, edit=float, bounds=(0.01, 0.05))
    motion_encoder_timeout_s: float = option(8, 'motion.encoder_timeout_s', 'ROBBI_MOTION_ENCODER_TIMEOUT_S', float, edit=float, bounds=(1, 30))
    motion_stall_timeout_s: float = option(0.75, 'motion.stall_timeout_s', 'ROBBI_MOTION_STALL_TIMEOUT_S', float, edit=float, bounds=(0.2, 5))
    motion_brake_s: float = option(0.08, 'motion.brake_s', 'ROBBI_MOTION_BRAKE_S', float, edit=float, bounds=(0, 0.2))
    motion_swap_motor_outputs: bool = option(False, 'motion.swap_motor_outputs', 'ROBBI_MOTION_SWAP_MOTOR_OUTPUTS', bool, edit=bool)
    motion_swap_encoder_inputs: bool = option(False, 'motion.swap_encoder_inputs', 'ROBBI_MOTION_SWAP_ENCODER_INPUTS', bool, edit=bool)
    motion_obstacle_stop_enabled: bool = option(True, 'motion.obstacle_stop_enabled', 'ROBBI_MOTION_OBSTACLE_STOP_ENABLED', bool, edit=bool)
    motion_obstacle_stop_cm: float = option(5.0, 'motion.obstacle_stop_cm', 'ROBBI_MOTION_OBSTACLE_STOP_CM', float, edit=float, bounds=(1, 100))
    motion_arc_radius_cm: float = option(20, 'motion.arc_radius_cm', 'ROBBI_MOTION_ARC_RADIUS_CM', float, edit=float, bounds=(1, 100))
    motion_sine_radius_cm: float = option(15, 'motion.sine_radius_cm', 'ROBBI_MOTION_SINE_RADIUS_CM', float, edit=float, bounds=(1, 100))
    motion_sine_angle_deg: float = option(45, 'motion.sine_angle_deg', 'ROBBI_MOTION_SINE_ANGLE_DEG', float, edit=float, bounds=(1, 90))
    motion_wiggle_angle_deg: float = option(45, 'motion.wiggle_angle_deg', 'ROBBI_MOTION_WIGGLE_ANGLE_DEG', float, edit=float, bounds=(1, 90))
    motion_avoid_backoff_cm: float = option(5.0, 'motion.avoid_backoff_cm', 'ROBBI_MOTION_AVOID_BACKOFF_CM', float, edit=float, bounds=(1, 100))
    motion_avoid_turn_deg: float = option(45.0, 'motion.avoid_turn_deg', 'ROBBI_MOTION_AVOID_TURN_DEG', float, edit=float, bounds=(1, 360))
    motion_left_motor_invert: bool = option(False, 'motion.left_motor_invert', 'ROBBI_MOTION_LEFT_MOTOR_INVERT', bool, edit=bool)
    motion_right_motor_invert: bool = option(False, 'motion.right_motor_invert', 'ROBBI_MOTION_RIGHT_MOTOR_INVERT', bool, edit=bool)
    motion_encoder_left_invert: bool = option(False, 'motion.encoder_left_invert', 'ROBBI_MOTION_ENCODER_LEFT_INVERT', bool, edit=bool)
    motion_encoder_right_invert: bool = option(False, 'motion.encoder_right_invert', 'ROBBI_MOTION_ENCODER_RIGHT_INVERT', bool, edit=bool)
    imu_enabled: bool = option(True, 'imu.enabled', 'ROBBI_IMU_ENABLED', bool, edit=bool)
    imu_poll_hz: float = option(50.0, 'imu.poll_hz', 'ROBBI_IMU_POLL_HZ', float, edit=float, bounds=(10, 100))
    tof_enabled: bool = option(True, 'tof.enabled', 'ROBBI_TOF_ENABLED', bool, edit=bool)
    tof_h2_enabled: bool = option(False, 'tof.h2_enabled', 'ROBBI_TOF_H2_ENABLED', bool, edit=bool)
    tof_poll_hz: float = option(200.0, 'tof.poll_hz', 'ROBBI_TOF_POLL_HZ', float, edit=float, bounds=(0.5, 200))
    tof_i2c_address: int = option(48, 'tof.i2c_address', 'ROBBI_TOF_I2C_ADDRESS', int)
    tof_distance_mode: int = option(2, 'tof.distance_mode', 'ROBBI_TOF_DISTANCE_MODE', int, edit=int, bounds=(1, 2))
    tof_timing_budget_ms: int = option(33, 'tof.timing_budget_ms', 'ROBBI_TOF_TIMING_BUDGET_MS', int, edit=int, bounds=(15, 500))
    tof_intermeasurement_ms: int = option(0, 'tof.intermeasurement_ms', 'ROBBI_TOF_INTERMEASUREMENT_MS', int, edit=int, bounds=(0, 2000))
    tof_roi_size: int = option(16, 'tof.roi_size', 'ROBBI_TOF_ROI_SIZE', int, edit=int, bounds=(4, 16))
    tof_max_age_s: float = option(0.75, 'tof.max_age_s', 'ROBBI_TOF_MAX_AGE_S', float, edit=float, bounds=(0.1, 5))
    slam_enabled: bool = option(True, 'slam.enabled', 'ROBBI_SLAM_ENABLED', bool, edit=bool)
    slam_grid_resolution_cm: float = option(5.0, 'slam.grid_resolution_cm', 'ROBBI_SLAM_GRID_RESOLUTION_CM', float, edit=float, bounds=(1, 20))
    slam_scan_step_deg: float = option(8.0, 'slam.scan_step_deg', 'ROBBI_SLAM_SCAN_STEP_DEG', float, edit=float, bounds=(2, 90))
    slam_samples_per_angle: int = option(3, 'slam.samples_per_angle', 'ROBBI_SLAM_SAMPLES_PER_ANGLE', int, edit=int, bounds=(1, 10))
    slam_max_range_cm: float = option(400.0, 'slam.max_range_cm', 'ROBBI_SLAM_MAX_RANGE_CM', float, edit=float, bounds=(20, 400))
    slam_min_range_cm: float = option(5.0, 'slam.min_range_cm', 'ROBBI_SLAM_MIN_RANGE_CM', float, edit=float, bounds=(1, 400))
    slam_explore_step_cm: float = option(100.0, 'slam.explore_step_cm', 'ROBBI_SLAM_EXPLORE_STEP_CM', float, edit=float, bounds=(1, 100))
    slam_max_explore_steps: int = option(0, 'slam.max_explore_steps', 'ROBBI_SLAM_MAX_EXPLORE_STEPS', int, edit=int, bounds=(0, 100))
    slam_min_pose_confidence: float = option(0.45, 'slam.min_pose_confidence', 'ROBBI_SLAM_MIN_POSE_CONFIDENCE', float, edit=float, bounds=(0, 1))
    slam_scan_match_enabled: bool = option(True, 'slam.scan_match_enabled', 'ROBBI_SLAM_SCAN_MATCH_ENABLED', bool, edit=bool)
    slam_turn_speed: int = option(100, 'slam.turn_speed', 'ROBBI_SLAM_TURN_SPEED', int, edit=int, bounds=(1, 100))
    slam_drive_speed: int = option(100, 'slam.drive_speed', 'ROBBI_SLAM_DRIVE_SPEED', int, edit=int, bounds=(1, 100))
    slam_sample_settle_s: float = option(0.08, 'slam.sample_settle_s', 'ROBBI_SLAM_SAMPLE_SETTLE_S', float, edit=float, bounds=(0, 5))
    slam_scan_mode: str = option('stepped', 'slam.scan_mode', 'ROBBI_SLAM_SCAN_MODE', str, edit=lambda value: str(value).strip().lower())
    slam_continuous_scan_hz: float = option(10.0, 'slam.continuous_scan_hz', 'ROBBI_SLAM_CONTINUOUS_SCAN_HZ', float, edit=float, bounds=(1, 25))
    slam_continuous_turn_speed: int = option(100, 'slam.continuous_turn_speed', 'ROBBI_SLAM_CONTINUOUS_TURN_SPEED', int, edit=int, bounds=(1, 100))
    slam_continuous_scan_timeout_s: float = option(25.0, 'slam.continuous_scan_timeout_s', 'ROBBI_SLAM_CONTINUOUS_SCAN_TIMEOUT_S', float, edit=float, bounds=(10, 300))
    slam_min_scan_samples: int = option(30, 'slam.min_scan_samples', 'ROBBI_SLAM_MIN_SCAN_SAMPLES', int, edit=int, bounds=(5, 120))

# fmt: on

RUNTIME_BOUNDS = {
    "runtime.worker_threads": (2, 8),
    "runtime.command_queue_size": (0, 8),
    "runtime.status_interval_s": (0.5, 30),
    "runtime.summary_interval_s": (2, 300),
    "runtime.catalog_interval_s": (5, 600),
    "runtime.feature_task_limit": (8, 32),
    "audio_studio.max_storage_mb": (16, 4096),
    "audio_studio.max_clip_mb": (1, 32),
    "audio_studio.history_limit": (10, 2000),
}

SPECS = {}
BY_ATTR = {}
for item in fields(Settings):
    meta = dict(item.metadata)
    path = meta["path"]
    meta.update(attr=item.name, default=item.default)
    if "env" not in meta:  # Reuse the autonomous domain's existing metadata.
        meta.update(
            env="ROBBI_" + path.upper().replace(".", "_"),
            edit=meta["cast"],
            bounds=(meta["low"], meta["high"]) if meta["low"] is not None else None,
            normalize=None,
            legacy=(),
            fallback=None,
            secret=False,
        )
    BY_ATTR[item.name] = meta
    if path and path not in SPECS:
        SPECS[path] = meta

NUMERIC_FIELDS = {
    path: dict(
        min=m["bounds"][0],
        max=m["bounds"][1],
        default=m["default"],
        integer=m["cast"] is int,
        step=1 if m["cast"] is int else "any",
    )
    for path, m in SPECS.items()
    if m["bounds"]
}


def editable_fields():
    return {
        path: dict(attr=m["attr"], cast=m["cast"], normalize=m["edit"])
        for path, m in SPECS.items()
        if m["edit"] is not None
    }


def validated_number(key, value):
    spec = NUMERIC_FIELDS[key]
    try:
        if isinstance(value, bool) or value is None or value == "":
            raise ValueError()
        numeric = float(value)
        if not math.isfinite(numeric) or not spec["min"] <= numeric <= spec["max"]:
            raise ValueError()
        if spec["integer"] and (not numeric.is_integer()):
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise ValueError(
            f"{key} must be {('an integer ' if spec['integer'] else '')}between {spec['min']} and {spec['max']}"
        ) from None
    return int(numeric) if spec["integer"] else numeric


def lookup(data, key, default=None):
    for part in key.split("."):
        if not isinstance(data, dict) or part not in data:
            return default
        data = data[part]
    return data


def put(data, key, value):
    parts = key.split(".")
    for part in parts[:-1]:
        if not isinstance(data.get(part), dict):
            data[part] = {}
        data = data[part]
    data[parts[-1]] = value


def repair_numeric_settings(data):
    """Explicit repair: keep all valid values, report every replacement."""
    repaired = copy.deepcopy(data)
    changes = []
    for key, spec in NUMERIC_FIELDS.items():
        old = lookup(data, key)
        try:
            validated_number(key, old)
        except ValueError:
            put(repaired, key, spec["default"])
            changes.append({"key": key, "old": old, "new": spec["default"]})
    for minimum, maximum in (
        ("motion.pwm_min_duty", "motion.pwm_max_duty"),
        ("autonomy.min_interval_s", "autonomy.max_interval_s"),
        ("slam.min_range_cm", "slam.max_range_cm"),
    ):
        if lookup(repaired, minimum) > lookup(repaired, maximum):
            old = lookup(repaired, minimum)
            put(repaired, minimum, lookup(repaired, maximum))
            changes.append(
                {"key": minimum, "old": old, "new": lookup(repaired, maximum)}
            )
    budget = lookup(repaired, "tof.timing_budget_ms")
    if budget not in (20, 33, 50, 100, 200, 500) and (
        not (budget == 15 and lookup(repaired, "tof.distance_mode") == 1)
    ):
        put(repaired, "tof.timing_budget_ms", 50)
        changes.append({"key": "tof.timing_budget_ms", "old": budget, "new": 50})
    period = lookup(repaired, "tof.intermeasurement_ms")
    if period and period < lookup(repaired, "tof.timing_budget_ms"):
        put(repaired, "tof.intermeasurement_ms", 0)
        changes.append({"key": "tof.intermeasurement_ms", "old": period, "new": 0})
    return (repaired, changes)


def validate_combinations(data: dict, updates, fallback=None) -> None:
    def value(key):
        result = lookup(
            data, key, default=fallback(key) if fallback else SPECS[key]["default"]
        )
        return validated_number(key, result) if key in NUMERIC_FIELDS else result

    pairs = (
        ("motion.pwm_min_duty", "motion.pwm_max_duty"),
        ("autonomy.min_interval_s", "autonomy.max_interval_s"),
        ("slam.min_range_cm", "slam.max_range_cm"),
    )
    for minimum, maximum in pairs:
        if {minimum, maximum}.intersection(updates) and value(minimum) > value(maximum):
            raise ValueError(f"{minimum} must not exceed {maximum}")
    if {"tof.intermeasurement_ms", "tof.timing_budget_ms"}.intersection(updates):
        if value("tof.intermeasurement_ms") and value(
            "tof.intermeasurement_ms"
        ) < value("tof.timing_budget_ms"):
            raise ValueError(
                "ToF intermeasurement period must be at least its timing budget"
            )
    if {"tof.distance_mode", "tof.timing_budget_ms"}.intersection(updates):
        mode = value("tof.distance_mode")
        budgets = (
            (15, 20, 33, 50, 100, 200, 500)
            if mode == 1
            else (20, 33, 50, 100, 200, 500)
        )
        if mode not in (1, 2) or value("tof.timing_budget_ms") not in budgets:
            raise ValueError(f"ToF timing budget must be one of {budgets}")
