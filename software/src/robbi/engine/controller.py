from __future__ import annotations

import logging
import threading
import time
from robbi.resources import ExecutionResources
from robbi.orchestration import Orchestrator
from robbi.services.productivity import ProductivityService, ResearchOptions
from robbi.services.conversation import ConversationService
from robbi.services.conversation_feed import ConversationFeed
from robbi.services.background_work import BackgroundTaskService
from robbi.dynamics import DynamicBehavior
from .diagnostics import controller_diagnostics

from .action_executor import ActionExecutorMixin
from robbi.configuration.settings import ROOT, HEARTBEAT_FILE, REMINDERS_FILE, SOUL_FILE, TOOLS_FILE, STATE_DIR, settings, settings_ui_snapshot, update_settings_from_payload
from .presentation_pipeline import PresentationPipelineMixin


from .domains.voice import VoiceDomain
from .domains.motion import MotionDomain
from .domains.apps import AppsDomain
from .domains.productivity import ProductivityDomain
from .domains.telegram import TelegramDomain
from .domains.presentation import PresentationDomain
from .domains.conversation import ConversationDomain
from .domains.background_work import BackgroundWorkDomain
from .domains.engagement import EngagementDomain
from .domains.tools import ToolsDomain
from .domains.vision import VisionDomain
from .lifecycle import LifecycleMixin

