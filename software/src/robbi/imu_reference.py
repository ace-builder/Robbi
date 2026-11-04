"""One persisted gravity reference for handling, driving and navigation."""
import json
import math
from pathlib import Path
import threading
import time

from .storage import write_json


def gravity_unit(values):
    if (not isinstance(values, (list, tuple)) or len(values) != 3
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)):
        raise ValueError('Upright reference requires three finite acceleration values')
    size = math.sqrt(sum(v*v for v in values))
    if size < .5:
        raise ValueError('Upright reference has no gravity direction')
    return [v/size for v in values]


class UprightReference:
    def __init__(self, data_dir):
        self.path = Path(data_dir)/'imu-upright.json'
        self.lock = threading.RLock()
        self.value = None
        for path, source in ((self.path, 'upright_pose'),
                             (Path(data_dir)/'imu-view-alignment.json', 'axis_calibration')):
            try:
                if not path.is_file() or path.stat().st_size > 4096:
                    continue
                value = json.loads(path.read_text())
                if value.get('version') != 1:
                    continue
                if source == 'upright_pose':
                    up = gravity_unit(value['gravity_unit'])
                else:
                    q = value['mount_wxyz']
                    if (not isinstance(q, list) or len(q) != 4
                            or any(type(v) not in (int,float) or not math.isfinite(v) for v in q)
                            or abs(sum(v*v for v in q)-1) > .01):
                        continue
                    w,x,y,z = q
                    up = gravity_unit([2*(x*z+w*y), 2*(y*z-w*x), 1-2*(x*x+y*y)])
                self.value = {'gravity_unit':up, 'source':source}
                break
            except (OSError, ValueError, TypeError, KeyError):
                continue

    def snapshot(self):
        with self.lock:
            return {**self.value, 'gravity_unit':list(self.value['gravity_unit'])} if self.value else None

    def save(self, acceleration):
        up = gravity_unit(acceleration)
        with self.lock:
            write_json(self.path, {'version':1, 'gravity_unit':up, 'saved_at':time.time()})
            self.value = {'gravity_unit':up, 'source':'upright_pose'}
            return self.snapshot()
