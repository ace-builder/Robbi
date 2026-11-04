"""Small, hardware-independent 6-DoF attitude filter (body -> world quaternion)."""

import math

GRAVITY = 9.80665


def norm(vector):
    return math.sqrt(sum(value * value for value in vector))


def multiply(a, b):
    w, x, y, z = a
    v, i, j, k = b
    return (w*v-x*i-y*j-z*k, w*i+x*v+y*k-z*j,
            w*j-x*k+y*v+z*i, w*k+x*j-y*i+z*v)


def euler_degrees(q):
    w, x, y, z = q
    return {
        "roll": math.degrees(math.atan2(2*(w*x+y*z), 1-2*(x*x+y*y))),
        "pitch": math.degrees(math.asin(max(-1, min(1, 2*(w*y-z*x))))),
        "yaw": math.degrees(math.atan2(2*(w*z+x*y), 1-2*(y*y+z*z))),
    }


class OrientationFilter:
    """Gyro integration plus gravity correction; yaw is relative, not a compass."""

    def __init__(self, gain=2.0):
        self.q = (1.0, 0.0, 0.0, 0.0)
        self.bias = (0.0, 0.0, 0.0)
        self.gain = gain
        self.last_time = None
        self.initialized = False
        self.sample_gaps = 0

    def update(self, acceleration, gyro, timestamp):
        if not all(math.isfinite(v) for v in (*acceleration, *gyro, timestamp)):
            raise ValueError("Non-finite IMU sample")
        magnitude = norm(acceleration)
        gravity_trusted = 0.85 * GRAVITY < magnitude < 1.15 * GRAVITY
        if not self.initialized:
            if not gravity_trusted:
                return self.q
            ax, ay, az = (v / magnitude for v in acceleration)
            q = (1 + az, ay, -ax, 0.0)
            self.q = tuple(v / norm(q) for v in q) if norm(q) > 1e-6 else (0, 1, 0, 0)
            self.initialized = True
            self.last_time = timestamp
            return self.q
        dt = timestamp - self.last_time
        if dt <= 0:
            return self.q
        self.last_time = timestamp
        if dt > 0.2:
            self.sample_gaps += 1
            return self.q  # Never invent a rotation across a sensor/CPU outage.
        omega = [value - bias for value, bias in zip(gyro, self.bias, strict=True)]
        if gravity_trusted:
            ax, ay, az = (v / magnitude for v in acceleration)
            w, x, y, z = self.q
            vx, vy, vz = 2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y)
            error = (ay*vz-az*vy, az*vx-ax*vz, ax*vy-ay*vx)
            omega = [value + self.gain * correction for value, correction in zip(omega, error, strict=True)]
        speed = norm(omega)
        if speed > 1e-12:
            half = speed * dt / 2
            delta = (math.cos(half), *(v * math.sin(half) / speed for v in omega))
            q = multiply(self.q, delta)
            self.q = tuple(v / norm(q) for v in q)
        return self.q


class GyroCalibration:
    """Accept only a sustained still window; timeouts leave the prior bias intact."""

    def __init__(self, started, duration_s=3.0):
        self.started = started
        self.duration = duration_s
        self.window_start = None
        self.previous_time = None
        self.anchor = None
        self.gyro_anchor = None
        self.total = [0.0, 0.0, 0.0]
        self.count = 0
        self.state = "collecting"
        self.progress = 0.0

    def update(self, acceleration, gyro, timestamp, stationary_allowed=True):
        if timestamp - self.started > 30:
            self.state = "timed_out"
            return None
        # Board stress can cause several degrees/s of constant offset. Reject
        # changing rates, rather than rejecting the very bias we need to measure.
        still = (stationary_allowed and abs(norm(acceleration) - GRAVITY) < 0.08 * GRAVITY and
                 norm(gyro) < math.radians(15))
        if self.anchor is not None:
            still = still and norm(tuple(a-b for a, b in zip(acceleration, self.anchor, strict=True))) < 0.25
            still = still and norm(tuple(a-b for a, b in zip(gyro, self.gyro_anchor, strict=True))) < math.radians(0.5)
        gap = self.previous_time is not None and timestamp - self.previous_time > 0.2
        self.previous_time = timestamp
        if not still or gap:
            self.window_start = None
            self.anchor = None
            self.gyro_anchor = None
            self.count = 0
            self.total = [0.0, 0.0, 0.0]
            self.progress = 0.0
            return None
        if self.window_start is None:
            self.window_start = timestamp
            self.anchor = acceleration
            self.gyro_anchor = gyro
        self.count += 1
        self.total = [a+b for a, b in zip(self.total, gyro, strict=True)]
        elapsed = timestamp - self.window_start
        self.progress = min(1.0, elapsed / self.duration)
        if elapsed >= self.duration and self.count >= 20:
            self.state = "complete"
            return tuple(value / self.count for value in self.total)
        return None