class RobotController(ActionExecutorMixin, PresentationPipelineMixin, LifecycleMixin, VoiceDomain, MotionDomain, AppsDomain, ProductivityDomain, TelegramDomain, PresentationDomain, ConversationDomain, BackgroundWorkDomain, EngagementDomain, ToolsDomain, VisionDomain):
    def __init__(self, *, diagnostic_intervals=None, feature_task_limit=16, recording=None) -> None:
        self.recording = recording
        self.conversation_feed = ConversationFeed(limit=max(10,getattr(settings,'conversation_history_messages',100)//2),path=STATE_DIR/'conversation-feed.json')
        self._log = logging.getLogger(__name__)
        self._init_lifecycle()
        self.orchestrator = Orchestrator()
        from robbi.configuration.settings import ensure_directories, CONFIG_ERRORS
        ensure_directories()
        if CONFIG_ERRORS:
            self._record_health("configuration", "degraded", "; ".join(CONFIG_ERRORS))
        self._running = False
        self.assets = self._component("assets", lambda: self._load("assets", "AssetRegistry")())
        self._call_optional("assets", "scan")
        self.apps = self._component("apps", lambda: self._load("apps", "AppRegistry")(
            bundled_root=ROOT / "templates" / "workflows"))
        # Runtime supplies the independent API-2 host. Legacy YAML apps also
        # work when this optional platform cannot initialize.
        self.app_host = None
        self.day_cycle = self._component("day_cycle", lambda: self._load("autonomy", "make_day_cycle")())
        self.autonomy_policy = self._component("autonomy_policy", lambda: self._load("autonomy", "make_autonomy_policy")(self.day_cycle))
        self.attention = self._component("attention", lambda: self._load("autonomy", "make_attention")())
        self.behavior = self._component("behavior", lambda: self._load("behavior", "BehaviorEngine")(day_cycle=self.day_cycle))
        self.camera = self._component("camera", lambda: self._load("camera", "CameraService")())
        self.camera.recording = recording
        self.media = self._component("media", lambda: self._bind_outputs(self._load("media", "MediaManager")()))
        self.microphone = self._component("microphone", lambda: self._load("microphone", "MicrophoneService")())
        self.motion = self._component("motion", lambda: self._bind_outputs(self._load("motion", "MotionManager")()))
        self.tof = self._component("tof", lambda: self._load("tof", "ToFService")())
        self.imu = self._component("imu", lambda: self._load("imu", "IMUService")(can_calibrate=lambda: not self.motion.is_running()))
        self._call_optional("motion", "set_obstacle_distance_getter", self.tof.distance_cm)
        self._call_optional("motion", "set_heading_getter", lambda: self.imu.heading_sample())
        from robbi.navigation.service import NavigationService
        self.slam = self._component("slam", lambda: NavigationService(self, STATE_DIR / 'navigation'))
        if self._component_health['slam']['state'] == 'ready':
            self.motion.motion_observer = self.slam.observe_motion
        self.llm = self._component("llm", lambda: self._load("llm", "LLMClient")(self.assets, self.behavior, recording=recording))
        self.memory = self._component("memory", lambda: self._load("memory", "MemoryStore")())
        self.productivity = self._component("productivity", lambda: self._load("productivity", "ProductivityStore")())
        self.onboarding = self._component("onboarding", lambda: self._load("onboarding", "OnboardingStore")())
        self.kids = self._component("kids", lambda: self._load("kids", "KidsEngine")())
        self.events = self._component("events", lambda: self._load("events", "EventBus")())
        self.tools = self._component("tools", lambda: self._load("tools", "ToolRegistry")())
        self.web_search = self._component("web_search", lambda: self._load("web_search", "WebSearchClient")())
        self.daily_loop = self._component("daily_loop", lambda: self._load("daily_loop", "DailyLoop")(day_cycle=self.day_cycle))
        self.telegram = self._component("telegram", lambda: self._load("telegram_service", "TelegramService")(
            text_handler=self._handle_telegram_text,
            voice_handler=self._handle_telegram_voice,
            status_handler=self._telegram_status_text,
            photo_handler=self._telegram_capture_photo,
            photo_question_handler=self._telegram_photo_question,
            reminder_handler=self._telegram_reminder_command,
            memory_handler=self._telegram_memory_search,
            todo_handler=self._telegram_todo_command,
            note_handler=self._telegram_note_command,
            notes_handler=self._telegram_notes_command,
            shopping_handler=self._telegram_shopping_command,
            brief_handler=self._telegram_brief_command,
            remember_handler=self._telegram_remember_command,
            forget_handler=self._telegram_forget_command,
            review_handler=self._telegram_review_command,
        ))
        self.initiative = self._component("initiative", lambda: self._load("autonomy", "InitiativeEngine")())
        self.retention = self._component("retention", lambda: self._load("retention", "RetentionManager")())
        self.reminders = self._component("reminders", lambda: self._load("reminder", "ReminderManager")(
            REMINDERS_FILE,
            due_callback=self._handle_due_reminder,
            can_present_callback=self._can_present_reminder,
        ))
        self.focus_timer = self._component("focus_timer", lambda: self._load("focus_timer", "FocusTimerManager")(
            show_face_callback=self._show_focus_timer_face,
            finished_callback=self._handle_focus_timer_finished,
            can_present_callback=self._can_show_focus_timer,
        ))
        self.execution = ExecutionResources(feature_task_limit)
        self._voice_executor = self.execution.lane("voice")
        self._maintenance_executor = self.execution.lane("maintenance")
        self._audio_executor = self.execution.lane("speech")
        self._overlay_executor = self.execution.lane("overlay")
        self._effect_executor = self.execution.lane("effects")
        self._presentation_executor = self.execution.lane("presentation")
        self._tts_prefetch_executor = self.execution.lane("tts-prefetch")
        self._memory_extractor_executor = self.execution.lane("memory-extract")
        self._research_executor = self.execution.lane("research")
        self.productivity_service = ProductivityService(
            self.productivity,
            search=self.web_search,
            scheduler=self._research_executor,
            publish_event=self._publish_event,
            research_options=lambda: ResearchOptions(settings.web_search_max_results, settings.web_search_search_depth),
        )
        self._llm_executor = self.execution.lane("llm-live")
        self.conversation_service = ConversationService(
            memory=self.memory, provider=self.llm,
            capture_image=lambda **kwargs: self.camera.get_llm_image_bytes(**kwargs),
            scheduler=self._llm_executor, options=self._conversation_options,
            background_scheduler=self.execution.lane("llm-background"),
            daily_state=self.today_state, publish_event=self._publish_event,
        )
        self.background_work = self._component("background_work", lambda: BackgroundTaskService(
            STATE_DIR / "background-work.sqlite3", scheduler=self.execution.lane("background-work"),
            options=self._background_work_options, publish_event=self._publish_event,
        ))
        self.animations = self._component("animations", lambda: self._load("animations", "AnimationPlayer")(self.assets, self.execute_action))
        from robbi.configuration.settings import ASSETS_DIR
        self.dynamics = self._component('dynamics', lambda: DynamicBehavior(self, ASSETS_DIR,
            pickup_reactions=getattr(settings, 'pickup_reactions_enabled', True)))
        self.motion.handling_interlock = lambda: bool(getattr(self.dynamics, 'motion_blocked', lambda: False)())
        self._ambient_thread: threading.Thread | None = None
        self._warmup_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_start_lock = threading.Lock()
        self._attention_wake = threading.Event()
        self._tts_keepwarm_thread: threading.Thread | None = None
        self._running = False
        self._filming_mode = False
        self._voice_processing = False
        self._last_activity_ts = time.time()
        self._last_proactive_ts = 0.0
        self._last_tts_warm_at = 0.0
        self._tts_warm_inflight = False
        self._tts_warm_lock = threading.Lock()
        self._conversation_lock = threading.Lock()
        self._conversation_turns = 0
        self._soul_text = self._ensure_prompt_file(
            SOUL_FILE,
            self._default_soul_text(),
        )
        self._heartbeat_text = self._ensure_prompt_file(
            HEARTBEAT_FILE,
            self._default_heartbeat_text(),
        )
        self._tools_text = self._ensure_prompt_file(
            TOOLS_FILE,
            self._default_tools_text(),
        )
        self._state_lock = threading.Lock()
        self._presentation_state = {
            "face": "neutral.png",
            "thinking": False,
            "playing_video": None,
            "last_sound": None,
            "last_animation": None,
            "last_command": None,
            "last_speak": None,
        }
        self._last_pipeline_metrics: dict = {}
        self._last_pipeline_request_id: int | None = None
        self._warmup_state = {
            "enabled": settings.startup_warmup_enabled,
            "running": False,
            "completed_at": None,
            "results": [],
            "ready_sound_played": False,
        }
        self._heartbeat_state = {
            "enabled": settings.autonomy_enabled and settings.heartbeat_enabled,
            "running": False,
            "last_run_at": None,
            "next_run_at": None,
            "last_decision": None,
            "last_reason": None,
            "last_request_id": None,
        }

        from robbi.live_awareness.robot import make_live_awareness
        self.live_awareness = self._component("live_awareness", lambda: make_live_awareness(self))
        self._diagnostics = controller_diagnostics(self, settings, **(diagnostic_intervals or {}))

    def _bind_outputs(self, component):
        component.bind_orchestrator(self.orchestrator)
        return component

    def start(self) -> None:
        self._running = True
        self._lifecycle_stop.clear()
        for name in ("events", "behavior", "retention", "camera", "media", "motion", "tof", "imu", "reminders", "telegram"):
            self._start_component(name)
        self._call_optional("apps", "scan")
        self._configure_microphone()
        self._start_component("microphone")
        self._recovery_thread = threading.Thread(target=self._recovery_loop, daemon=True, name="component-recovery")
        self._recovery_thread.start()
        for attr, target, name in (("_ambient_thread", self._ambient_loop, "ambient"),
                                  ("_tts_keepwarm_thread", self._tts_keepwarm_loop, "tts-keepwarm")):
            thread = threading.Thread(target=target, daemon=True, name=name)
            setattr(self, attr, thread)
            thread.start()
        self._start_heartbeat_once()
        self._call_optional('dynamics', 'start')
        if self._filming_mode:
            return  # A restored filming session stays quiet; no startup cue.
        try:
            self._show_startup_face()
            if settings.startup_warmup_enabled:
                self._warmup_thread = threading.Thread(target=self._startup_warmup, daemon=True, name="startup-warmup")
                self._warmup_thread.start()
            else:
                self._finish_startup_presentation()
        except Exception as exc:
            self._record_health("presentation", "degraded", str(exc))

    def stop(self) -> None:
        self._running = False
        self._lifecycle_stop.set()
        self._attention_wake.set()
        self._call_optional("live_awareness", "shutdown")
        self._call_optional('dynamics', 'stop')
        self.cancel_actions()
        self._call_optional("background_work", "shutdown")
        for name, method in (("slam", "stop"), ("motion", "shutdown"), ("microphone", "stop"),
                             ("camera", "stop"), ("media", "shutdown"), ("tof", "shutdown"), ("imu", "shutdown"),
                             ("retention", "stop"), ("behavior", "stop"), ("reminders", "shutdown"),
                             ("telegram", "stop"), ("focus_timer", "shutdown"), ("llm", "shutdown"), ("events", "stop")):
            self._call_optional(name, method)
        self.execution.shutdown()

    def _start_heartbeat_once(self):
        if not self._running or not settings.autonomy_enabled or not settings.heartbeat_enabled:
            return
        with self._heartbeat_start_lock:
            if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
                self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True, name="heartbeat")
                self._heartbeat_thread.start()

    def status(self, refresh_assets: bool = False, *, force_refresh: bool = False) -> dict:
        assets = self.refresh_asset_catalog() if refresh_assets else None
        snapshot = self._diagnostics.sample(force=force_refresh)
        if assets is not None:
            snapshot["assets"] = assets
        return snapshot

    def refresh_asset_catalog(self) -> dict:
        self.assets.refresh(force=True)
        self._diagnostics.invalidate("assets")
        return {category: self.assets.names(category) for category in
                ("faces", "sounds", "videos", "animations", "filming_sequences")}

    def diagnostics_metrics(self) -> dict:
        return self._diagnostics.metrics()

    def resource_metrics(self) -> dict:
        return {**self.execution.snapshot(), "outputs": self.orchestrator.snapshot()}

    def settings_snapshot(self, discover: bool = False) -> dict:
        snapshot = settings_ui_snapshot()
        if not discover:
            return snapshot
        discovered = self.llm.discover_local_runtime()
        options = snapshot.setdefault("options", {})
        models = options.setdefault("models", {})
        if discovered["llm_models"]:
            models["local"] = discovered["llm_models"]
        stt_models = options.setdefault("stt_models", {})
        if discovered["stt_models"]:
            stt_models["local"] = discovered["stt_models"]
        tts_models = options.setdefault("tts_models", {})
        if discovered["tts_models"]:
            tts_models["local"] = discovered["tts_models"]
        tts_voices = options.setdefault("tts_voices", {})
        if discovered["tts_voices"]:
            tts_voices["local"] = discovered["tts_voices"]
        snapshot["discovered"] = {"local": discovered}
        return snapshot

    def apply_settings(self, payload: dict) -> dict:
        snapshot = update_settings_from_payload(payload)
        groups = set(payload)
        if groups.intersection({"llm", "stt", "tts", "providers"}):
            self.llm.reload_runtime_settings()
        if groups.intersection({"voice", "conversation", "stt", "providers"}):
            self.microphone.reload_runtime_settings()
        if "motion" in groups:
            self.motion.reload_runtime_settings()
        if "tof" in groups:
            self.tof.reload_runtime_settings()
            self._record_health("tof", "ready" if settings.tof_enabled else "disabled", "")
        if "imu" in groups:
            self.imu.reload_runtime_settings()
            self._record_health("imu", "ready" if settings.imu_enabled else "disabled", "")
        if "slam" in groups:
            self.slam.reload_runtime_settings()
        if "events" in groups:
            if settings.events_enabled:
                self.events.start()
            else:
                self.events.stop()
        if groups.intersection({"autonomy", "heartbeat"}):
            self._start_heartbeat_once()
        if "telegram" in groups:
            self.telegram.restart()
        self._set_presentation(last_command="settings:updated")
        self._publish_event("settings.updated", {"keys": sorted(self._flatten_payload_keys(payload))})
        self._diagnostics.invalidate()
        return {
            "settings": snapshot,
            "status": self.status(),
        }

    def _flatten_payload_keys(self, payload: dict, prefix: str = "") -> list[str]:
        keys: list[str] = []
        if not isinstance(payload, dict):
            return keys
        for key, value in payload.items():
            full = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                keys.extend(self._flatten_payload_keys(value, full))
            else:
                keys.append(full)
        return keys
