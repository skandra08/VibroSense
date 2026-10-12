"""Make a fake session in collect.py's format, to test analyze.py without hardware.

  python3 tests/make_synthetic.py <out_dir>

The fake sensor has white noise plus gravity, and events are short bursts of
band-limited vibration. Treat any numbers from it as a pipeline test only.
"""

import csv
import sys
from pathlib import Path

import numpy as np

FS = 800.0
NOISE_G = 0.0004   # rest noise per sample, in g
# (label, seconds, burst amplitude in g, burst rate per second, burst length s)
STEPS = [
    ("rest", 60, 0, 0, 0),
    ("type", 30, 0.004, 6, 0.05),
    ("rest", 20, 0, 0, 0),
    ("tap_desk", 30, 0.02, 0.5, 0.08),
    ("rest", 20, 0, 0, 0),
    ("walk_near", 30, 0.0006, 2, 0.15),   # deliberately near the noise floor
    ("rest", 20, 0, 0, 0),
]


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)
    t = 1000.0
    with open(out / "imu.csv", "w", newline="") as f, \
            open(out / "events.csv", "w", newline="") as g:
        w, e = csv.writer(f), csv.writer(g)
        w.writerow(["sensor", "t", "x", "y", "z"])
        e.writerow(["label", "t_start", "t_end"])
        for label, secs, amp, rate, blen in STEPS:
            n = int(secs * FS)
            ts = t + np.arange(n) / FS + rng.normal(0, 2e-5, n)
            sig = rng.normal(0, NOISE_G, (n, 3))
            sig[:, 2] += 1.0
            if amp:
                for onset in rng.uniform(0, secs, int(rate * secs)):
                    i = int(onset * FS)
                    m = int(blen * FS)
                    burst = amp * np.hanning(m)[:, None] * rng.normal(0, 1, (m, 3))
                    sig[i:i + m] += burst[: max(0, n - i)]
            for k in range(n):
                w.writerow(["accel", f"{ts[k]:.6f}", *(f"{v:.6f}" for v in sig[k])])
            for k in range(0, n, 8):    # gyro at a tenth of the rate; analysis only needs timing
                w.writerow(["gyro", f"{ts[k]:.6f}", "0.0", "0.0", "0.0"])
            e.writerow([label, f"{t:.6f}", f"{t + secs:.6f}"])
            t += secs
    print("wrote", out)


if __name__ == "__main__":
    main()
