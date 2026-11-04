from __future__ import annotations

import json
import math
import os
import tempfile
import statistics
import threading
import time
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from robbi.orchestration import AUTONOMY, Intent, current_intent, intent_scope

from robbi.configuration.settings import MAPS_DIR, settings
from .exploration import choose_target


DistanceGetter = Callable[[], float | None]
CanRunCallback = Callable[[], tuple[bool, str]]


class MappingHalted(RuntimeError):
    """An expected stop/interlock, not an internal mapping error."""


@dataclass
class Pose2D:
    x_cm: float = 0.0
    y_cm: float = 0.0
    theta_deg: float = 0.0
    confidence: float = 1.0
    source: str = "origin"


@dataclass
class RangeSample:
    angle_deg: float
    distance_cm: float | None
    valid: bool


@dataclass
class RangeScan:
    ts: float
    pose: Pose2D
    samples: list[RangeSample] = field(default_factory=list)
    match: dict | None = None


class OccupancyGrid:
    def __init__(self, resolution_cm: float = 5.0, max_cells: int = 50000) -> None:
        self.resolution_cm = max(1.0, float(resolution_cm))
        self.max_cells = max(100, int(max_cells))
        self.capacity_reached = False
        self.cells: dict[tuple[int, int], float] = {}

    def reset(self) -> None:
        self.cells.clear()
        self.capacity_reached = False

    def world_to_cell(self, x_cm: float, y_cm: float) -> tuple[int, int]:
        return int(round(x_cm / self.resolution_cm)), int(round(y_cm / self.resolution_cm))

    def cell_to_world(self, ix: int, iy: int) -> tuple[float, float]:
        return ix * self.resolution_cm, iy * self.resolution_cm

    def update_ray(self, pose: Pose2D, angle_deg: float, distance_cm: float | None, valid: bool, max_range_cm: float) -> None:
        if not valid or distance_cm is None or not math.isfinite(distance_cm) or distance_cm <= 0:
            return
        distance = float(distance_cm if distance_cm is not None else max_range_cm)
        distance = max(0.0, min(distance, max_range_cm))
        theta = math.radians(angle_deg)
        step = max(1.0, self.resolution_cm * 0.5)
        free_until = max(0.0, distance - self.resolution_cm)
        travelled = 0.0
        while travelled <= free_until:
            x = pose.x_cm + math.cos(theta) * travelled
            y = pose.y_cm + math.sin(theta) * travelled
            self._add(self.world_to_cell(x, y), -0.35)
            travelled += step
        if valid and distance < max_range_cm:
            x = pose.x_cm + math.cos(theta) * distance
            y = pose.y_cm + math.sin(theta) * distance
            self._add(self.world_to_cell(x, y), 0.9)

    def score_scan(self, pose: Pose2D, samples: list[RangeSample], max_range_cm: float, scan_origin_theta_deg: float = 0.0) -> float:
        score = 0.0
        used = 0
        for sample in samples:
            if not sample.valid or sample.distance_cm is None or sample.distance_cm >= max_range_cm:
                continue
            relative_angle = ((sample.angle_deg - scan_origin_theta_deg + 180.0) % 360.0) - 180.0
            theta = math.radians(pose.theta_deg + relative_angle)
            x = pose.x_cm + math.cos(theta) * sample.distance_cm
            y = pose.y_cm + math.sin(theta) * sample.distance_cm
            score += self.cells.get(self.world_to_cell(x, y), 0.0)
            used += 1
        return score / max(1, used)

    def to_dict(self) -> dict:
        cells = []
        for (ix, iy), value in self.cells.items():
            if value >= 0.35:
                state = "occupied"
            elif value <= -0.35:
                state = "free"
            else:
                state = "unknown"
            cells.append({"x": ix, "y": iy, "v": round(value, 2), "state": state})
        bounds = self.bounds()
        return {"resolution_cm": self.resolution_cm, "bounds": bounds, "cells": cells,
                "capacity_reached": self.capacity_reached, "max_cells": self.max_cells}

    def bounds(self) -> dict:
        if not self.cells:
            return {"min_x": -10, "max_x": 10, "min_y": -10, "max_y": 10}
        xs = [cell[0] for cell in self.cells]
        ys = [cell[1] for cell in self.cells]
        return {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)}

    def _add(self, cell: tuple[int, int], delta: float) -> None:
        if cell not in self.cells and len(self.cells) >= self.max_cells:
            self.capacity_reached = True
            raise MappingHalted("Map capacity reached; save and start a new session")
        self.cells[cell] = max(-3.0, min(3.0, self.cells.get(cell, 0.0) + delta))


