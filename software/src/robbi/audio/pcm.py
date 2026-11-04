"""Small vectorized PCM operations, independent of the removed audioop module.

NumPy is loaded only when audio is used. Samples are signed little-endian;
WAV's unsigned 8-bit conversion remains explicit at the media boundary.
"""

import math


def _plain_samples(data, width):
    if width not in (1, 2, 3, 4) or len(data) % width:
        raise ValueError("Invalid PCM width or truncated frame")
    return [
        int.from_bytes(data[i : i + width], "little", signed=True)
        for i in range(0, len(data), width)
    ]


def _samples(data, width):
    import numpy as np

    if width not in (1, 2, 3, 4) or len(data) % width:
        raise ValueError("Invalid PCM width or truncated frame")
    if width == 3:
        raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        value = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        return (value ^ 0x800000) - 0x800000
    return np.frombuffer(data, dtype={1: "i1", 2: "<i2", 4: "<i4"}[width])


def levels(data, width=2):
    try:
        import numpy as np
    except ImportError:
        values = _plain_samples(data, width)
        return (
            (
                int(math.sqrt(sum(v * v for v in values) / len(values))),
                max(abs(v) for v in values),
            )
            if values
            else (0, 0)
        )
    values = _samples(data, width).astype(np.float64)
    if not len(values):
        return 0, 0
    return int(math.sqrt(float(np.mean(values * values)))), int(np.max(np.abs(values)))


def mul(data, width, factor):
    if not math.isfinite(factor):
        raise ValueError("PCM gain must be finite")
    try:
        import numpy as np
    except ImportError:
        values = _plain_samples(data, width)
        limit = 1 << (8 * width - 1)
        return b"".join(
            max(-limit, min(limit - 1, math.floor(v * factor))).to_bytes(
                width, "little", signed=True
            )
            for v in values
        )
    values = _samples(data, width).astype(np.float64)
    limit = 1 << (8 * width - 1)
    values = np.clip(np.floor(values * factor), -limit, limit - 1)
    if width == 3:
        values = values.astype(np.int32)
        output = np.empty((len(values), 3), dtype=np.uint8)
        for index in range(3):
            output[:, index] = (values >> (8 * index)) & 255
        return output.tobytes()
    return values.astype({1: "i1", 2: "<i2", 4: "<i4"}[width]).tobytes()


def bias(data, width, value):
    if width != 1:
        raise ValueError("Only 8-bit PCM bias is used by Robbi")
    try:
        import numpy as np
    except ImportError:
        return bytes((v + value) & 255 for v in data)
    return (
        ((np.frombuffer(data, dtype=np.uint8).astype(np.int16) + value) & 255)
        .astype(np.uint8)
        .tobytes()
    )
