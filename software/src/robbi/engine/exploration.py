"""Conservative local exploration targets from a single forward range scan."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ExploreTarget:
    heading_deg: float
    distance_cm: float
    score: float


def angle_delta(target, current):
    return (target - current + 180) % 360 - 180


def choose_target(scan, pose, grid, visited, max_step_cm, clearance_cm, max_range_cm):
    """Missing/invalid ranges are unknown, never permission to drive forward."""
    samples = [s for s in scan.samples if s.valid and s.distance_cm is not None
               and math.isfinite(s.distance_cm) and 0 < s.distance_cm <= max_range_cm]
    best = None
    for sample in samples:
        sector = [s for s in scan.samples if abs(angle_delta(s.angle_deg, sample.angle_deg)) <= 22]
        if any(not s.valid or s.distance_cm is None or not math.isfinite(s.distance_cm) for s in sector):
            continue
        # Require supporting measurements on both sides, not one clear needle.
        nearby = [(angle_delta(s.angle_deg, sample.angle_deg), s.distance_cm) for s in sector]
        left = [d for a, d in nearby if 2 <= a <= 22]
        right = [d for a, d in nearby if -22 <= a <= -2]
        if not left or not right:
            continue
        distance = min(max_step_cm, sample.distance_cm - clearance_cm,
                       min(left) - clearance_cm, min(right) - clearance_cm)
        if distance < 10:
            continue
        theta = math.radians(sample.angle_deg)
        x = pose.x_cm + math.cos(theta) * distance
        y = pose.y_cm + math.sin(theta) * distance
        visits = sum(math.hypot(x - p[0], y - p[1]) < 35 for p in visited[-100:])
        unknown = sum(grid.world_to_cell(pose.x_cm + math.cos(theta) * distance * f,
                                         pose.y_cm + math.sin(theta) * distance * f)
                      not in grid.cells for f in (0.5, 0.75, 1.0)) / 3
        turn = abs(angle_delta(sample.angle_deg, pose.theta_deg)) / 180
        score = distance / max(1, max_step_cm) + 0.25 * unknown - 0.5 * min(visits, 4) - 0.15 * turn
        if best is None or score > best.score:
            best = ExploreTarget(sample.angle_deg, distance, score)
    return best