class SlamService:
    def __init__(self, motion, distance_getter: DistanceGetter, can_run_callback: CanRunCallback, range_snapshot_getter=None) -> None:
        self.motion = motion
        self._distance_getter = distance_getter
        self._range_snapshot_getter = range_snapshot_getter
        self._can_run_callback = can_run_callback
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker_intent = None
        self._run_limits = threading.local()
        self._pose = Pose2D()
        self._grid = OccupancyGrid(settings.slam_grid_resolution_cm)
        self._preview_grid = OccupancyGrid(settings.slam_grid_resolution_cm)
        self._scans: list[RangeScan] = []
        self._pose_history: list[dict] = [asdict(self._pose)]
        self._visited: list[tuple[float, float]] = []
        self._status: dict = {
            "enabled": settings.slam_enabled,
            "localization_enabled": False,
            "active": False,
            "job": "idle",
            "progress": 0.0,
            "last_error": "",
            "last_stop_reason": "",
            "scan_count": 0,
            "saved_session": "",
            "phase": "idle",
            "explore_steps": 0,
            "continuous_exploration": False,
            "leg_target_cm": 0.0,
            "leg_travelled_cm": 0.0,
            "scan_samples_collected": 0,
            "scan_samples_target": 0,
            "scan_preview": False,
        }

    def reload_runtime_settings(self) -> None:
        with self._lock:
            self._status["enabled"] = settings.slam_enabled

    def is_running(self):
        """Cheap orchestration check without rebuilding map diagnostics."""
        with self._lock:
            return bool(self._status["active"])

    def status(self) -> dict:
        with self._lock:
            status = dict(self._status)
            pose = asdict(self._pose)
            latest_scan = self._scan_summary(self._scans[-1]) if self._scans else None
            grid_bounds = self._grid.bounds()
        status.update(
            {
                "pose": pose,
                "latest_scan": latest_scan,
                "map_bounds": grid_bounds,
                "cell_count": len(self._grid.cells),
                "map_capacity_reached": self._grid.capacity_reached,
                "max_map_cells": self._grid.max_cells,
                "config": {
                    "grid_resolution_cm": settings.slam_grid_resolution_cm,
                    "scan_step_deg": settings.slam_scan_step_deg,
                    "samples_per_angle": settings.slam_samples_per_angle,
                    "max_range_cm": settings.slam_max_range_cm,
                    "explore_step_cm": settings.slam_explore_step_cm,
                    "max_explore_steps": settings.slam_max_explore_steps,
                    "min_pose_confidence": settings.slam_min_pose_confidence,
                    "scan_match_enabled": settings.slam_scan_match_enabled,
                    "scan_mode": settings.slam_scan_mode,
                    "continuous_scan_hz": settings.slam_continuous_scan_hz,
                    "continuous_turn_speed": settings.slam_continuous_turn_speed,
                    "min_scan_samples": settings.slam_min_scan_samples,
                },
            }
        )
        return status

    def map_data(self) -> dict:
        with self._lock:
            # In-progress rays are visible but are NOT fed into scan matching.
            display_grid = OccupancyGrid(self._grid.resolution_cm)
            display_grid.cells = {**self._grid.cells, **self._preview_grid.cells}
            return {
                "pose": asdict(self._pose),
                "grid": display_grid.to_dict(),
                "scans": [self._scan_summary(scan) for scan in self._scans[-5:]],
                "status": dict(self._status),
            }

    def sessions(self) -> dict:
        MAPS_DIR.mkdir(parents=True, exist_ok=True)
        sessions = []
        for path in sorted(MAPS_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            sessions.append({"name": path.name, "path": str(path), "modified_ts": path.stat().st_mtime})
        return {"sessions": sessions}

    def start_scan(self) -> dict:
        return self._start_worker("scan", self._scan_worker)

    def start_explore(self, max_steps: int | None = None, *, max_distance_cm=None, timeout_s=None) -> dict:
        if max_steps is not None and not 0 <= max_steps <= 100:
            return {"ok": False, "error": "max_steps must be 0 (until stopped) or 1-100"}
        if self._fresh_distance() is None:
            return {"ok": False, "error": "tof_unavailable", "status": self.status()}
        if max_distance_cm is not None and (not math.isfinite(float(max_distance_cm)) or not 1 <= max_distance_cm <= 100):
            return {"ok": False, "error": "Exploration travel budget must be 1-100 cm"}
        if timeout_s is not None and (not math.isfinite(float(timeout_s)) or not 5 <= timeout_s <= 180):
            return {"ok": False, "error": "Exploration deadline must be 5-180 seconds"}
        def explore():
            self._run_limits.value = {"distance_cm": max_distance_cm, "travelled_cm": 0,
                                      "deadline": time.monotonic() + timeout_s if timeout_s is not None else None}
            try:
                self._explore_worker(max_steps=max_steps)
            finally:
                self._run_limits.value = None
        return self._start_worker("explore", explore)

    def set_localization_enabled(self, enabled: bool) -> dict:
        if enabled and not settings.slam_enabled:
            return {"ok": False, "error": "SLAM is disabled", "status": self.status()}
        if not enabled:
            with self._lock:
                self._status["localization_enabled"] = False
            self.stop()
            return {"ok": True, "status": self.status()}
        with self._lock:
            self._status["localization_enabled"] = True
            self._status["last_stop_reason"] = ""
        if not self._scans:
            result = self.start_scan()
            if not result.get("ok") and result.get("error") == "mapping job already running":
                return {"ok": True, "status": self.status()}
            return result
        return {"ok": True, "status": self.status()}

    def track_drive_action(self, drive: dict, future: Future | None) -> None:
        with self._lock:
            should_track = bool(self._status.get("localization_enabled")) and not bool(self._status.get("active"))
        if not should_track or future is None:
            return
        parent = current_intent()
        intent = Intent("mapping:localization", AUTONOMY,
                        cancelled=parent.is_set if parent is not None else None)
        def track():
            with intent_scope(intent):
                if not intent.is_set():
                    self._track_drive_worker(dict(drive or {}), future)
        threading.Thread(target=track, daemon=True, name="slam-track-drive").start()

    def stop(self) -> dict:
        self._stop_event.set()
        with self._lock:
            # A start racing the first set() may have cleared it under this lock.
            self._stop_event.set()
            owner = self._worker_intent
            if owner is not None:
                owner.cancel("Mapping stopped")
            self._status.update(last_stop_reason="stop_requested", phase="stopping" if self._status.get("active") else "idle")
        if owner is not None or getattr(self.motion, "_orchestrator", None) is None:
            self._stop_motion(owner)
        return self.status()

    def _stop_motion(self, owner=None):
        if getattr(self.motion, "_orchestrator", None) is None:
            return self.motion.stop()
        owner = owner or current_intent() or self._worker_intent
        if owner is not None:
            return self.motion.stop(owner=owner)

    def reset(self) -> dict:
        self.stop()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.0)
            if thread.is_alive():
                return {"ok": False, "error": "mapping is stopping; retry reset", "status": self.status()}
        with self._lock:
            self._pose = Pose2D()
            self._grid = OccupancyGrid(settings.slam_grid_resolution_cm)
            self._preview_grid = OccupancyGrid(settings.slam_grid_resolution_cm)
            self._scans = []
            self._visited = []
            self._pose_history = [asdict(self._pose)]
            self._status.update({"job": "idle", "phase": "idle", "progress": 0.0, "last_error": "", "last_stop_reason": "", "scan_count": 0, "saved_session": "",
                                 "explore_steps": 0, "leg_target_cm": 0, "leg_travelled_cm": 0, "continuous_exploration": False,
                                 "scan_samples_collected": 0, "scan_samples_target": 0, "scan_preview": False})
        return self.status()

    def save(self) -> dict:
        MAPS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = MAPS_DIR / f"map_{stamp}.json"
        with self._lock:
            data = {
                "saved_at": time.time(),
                "pose": asdict(self._pose),
                "pose_history": self._pose_history,
                "scans": [self._scan_to_dict(scan) for scan in self._scans],
                "grid": self._grid.to_dict(),
                "status": dict(self._status),
            }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=MAPS_DIR,
                                             prefix=".map-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, separators=(",", ":"), allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._set_status(saved_session=path.name)
        return {"ok": True, "path": str(path), "name": path.name}

    def _start_worker(self, job: str, target) -> dict:
        if not settings.slam_enabled:
            return {"ok": False, "error": "SLAM is disabled", "status": self.status()}
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"ok": False, "error": "mapping job already running", "status": self.status()}
            motion_status = self.motion.status()
            if not motion_status.get("available") or not motion_status.get("encoder_enabled"):
                return {"ok": False, "error": "mapping requires working encoder-controlled motors", "status": self.status()}
            if motion_status.get("running"):
                return {"ok": False, "error": "motion is already running", "status": self.status()}
            ok, reason = self._can_run_callback()
            if not ok:
                return {"ok": False, "error": reason, "status": self.status()}
            self._stop_event.clear()
            parent = current_intent()
            intent = Intent(f"mapping:{job}", AUTONOMY, cancelled=lambda: (
                self._stop_event.is_set() or (parent is not None and parent.is_set())))
            self._worker_intent = intent
            self._status.update({"active": True, "job": job, "progress": 0.0, "last_error": "", "last_stop_reason": "", "scan_warning": "", "continuous_exploration": False})
            def run():
                with intent_scope(intent):
                    target()
            self._thread = threading.Thread(target=run, daemon=True, name=f"slam-{job}")
            self._thread.start()
        return {"ok": True, "status": self.status()}

    def _scan_worker(self) -> None:
        try:
            self._set_status(phase="scanning")
            self._perform_scan(update_map=True)
            self._set_status(progress=1.0)
        except MappingHalted as exc:
            self._set_status(last_stop_reason=str(exc))
        except Exception as exc:
            self._set_status(last_error=str(exc), last_stop_reason="error")
        finally:
            self._stop_motion()
            self._set_status(job="idle", active=False, phase="idle")

    def _explore_worker(self, max_steps: int | None = None) -> None:
        try:
            steps = int(settings.slam_max_explore_steps if max_steps is None else max_steps)
            step_index = 0
            no_progress = 0
            self._visited = [(self._pose.x_cm, self._pose.y_cm)]
            self._set_status(continuous_exploration=steps == 0, explore_steps=0)
            while True:
                self._check_guard()
                self._set_status(phase="scanning", progress=0.0)
                scan = self._perform_scan(update_map=True)
                self._check_guard()
                # Also scan the final destination of a bounded exploration.
                if steps and step_index >= steps:
                    self._set_status(last_stop_reason="step_limit_reached")
                    break
                if self._pose.confidence < settings.slam_min_pose_confidence:
                    raise MappingHalted("low_confidence")
                target = self._choose_explore_target(scan)
                if target is None:
                    raise MappingHalted("no_safe_frontier")
                self._set_status(phase="turning", leg_target_cm=round(target.distance_cm, 1), leg_travelled_cm=0.0, last_stop_reason="")
                self._turn_to_heading(target.heading_deg)
                travelled = self._travel_leg(target.distance_cm)
                pose = self._copy_pose()
                self._visited.append((pose.x_cm, pose.y_cm))
                self._visited = self._visited[-100:]
                step_index += 1
                self._set_status(explore_steps=step_index)
                # A newly blocked path can be rescanned once, but never spin in
                # place forever when repeated replanning makes no progress.
                no_progress = no_progress + 1 if travelled < 3 else 0
                if no_progress >= 2:
                    raise MappingHalted("no_progress")
        except MappingHalted as exc:
            self._set_status(last_stop_reason=str(exc))
        except Exception as exc:
            self._set_status(last_error=str(exc), last_stop_reason="error")
        finally:
            self._stop_motion()
            self._set_status(job="idle", active=False, phase="idle")

    def _perform_scan(self, update_map: bool) -> RangeScan:
        if str(settings.slam_scan_mode or "").strip().lower() == "continuous":
            scan = self._perform_continuous_scan(update_map=False)
            # Repeated readings at the same heading are not extra coverage.
            bins = {round(s.angle_deg / 5) % 72 for s in scan.samples if s.valid}
            if len(bins) >= min(60, max(1, int(settings.slam_min_scan_samples))):
                return self._finish_scan(scan, scan.pose, update_map)
            self._set_status(scan_warning=f"continuous_scan_too_sparse:{len(bins)}; using stepped scan")
            return self._perform_stepped_scan(update_map=update_map)
        return self._perform_stepped_scan(update_map=update_map)

    def _perform_stepped_scan(self, update_map: bool) -> RangeScan:
        step = max(2.0, min(90.0, float(settings.slam_scan_step_deg)))
        count = max(1, int(round(360.0 / step)))
        step = 360.0 / count
        start_pose = self._copy_pose()
        scan = RangeScan(ts=time.time(), pose=start_pose, samples=[])
        self._begin_preview(count)
        for index in range(count):
            self._check_guard()
            if index:
                self._turn_to_heading(start_pose.theta_deg + index * step)
            angle = self._copy_pose().theta_deg
            sample = self._sample_at_angle(angle)
            scan.samples.append(sample)
            self._preview_sample(start_pose, sample, len(scan.samples))
            self._set_status(progress=(index + 1) / count)
        self._turn_to_heading(start_pose.theta_deg)
        self._check_guard()
        if sum(s.valid for s in scan.samples) < min(count, max(1, settings.slam_min_scan_samples)):
            raise MappingHalted("scan_too_sparse")
        return self._finish_scan(scan, start_pose, update_map)

    def _perform_continuous_scan(self, update_map: bool) -> RangeScan:
        start_pose = self._copy_pose()
        scan = RangeScan(ts=time.time(), pose=start_pose, samples=[])
        self._begin_preview(0)
        hz = max(1.0, min(25.0, float(settings.slam_continuous_scan_hz), float(settings.tof_poll_hz)))
        interval_s = 1.0 / hz
        timeout_s = max(6.0, float(settings.slam_continuous_scan_timeout_s))
        future = self.motion.move(
            mode="turn",
            direction="turn",
            angle_deg=360.0,
            speed=settings.slam_continuous_turn_speed,
            wait=False,
            ignore_obstacles=True,
            cancel_event=self._stop_event,
        )
        if future is None:
            raise MappingHalted("motion_unavailable")
        started = time.monotonic()
        next_sample = started
        last_progress = 0.0
        while not future.done():
            self._check_guard()
            now = time.monotonic()
            if now - started > timeout_s:
                self._stop_motion()
                raise MappingHalted("continuous_scan_timeout")
            progress_angle = self._turn_angle_from_live_counts()
            last_progress = max(last_progress, min(360.0, abs(progress_angle)))
            self._set_status(progress=min(0.99, last_progress / 360.0))
            with self._lock:
                self._pose.theta_deg = self._normalize_deg(start_pose.theta_deg + progress_angle)
            if now >= next_sample:
                angle = self._normalize_deg(start_pose.theta_deg + progress_angle)
                sample = self._sample_now(angle)
                scan.samples.append(sample)
                self._preview_sample(start_pose, sample, len(scan.samples))
                next_sample = now + interval_s
            self._stop_event.wait(min(0.03, interval_s * 0.5))
        completed = future.result(timeout=1.0)
        result = completed if isinstance(completed, dict) else self.motion.status().get("last_result", {})
        if result.get("ok") is False:
            self._set_status(last_stop_reason=result.get("reason", "motion_failed"))
            raise MappingHalted(result.get("reason", "scan_turn_failed"))
        final_angle = self._turn_angle_from_motion_result(result)
        if final_angle is None:
            final_angle = self._turn_angle_from_live_counts()
        with self._lock:
            self._pose.theta_deg = self._normalize_deg(start_pose.theta_deg + final_angle)
            self._pose.source = "odometry"
            self._pose.confidence = max(0.1, self._pose.confidence - 0.03)
            self._pose_history.append(asdict(self._pose))
        # Validate angular coverage before committing a continuous scan.
        return scan

    def _begin_preview(self, expected: int) -> None:
        with self._lock:
            self._preview_grid = OccupancyGrid(self._grid.resolution_cm)
            self._status.update(scan_samples_collected=0, scan_samples_target=expected, scan_preview=True)

    def _preview_sample(self, pose: Pose2D, sample: RangeSample, count: int) -> None:
        with self._lock:
            self._preview_grid.update_ray(pose, sample.angle_deg, sample.distance_cm, sample.valid, settings.slam_max_range_cm)
            self._status["scan_samples_collected"] = count

    def _finish_scan(self, scan: RangeScan, start_pose: Pose2D, update_map: bool) -> RangeScan:
        matched_pose = None
        if settings.slam_scan_match_enabled and self._scans and self._grid.cells:
            matched_pose = self._scan_match(scan)
            scan.match = matched_pose.get("match") if matched_pose else None
            if matched_pose:
                with self._lock:
                    corrected = matched_pose["pose"]
                    correction = self._angle_delta(corrected.theta_deg, start_pose.theta_deg)
                    self._pose.x_cm += corrected.x_cm - start_pose.x_cm
                    self._pose.y_cm += corrected.y_cm - start_pose.y_cm
                    self._pose.theta_deg = self._normalize_deg(self._pose.theta_deg + correction)
                    self._pose.confidence = corrected.confidence
                    self._pose.source = corrected.source
                    self._pose_history.append(asdict(self._pose))
                    for sample in scan.samples:
                        sample.angle_deg = self._normalize_deg(sample.angle_deg + correction)
                    scan.pose = corrected
        if update_map:
            pose_for_map = matched_pose["pose"] if matched_pose else start_pose
            with self._lock:
                for sample in scan.samples:
                    self._grid.update_ray(pose_for_map, sample.angle_deg, sample.distance_cm, sample.valid, settings.slam_max_range_cm)
        with self._lock:
            self._preview_grid.reset()
            self._status["scan_preview"] = False
            self._scans.append(scan)
            self._scans = self._scans[-200:]
            self._pose_history = self._pose_history[-5000:]
            self._status["scan_count"] += 1
        return scan

    def _sample_at_angle(self, angle_deg: float) -> RangeSample:
        self._stop_event.wait(max(0.0, float(settings.slam_sample_settle_s)))
        samples = []
        wanted = max(1, int(settings.slam_samples_per_angle))
        interval = 1 / max(1.0, float(settings.tof_poll_hz))
        seen = self._range_snapshot_getter().get("sample_count") if self._range_snapshot_getter else None
        deadline = time.monotonic() + max(1.0, wanted * interval * 3)
        observed = 0
        while observed < wanted and time.monotonic() < deadline:
            self._check_guard()
            if self._range_snapshot_getter:
                data = self._range_snapshot_getter()
                identity = data.get("sample_count")
                age = data.get("age_s")
                if identity == seen or not data.get("available") or age is None or age > settings.tof_max_age_s:
                    self._stop_event.wait(min(.05, interval))
                    continue
                seen = identity
                distance = data.get("distance_cm")
            else:
                distance = self._fresh_distance()
            observed += 1
            if distance is not None:
                samples.append(float(distance))
            if observed < wanted:
                self._stop_event.wait(interval)
        distance = statistics.median(samples) if samples else None
        if self._range_snapshot_getter and not observed:
            raise MappingHalted("tof_unavailable")
        valid = distance is not None and settings.slam_min_range_cm <= distance <= settings.slam_max_range_cm
        return RangeSample(angle_deg=round(self._normalize_deg(angle_deg), 2), distance_cm=round(distance, 1) if distance is not None else None, valid=valid)

    def _sample_now(self, angle_deg: float) -> RangeSample:
        distance = self._fresh_distance()
        valid = distance is not None and settings.slam_min_range_cm <= float(distance) <= settings.slam_max_range_cm
        return RangeSample(
            angle_deg=round(self._normalize_deg(angle_deg), 2),
            distance_cm=round(float(distance), 1) if distance is not None else None,
            valid=valid,
        )

    def _scan_match(self, scan: RangeScan) -> dict | None:
        base = Pose2D(**asdict(scan.pose))
        base.confidence = self._copy_pose().confidence
        best_score = -999.0
        best_cost = float("inf")
        best_pose = base
        for dx in range(-15, 16, 5):
            self._check_guard()
            for dy in range(-15, 16, 5):
                for dt in range(-12, 13, 4):
                    candidate = Pose2D(base.x_cm + dx, base.y_cm + dy, self._normalize_deg(base.theta_deg + dt), base.confidence, "scan_match")
                    score = self._grid.score_scan(candidate, scan.samples, settings.slam_max_range_cm, scan.pose.theta_deg)
                    cost = dx * dx + dy * dy + dt * dt
                    if score > best_score + 1e-9 or (abs(score - best_score) <= 1e-9 and cost < best_cost):
                        best_score = score
                        best_cost = cost
                        best_pose = candidate
        if best_score < 0.15:
            base.confidence = max(0.1, base.confidence * 0.85)
            base.source = "odometry"
            return {"pose": base, "match": {"accepted": False, "score": round(best_score, 3)}}
        # Longer travel needs real map evidence, not just a larger confidence
        # bonus for marginal matches. Weak matches retain the old small bonus.
        best_pose.confidence = min(1.0, base.confidence + (0.2 if best_score >= 1.0 else 0.08))
        return {"pose": best_pose, "match": {"accepted": True, "score": round(best_score, 3)}}

    def _choose_explore_heading(self, scan: RangeScan) -> float | None:
        target = self._choose_explore_target(scan)
        return target.heading_deg if target else None

    def _choose_explore_target(self, scan: RangeScan):
        limits = getattr(self._run_limits, "value", None)
        distance = min(100, max(1, settings.slam_explore_step_cm))
        if limits and limits["distance_cm"] is not None:
            distance = min(distance, max(0, limits["distance_cm"] - limits["travelled_cm"]))
        return choose_target(scan, self._copy_pose(), self._grid, self._visited,
                             distance,
                             self._explore_clearance(), settings.slam_max_range_cm)

    @staticmethod
    def _explore_clearance() -> float:
        return max(15.0, float(settings.motion_obstacle_stop_cm) + 10.0)

    def _fresh_distance(self) -> float | None:
        try:
            distance = self._distance_getter()  # ToF service filters stale data.
            return float(distance) if distance is not None and math.isfinite(float(distance)) and float(distance) >= 0 else None
        except Exception:
            return None

    def _check_guard(self) -> None:
        reason = "stop_requested" if self._stop_event.is_set() else ""
        limits = getattr(self._run_limits, "value", None)
        if limits:
            if limits["deadline"] is not None and time.monotonic() >= limits["deadline"]:
                reason = reason or "exploration_deadline"
            if limits["distance_cm"] is not None and limits["travelled_cm"] >= limits["distance_cm"]:
                reason = reason or "travel_budget_reached"
        intent = current_intent()
        if intent is not None and intent.is_set():
            reason = reason or "motion_owner_cancelled"
        if not settings.slam_enabled:
            reason = reason or "slam_disabled"
        if not reason:
            ok, reason = self._can_run_callback()
            reason = "" if ok else reason
        if reason:
            self._stop_motion()
            raise MappingHalted(reason)

    def _travel_leg(self, distance_cm: float) -> float:
        limits = getattr(self._run_limits, "value", None)
        if limits and limits["distance_cm"] is not None:
            distance_cm = min(distance_cm, max(0, limits["distance_cm"] - limits["travelled_cm"]))
        travelled = 0.0
        self._set_status(phase="driving", leg_target_cm=round(distance_cm, 1))
        while travelled < distance_cm - 1.0:
            self._check_guard()
            distance = self._fresh_distance()
            if distance is None:
                raise MappingHalted("tof_unavailable")
            chunk = min(20.0, distance_cm - travelled, distance - self._explore_clearance())
            if chunk < 3:
                self._set_status(last_stop_reason="obstacle_replan")
                break
            before = self._copy_pose()
            completed = self._move_forward(chunk)
            after = self._copy_pose()
            delta = math.hypot(after.x_cm - before.x_cm, after.y_cm - before.y_cm)
            travelled += delta
            self._set_status(leg_travelled_cm=round(travelled, 1), progress=min(1, travelled / distance_cm))
            if not completed:
                break
            if delta < 0.5:
                raise MappingHalted("no_progress")
        return travelled

    def _turn_to_heading(self, heading_deg: float) -> bool:
        delta = self._angle_delta(heading_deg, self._pose.theta_deg)
        return self._turn_relative(delta)

    def _turn_relative(self, angle_deg: float) -> bool:
        self._check_guard()
        if abs(angle_deg) < 1:
            return True
        future = self.motion.move(mode="turn", direction="turn", angle_deg=angle_deg, speed=settings.slam_turn_speed, wait=False, ignore_obstacles=True, cancel_event=self._stop_event)
        return self._await_motion(future, forward=False, timeout=max(settings.motion_encoder_timeout_s + 2, abs(angle_deg) / 45 * 4 + 2))

    def _move_forward(self, distance_cm: float) -> bool:
        self._check_guard()
        distance = self._fresh_distance()
        if distance is None:
            raise MappingHalted("tof_unavailable")
        if distance <= self._explore_clearance():
            self._set_status(last_stop_reason="obstacle_replan")
            return False
        future = self.motion.move(mode="distance", direction="forward", distance_cm=distance_cm, speed=settings.slam_drive_speed, wait=False, cancel_event=self._stop_event)
        return self._await_motion(future, forward=True, timeout=max(settings.motion_encoder_timeout_s + 2, distance_cm / 3 + 2))

    def _await_motion(self, future, *, forward: bool, timeout: float) -> bool:
        if future is None:
            raise MappingHalted("motion_unavailable")
        started = time.monotonic()
        stop_reason = ""
        try:
            while not future.done():
                self._check_guard()
                if time.monotonic() - started > timeout:
                    raise MappingHalted("motion_timeout")
                if forward:
                    distance = self._fresh_distance()
                    if distance is None:
                        raise MappingHalted("tof_unavailable")
                    if distance <= self._explore_clearance():
                        raise MappingHalted("obstacle_replan")
                self._stop_event.wait(0.05)
        except MappingHalted as exc:
            stop_reason = str(exc)
            self._stop_motion()
        finally:
            if stop_reason:
                try:
                    future.result(timeout=1.0)
                except Exception:
                    pass
        if not future.done():
            raise MappingHalted(stop_reason or "motion_timeout")
        completed = future.result()
        result = completed if isinstance(completed, dict) else self.motion.status().get("last_result", {})
        delta = self.motion.wheel_delta_cm_from_result(result)
        if delta is not None:
            # Update at each short sub-move, including an interrupted move.
            # Do not use commanded distance or sample pre-reset live counters.
            self._apply_wheel_delta(*delta)
        self._update_front_ray()
        if not stop_reason and result.get("ok") is not True:
            stop_reason = result.get("reason", "motion_failed")
        if forward and stop_reason in {"obstacle", "obstacle_replan"}:
            self._set_status(last_stop_reason="obstacle_replan")
            return False
        if stop_reason:
            raise MappingHalted(stop_reason)
        if delta is None:
            raise MappingHalted("missing_odometry")
        return True

    def _apply_wheel_delta(self, left_cm, right_cm) -> None:
        distance_cm = (left_cm + right_cm) / 2
        limits = getattr(self._run_limits, "value", None)
        if limits:
            limits["travelled_cm"] += abs(distance_cm)
        angle = self.motion.turn_angle_deg_from_wheel_cm(left_cm, right_cm)
        with self._lock:
            theta = math.radians(self._pose.theta_deg + angle / 2)
            self._pose.x_cm += math.cos(theta) * distance_cm
            self._pose.y_cm += math.sin(theta) * distance_cm
            self._pose.theta_deg = self._normalize_deg(self._pose.theta_deg + angle)
            self._pose.source = "odometry"
            self._pose.confidence = max(0.1, self._pose.confidence - abs(distance_cm) / 1200 - abs(angle) / 3600)
            self._pose_history.append(asdict(self._pose))
            self._pose_history = self._pose_history[-5000:]

    def _track_drive_worker(self, drive: dict, future: Future) -> None:
        try:
            completed = future.result(timeout=max(2.0, float(drive.get("duration") or 1.0) + 20.0))
        except Exception as exc:
            self._set_status(last_stop_reason=f"drive_track_failed:{exc}")
            return
        with self._lock:
            intent = current_intent()
            if (intent is not None and intent.is_set()) or not self._status.get("localization_enabled") or self._status.get("active"):
                return
        result = completed if isinstance(completed, dict) else self.motion.status().get("last_result", {})
        if result.get("ok") is False:
            self._set_status(last_stop_reason=result.get("reason", "motion_failed"))
            return
        self._apply_drive_delta(drive, result)
        self._update_front_ray()

    def _apply_drive_delta(self, drive: dict, result: dict) -> None:
        estimate = self.motion.estimate_wheel_delta_cm(drive, result)
        if estimate is None:
            with self._lock:
                self._pose.confidence = max(0.1, self._pose.confidence * 0.92)
                self._pose.source = "untracked_motion"
            return
        left_cm, right_cm = estimate
        distance_cm = (left_cm + right_cm) / 2.0
        delta_theta_deg = self.motion.turn_angle_deg_from_wheel_cm(left_cm, right_cm)
        with self._lock:
            mid_theta = math.radians(self._pose.theta_deg + delta_theta_deg / 2.0)
            self._pose.x_cm += math.cos(mid_theta) * distance_cm
            self._pose.y_cm += math.sin(mid_theta) * distance_cm
            self._pose.theta_deg = self._normalize_deg(self._pose.theta_deg + delta_theta_deg)
            self._pose.source = "tracked_drive"
            self._pose.confidence = max(0.1, self._pose.confidence - abs(distance_cm) / 1200.0 - abs(delta_theta_deg) / 3600.0)
            self._pose_history.append(asdict(self._pose))

    def _update_front_ray(self) -> None:
        if self._status.get("phase") == "scanning":
            return  # Stationary scan samples have their own non-committed preview.
        pose = self._copy_pose()
        sample = self._sample_now(pose.theta_deg)
        with self._lock:
            self._grid.update_ray(pose, sample.angle_deg, sample.distance_cm, sample.valid, settings.slam_max_range_cm)

    def _turn_angle_from_live_counts(self) -> float:
        try:
            counts = self.motion.status().get("counts", {})
            left_cm = float(counts.get("left", 0)) * self.motion.cm_per_count()
            right_cm = float(counts.get("right", 0)) * self.motion.cm_per_count()
            return self.motion.turn_angle_deg_from_wheel_cm(left_cm, right_cm)
        except Exception:
            return 0.0

    def _turn_angle_from_motion_result(self, result: dict) -> float | None:
        final = result.get("final") if isinstance(result, dict) else None
        if not isinstance(final, dict):
            return None
        try:
            left_cm = float(final.get("left", 0)) * self.motion.cm_per_count()
            right_cm = float(final.get("right", 0)) * self.motion.cm_per_count()
            return self.motion.turn_angle_deg_from_wheel_cm(left_cm, right_cm)
        except Exception:
            return None

    def _front_blocked(self) -> bool:
        distance = self._distance_getter()
        return distance is not None and distance < settings.motion_obstacle_stop_cm

    def _copy_pose(self) -> Pose2D:
        with self._lock:
            return Pose2D(**asdict(self._pose))

    def _set_status(self, **values) -> None:
        with self._lock:
            self._status.update(values)

    def _scan_summary(self, scan: RangeScan) -> dict:
        valid = [sample for sample in scan.samples if sample.valid]
        return {
            "ts": scan.ts,
            "sample_count": len(scan.samples),
            "valid_count": len(valid),
            "pose": asdict(scan.pose),
            "match": scan.match,
        }

    def _scan_to_dict(self, scan: RangeScan) -> dict:
        return {"ts": scan.ts, "pose": asdict(scan.pose), "samples": [asdict(sample) for sample in scan.samples], "match": scan.match}

    @staticmethod
    def _normalize_deg(angle: float) -> float:
        return float(angle) % 360.0

    @staticmethod
    def _angle_delta(target: float, current: float) -> float:
        return ((target - current + 180.0) % 360.0) - 180.0
