"""Curve geometry and coupled wheel tracking, without GPIO/I/O."""

from bisect import bisect_right
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class PathPoint:
    s: float
    x: float
    y: float
    heading: float
    curvature: float


class CurvePath:
    def __init__(self, points: list[PathPoint], track_cm: float, direction: int = 1):
        self.points = tuple(points)
        self.distances = tuple(p.s for p in points)
        self.track_cm = track_cm
        self.length_cm = points[-1].s
        self.direction = direction

    def at(self, distance_cm: float) -> PathPoint:
        s = max(0.0, min(self.length_cm, distance_cm))
        index = min(len(self.points) - 1, bisect_right(self.distances, s))
        if index == 0 or s >= self.length_cm:
            return self.points[index]
        a, b = self.points[index - 1], self.points[index]
        t = (s - a.s) / max(1e-9, b.s - a.s)
        return PathPoint(s, *(getattr(a, key) + t * (getattr(b, key) - getattr(a, key))
                              for key in ("x", "y", "heading", "curvature")))

    def wheel_cm(self, distance_cm: float) -> tuple[float, float]:
        point = self.at(distance_cm)
        turn = self.track_cm * point.heading / 2
        return self.direction * point.s - turn, self.direction * point.s + turn

    def targets(self, cm_per_count: float) -> tuple[int, int]:
        return tuple(round(cm / cm_per_count) for cm in self.wheel_cm(self.length_cm))

    @classmethod
    def authored(cls, values, direction, track_cm):
        """A single travel direction with no inner-wheel reversal or cusp.

        Distances increase along travel; headings are BODY headings relative to
        the start, including for reverse travel. Curvature is d(heading)/ds.
        """
        if type(direction) is not int or direction not in (-1, 1):
            raise ValueError('Path direction must be 1 or -1')
        if not isinstance(values, list) or not 2 <= len(values) <= 2200:
            raise ValueError('An authored path needs 2–2200 samples')
        if not math.isfinite(track_cm) or track_cm <= 0:
            raise ValueError('Path wheel spacing must be positive')
        points = []
        for row in values:
            if not isinstance(row, list) or len(row) != 5 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in row):
                raise ValueError('Path samples must be five finite numbers')
            p = PathPoint(*row)
            if not 0 <= p.s <= 500 or abs(p.x) > 500 or abs(p.y) > 500 or abs(p.heading) > 4*math.pi or abs(p.curvature)*track_cm/2 >= .95:
                raise ValueError('Path exceeds distance, heading or turning-radius limits')
            if points and p.s <= points[-1].s:
                raise ValueError('Path distances must increase')
            if points and abs(p.heading-points[-1].heading)*track_cm/2 >= .95*(p.s-points[-1].s):
                raise ValueError('Path heading changes would reverse the inside wheel')
            points.append(p)
        if any(abs(v) > 1e-6 for v in (points[0].s, points[0].x, points[0].y, points[0].heading)):
            raise ValueError('Path must start at the local origin')
        return cls(points, track_cm, direction)



    @classmethod
    def straight(cls, distance_cm, track_cm):
        if not math.isfinite(distance_cm) or not math.isfinite(track_cm) or track_cm <= 0:
            raise ValueError('Straight path requires finite distance and positive wheel spacing')
        return cls([PathPoint(0,0,0,0,0), PathPoint(abs(distance_cm),distance_cm,0,0,0)],
                   track_cm, -1 if distance_cm < 0 else 1)

    @classmethod
    def arc(cls, radius_cm: float, angle_deg: float, track_cm: float):
        radius = max(track_cm / 2 + 0.5, abs(radius_cm))
        if not all(math.isfinite(v) for v in (radius_cm, angle_deg, track_cm)) or track_cm <= 0:
            raise ValueError("Arc geometry must be finite with positive wheel spacing")
        if abs(angle_deg) > 360:
            raise ValueError("An arc is limited to 360 degrees")
        direction = 1 if angle_deg >= 0 else -1
        theta = math.radians(abs(angle_deg))
        steps = max(1, math.ceil(abs(angle_deg) / 3))
        return cls([PathPoint(radius * theta * i / steps,
                              radius * math.sin(theta * i / steps),
                              direction * radius * (1 - math.cos(theta * i / steps)),
                              direction * theta * i / steps, direction / radius)
                    for i in range(steps + 1)], track_cm)

    @classmethod
    def sine(cls, radius_cm: float, max_heading_deg: float, cycles: int, track_cm: float,
             distance_cm: float | None = None, amplitude_cm: float | None = None):
        values = [radius_cm, max_heading_deg, track_cm]
        values.extend(v for v in (distance_cm, amplitude_cm) if v is not None)
        if not all(math.isfinite(v) for v in values) or track_cm <= 0:
            raise ValueError("Sine geometry must be finite with positive wheel spacing")
        radius = max(track_cm / 2 + 0.5, abs(radius_cm))
        angle = math.radians(max(1.0, min(80.0, abs(max_heading_deg))))
        cycles = max(1, min(6, int(cycles)))
        length = 4 * radius * math.sin(angle) * cycles if distance_cm is None else distance_cm
        if length < 0 or length > 2400:
            raise ValueError("Sine forward distance must be between 0 and 2400 cm")
        if length == 0:
            return cls([PathPoint(0, 0, 0, 0, 0)], track_cm)
        wave_number = 2 * math.pi * cycles / length
        # A shifted cosine is a sine wave with zero slope at both endpoints.
        # Limit curvature so the inside wheel never needs to reverse.
        max_amplitude = min(1 / (radius * wave_number ** 2), math.tan(angle) / wave_number)
        amplitude = max_amplitude if amplitude_cm is None else amplitude_cm
        if not 0 <= amplitude <= max_amplitude + 1e-9:
            raise ValueError("Sine amplitude exceeds radius/heading limits; increase distance or reduce amplitude")
        steps = 128 * cycles
        dx = length / steps
        points = []
        s, previous_ds_dx = 0.0, 1.0
        for i in range(steps + 1):
            x = i * dx
            phase = wave_number * x
            slope = amplitude * wave_number * math.sin(phase)
            ds_dx = math.hypot(1, slope)
            if i:
                s += dx * (previous_ds_dx + ds_dx) / 2
            points.append(PathPoint(s, x, amplitude * (1 - math.cos(phase)), math.atan(slope),
                                    amplitude * wave_number ** 2 * math.cos(phase) / ds_dx ** 3))
            previous_ds_dx = ds_dx
        return cls(points, track_cm)


