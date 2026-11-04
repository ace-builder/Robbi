"""Bounded NumPy primitives for the local wake matcher.

DTW evaluates the same three-neighbour recurrence and float32 additions as the
original matcher. Cells on one anti-diagonal are independent, moving the inner
cell loop into NumPy without approximate bands or changing the wake threshold.
"""
from __future__ import annotations

import numpy as np


def normalize_rows(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float32)
    if values.ndim != 2 or not values.shape[0] or not values.shape[1]:
        raise ValueError("wake features must be a non-empty 2D array")
    if not np.isfinite(values).all():
        raise ValueError("wake features contain non-finite values")
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    return values / np.maximum(norms, 1e-9)


def dtw_normalized_rows(a: np.ndarray, b: np.ndarray) -> float:
    """Exact cosine DTW of already normalized, non-empty float32 feature rows."""
    if a.ndim != 2 or b.ndim != 2 or not a.shape[0] or not b.shape[0] or a.shape[1] != b.shape[1]:
        raise ValueError("wake feature arrays must have compatible non-empty shapes")
    cost = a @ b.T
    np.clip(cost, -1.0, 1.0, out=cost)
    np.subtract(1.0, cost, out=cost)
    rows, cols = cost.shape
    stride = cols + 1
    dp = np.full((rows + 1) * stride, np.inf, dtype=np.float32)
    dp[0] = 0.0
    cost = cost.ravel()
    indices = np.arange(1, rows + 1)
    dp_base = indices * cols
    cost_base = indices * (cols - 1) - cols - 1
    for diagonal in range(2, rows + cols + 1):
        span = slice(max(1, diagonal - cols) - 1, min(rows, diagonal - 1))
        target = dp_base[span] + diagonal
        previous = np.minimum(dp[target - stride], dp[target - 1])
        np.minimum(previous, dp[target - stride - 1], out=previous)
        dp[target] = cost[cost_base[span] + diagonal] + previous
    return float(dp[-1] / max(1, rows + cols))


class AudioWindow:
    """Circular samples with a reusable chronological view for periodic matching.

    Appending only copies the incoming samples; chronological materialization
    happens when matching is due. Both arrays are allocated once. Callers must
    finish consuming ``view()`` before another call overwrites its scratch space.
    """

    def __init__(self, size: int):
        if size < 1:
            raise ValueError("audio window size must be positive")
        self._samples = np.zeros(size, dtype=np.float32)
        self._scratch = np.empty(size, dtype=np.float32)
        self._position = 0

    def append(self, values: np.ndarray) -> None:
        size = self._samples.size
        if values.size >= size:
            self._samples[:] = values[-size:]
            self._position = 0
            return
        first = min(values.size, size - self._position)
        self._samples[self._position : self._position + first] = values[:first]
        if first < values.size:
            self._samples[: values.size - first] = values[first:]
        self._position = (self._position + values.size) % size

    def view(self) -> np.ndarray:
        tail = self._samples.size - self._position
        self._scratch[:tail] = self._samples[self._position :]
        self._scratch[tail:] = self._samples[: self._position]
        return self._scratch

    def clear(self) -> None:
        self._samples.fill(0.0)
        self._position = 0
