from __future__ import annotations
import re
from ..vision_skills import person_facing_request

class MotionDomain:
    def _can_run_slam_motion(self) -> tuple[bool, str]:
        if self._filming_mode:
            return False, "filming_mode"
        if self.focus_timer.status().get("active"):
            return False, "focus_timer_active"
        if self._voice_processing:
            return False, "voice_processing"
        voice_state = str((self.microphone.health() or {}).get("voice_state") or "")
        if voice_state == "recording_command":
            return False, "voice_recording"
        return True, ""

    def slam_start_scan(self) -> dict:
        return self.slam.start_scan()

    def slam_start_explore(self, max_steps: int | None = None) -> dict:
        return self.slam.start_explore(max_steps=max_steps)

    def slam_set_localization(self, enabled: bool) -> dict:
        return self.slam.set_localization_enabled(enabled)

    def slam_stop(self) -> dict:
        return self.slam.stop()

    def slam_reset(self) -> dict:
        return self.slam.reset()

    def slam_save(self) -> dict:
        return self.slam.save()

    def slam_status(self) -> dict:
        return self.slam.status()

    def slam_map(self) -> dict:
        return self.slam.map_data()

    def slam_sessions(self) -> dict:
        return self.slam.sessions()

    def _sanitize_llm_motion(self, action: dict, user_text: str = "") -> dict:
        if not isinstance(action, dict):
            return action
        normalized = dict(action)
        explicit = self._looks_like_explicit_motion_request(user_text)
        if isinstance(normalized.get("sequence"), list):
            sanitized_steps = []
            for step in normalized["sequence"]:
                if not isinstance(step, dict):
                    sanitized_steps.append(step)
                    continue
                clean_step = dict(step)
                clean_drive = self._sanitize_llm_drive_payload(clean_step.get("drive"), explicit=explicit)
                if clean_drive is None:
                    clean_step.pop("drive", None)
                else:
                    clean_step["drive"] = clean_drive
                sanitized_steps.append(clean_step)
            normalized["sequence"] = sanitized_steps
        if "drive" in normalized:
            clean_drive = self._sanitize_llm_drive_payload(normalized.get("drive"), explicit=explicit)
            if clean_drive is None:
                normalized.pop("drive", None)
                self._publish_event(
                    "tool.failed",
                    {"tool": "drive_sanitizer", "reason": "removed_non_explicit_llm_drive", "user_text": user_text},
                )
            else:
                normalized["drive"] = clean_drive
        return normalized

    def _sanitize_llm_drive_payload(self, drive, explicit: bool) -> dict | None:
        if not isinstance(drive, dict):
            return None
        mode = str(drive.get("mode") or "").strip().lower()
        direction = str(drive.get("direction") or "forward").strip().lower()
        if mode == "stop" or (not mode and direction == "stop"):
            return {"mode": "stop"}
        try:
            speed = int(drive.get("speed", 100))
        except Exception:
            speed = 100
        try:
            duration = float(drive.get("duration", 1.0))
        except Exception:
            duration = 1.0
        if explicit:
            cleaned = {
                "mode": mode,
                "direction": direction,
                "speed": max(0, min(100, speed)),
                "duration": max(0.05, min(3.0, duration)),
            }
            for key, limit in (
                ("distance_cm", 100.0),
                ("angle_deg", 360.0),
                ("radius_cm", 50.0),
                ("amplitude_cm", 25.0),
                ("cycles", 8.0),
            ):
                if key in drive:
                    value = self._optional_float(drive.get(key))
                    if value is not None:
                        cleaned[key] = max(-limit, min(limit, value))
            if drive.get("ignore_obstacles") or drive.get("obstacle_override"):
                cleaned["ignore_obstacles"] = True
            return {key: value for key, value in cleaned.items() if value not in ("", None)}
        expressive_directions = {
            "left",
            "right",
            "forward_slight_left",
            "forward_slight_right",
            "slight_left",
            "slight_right",
            "arc_left",
            "arc_right",
            "sine",
            "sine_forward",
            "wiggle",
        }
        if direction not in expressive_directions and mode not in expressive_directions:
            return None
        if direction == "slight_left":
            direction = "forward_slight_left"
        elif direction == "slight_right":
            direction = "forward_slight_right"
        cleaned = {
            "mode": mode,
            "direction": direction,
            "speed": max(20, min(100, speed)),
            "duration": max(0.1, min(0.5, duration)),
        }
        if mode in {"arc_left", "arc_right", "sine", "sine_forward", "wiggle"}:
            cleaned["duration"] = max(0.1, min(2.0, duration))
        for key, limit in (("angle_deg", 90.0), ("radius_cm", 20.0), ("cycles", 3.0)):
            if key in drive:
                value = self._optional_float(drive.get(key))
                if value is not None:
                    cleaned[key] = max(-limit, min(limit, value))
        return {key: value for key, value in cleaned.items() if value not in ("", None)}

    def _looks_like_explicit_motion_request(self, text: str) -> bool:
        lowered = (text or "").strip().lower()
        if not lowered:
            return False
        if person_facing_request(lowered):return True
        if re.search(r'\b(?:do|perform|make|give me)\s+(?:a\s+)?(?:full\s+)?(?:360|three[ -]sixty)\b',lowered):
            return True
        return bool(
            re.search(
                r"\b(drive|move|roll|turn|spin|dance|wiggle|wave|sine|sway|arc|go forward|go backward|back up|left|right|forward|backward|come here|run around)\b",
                lowered,
            )
        )