class CurveFollower:
    def __init__(self, path: CurvePath, cm_per_count: float):
        self.path = path
        self.cm_per_count = cm_per_count
        self.integral = [0.0, 0.0]
        self.previous_error = [0.0, 0.0]
        self.previous_duty = [0.0, 0.0]
        self.previous_counts = [0.0, 0.0]
        self.velocity = [0.0, 0.0]
        self.max_heading_error_deg = 0.0
        self.checkpoints = []
        self.trim = 0.
        self.still_s = [0., 0.]
        self.breakaway_assists = [0, 0]
        self.assisting = [False, False]
        self.max_cross_track_cm = 0.

    def command(self, counts: tuple[int, int], cruise: float, minimum: float, cap: float,
                kp: float, ki: float, kd: float, integral_limit: float,
                dt: float, slew_per_s: float, *, raw_counts=None, trim_gain=.8,
                start_assist=True, pose=None, cross_track_gain=1.) -> tuple[float, float]:
        direction = self.path.direction
        progress = direction * sum(counts) * self.cm_per_count / 2
        point = self.path.at(progress)
        track_heading = point.heading
        if pose is not None:
            cross_track = -(pose[0]-point.x)*math.sin(point.heading)+(pose[1]-point.y)*math.cos(point.heading)
            self.max_cross_track_cm = max(self.max_cross_track_cm, abs(cross_track))
            lookahead = max(5., self.path.track_cm)
            # Correct lateral drift while travelling, tapering toward the
            # specified endpoint heading. Reverse paths invert steering sign.
            taper = min(1., max(0., self.path.length_cm-progress)/lookahead)
            correction = -direction*math.atan2(cross_track, lookahead)*taper*cross_track_gain
            track_heading += max(-math.radians(8), min(math.radians(8), correction))
        # Reuse this interpolation rather than allocating a second path sample.
        turn = self.path.track_cm * track_heading / 2
        desired = (direction * point.s-turn, direction * point.s+turn)
        half_turn_rate = self.path.track_cm * point.curvature / 2
        rates = (1 - direction * half_turn_rate, 1 + direction * half_turn_rate)
        maximum_rate = max(rates)
        # Static breakaway PWM is higher than rolling friction. Using the full
        # minimum here flattens the speed ratio at low cruise duty. Encoder
        # velocity feedback below corrects the model while wheels are moving.
        friction = min(minimum * .65, cruise * .65)
        phase_errors = [direction * (desired[i] / self.cm_per_count - counts[i]) for i in (0, 1)]
        measured = counts if raw_counts is None else raw_counts
        alpha = min(1., max(.001, dt) / (.08 + max(.001, dt)))
        for i in (0, 1):
            delta_counts = direction * (measured[i] - self.previous_counts[i])
            rate = delta_counts / max(.001, dt)
            self.velocity[i] += alpha * (rate - self.velocity[i])
            self.still_s[i] = self.still_s[i] + dt if delta_counts < 1 else 0.
        self.previous_counts[:] = measured
        common_rate = max(0., sum(self.velocity) / 2)
        rate_errors = [common_rate * rates[i] - self.velocity[i] for i in (0, 1)]
        actual_heading = (counts[1] - counts[0]) * self.cm_per_count / self.path.track_cm
        heading_error = math.degrees(point.heading - actual_heading)
        # Learn a bounded differential PWM trim from fused path error. A pure
        # proportional correction needs a permanent angle error to compensate
        # unequal motors. This slow integral removes that offset without an
        # assumed RPM/PWM calibration or integrating acceleration into distance.
        # Existing user PID integral is retained; don't stack two integrators.
        if ki == 0:
            self.trim = max(-cap*.3, min(cap*.3,
                self.trim + direction * math.degrees(track_heading-actual_heading) * trim_gain * min(dt,.05)))
        self.max_heading_error_deg = max(self.max_heading_error_deg, abs(heading_error))
        if len(self.checkpoints) < 3 and progress >= self.path.length_cm * (.25 * (len(self.checkpoints)+1)):
            self.checkpoints.append({'progress':round(progress / max(.001,self.path.length_cm),3),
                                     'target_deg':round(math.degrees(point.heading),3),
                                     'measured_deg':round(math.degrees(actual_heading),3)})
        output = []
        for side in (0, 1):
            feedforward = friction + (cruise - friction) * rates[side] / maximum_rate
            # Cross-couple the wheel phases: a leading wheel slows down while
            # its lagging partner speeds up. Common travel is handled by cruise.
            error = phase_errors[side] - phase_errors[1 - side]
            integral = max(-integral_limit, min(integral_limit, self.integral[side] + error * min(dt,.05)))
            derivative = (error - self.previous_error[side]) / max(0.001, dt)
            # Correct the wheel speed ratio every tick as well as accumulated
            # phase. This acts during travel instead of saving up a final turn.
            velocity_correction = kp * .10 * (rate_errors[side] - rate_errors[1-side])
            demand = feedforward + kp * error + velocity_correction + ki * integral + kd * derivative
            demand += self.trim if side else -self.trim
            # PWM-limited start assist gets the inside wheel rolling.
            # It never raises the owner's PWM ceiling or hides a stalled wheel.
            assist = start_assist and self.still_s[side] >= .12 and self.velocity[side] < 15 and demand > 0
            if assist:
                demand = max(demand, cap)
                if not self.assisting[side]:self.breakaway_assists[side] += 1
            self.assisting[side] = assist
            clipped = max(0.0, min(cap, demand))
            if demand == clipped or (demand > cap and error < 0) or (demand < 0 and error > 0):
                self.integral[side] = integral
            self.previous_error[side] = error
            output.append(clipped)
        # Interpolate BOTH demands together; independent ramps flatten the ratio.
        delta = max(abs(output[i] - self.previous_duty[i]) for i in (0, 1))
        fraction = min(1.0, max(0.0, slew_per_s) * min(max(dt, 0.001), 0.1) / max(delta, 1e-9))
        self.previous_duty = [max(0.0, min(cap, old + fraction * (new - old)))
                              for old, new in zip(self.previous_duty, output, strict=True)]
        return tuple(direction * duty for duty in self.previous_duty)
