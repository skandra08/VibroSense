"""Answer the go/no-go question: can the MacBook IMU see anything above its noise?

  python3 analyze.py data/<session>

Prints, for a session recorded by collect.py:
  1. the real sample rate and timing quality
  2. the noise floor while at rest
  3. for each labeled event: how often it rises above the rest noise
and saves figures to data/<session>/figs/.
"""

import csv
import sys
from pathlib import Path

import numpy as np

WINDOW_S = 0.1          # detection window length
DETREND_S = 1.0         # moving average removed to drop gravity and slow drift
FALSE_ALARM = 0.01      # threshold = this tail of the rest-window distribution
VISIBLE_RATE = 0.5      # sustained event: >50% of its windows beat the threshold
BURST_RATE = 0.03       # bursty event: some windows beat it (rest only does ~1%)...
BURST_PEAK = 2.0        # ...and the loudest 1% of windows reach 2x the threshold


def load_imu(path):
    rows = {"accel": [], "gyro": []}
    with open(path) as f:
        r = csv.reader(f)
        next(r)
        for sensor, t, x, y, z in r:
            rows[sensor].append((float(t), float(x), float(y), float(z)))
    return {k: np.array(v) for k, v in rows.items() if v}


def load_events(path):
    if not path.exists():
        return []
    with open(path) as f:
        r = csv.DictReader(f)
        return [(row["label"], float(row["t_start"]), float(row["t_end"])) for row in r]


def timing_report(name, a):
    t = a[:, 0]
    dt = np.diff(t)
    rate = (len(t) - 1) / (t[-1] - t[0])
    med = np.median(dt)
    gaps = int(np.sum(dt > 3 * med))
    print(f"  {name:5s} {len(t):>9,d} samples  {rate:8.1f} Hz  "
          f"median dt {med * 1e3:6.3f} ms  p99 dt {np.percentile(dt, 99) * 1e3:6.3f} ms  "
          f"gaps>3x: {gaps}")
    return rate


def detrend(v, fs):
    """Subtract a ~1 s moving average per axis, leaving only fast vibration."""
    n = max(int(DETREND_S * fs), 1)
    k = np.ones(n) / n
    out = np.empty_like(v)
    for i in range(v.shape[1]):
        pad = np.pad(v[:, i], (n // 2, n - 1 - n // 2), mode="edge")
        out[:, i] = v[:, i] - np.convolve(pad, k, mode="valid")
    return out


def window_rms(v, fs):
    """RMS of the detrended vector magnitude in non-overlapping windows."""
    n = max(int(WINDOW_S * fs), 1)
    mag2 = np.sum(detrend(v, fs) ** 2, axis=1)
    m = len(mag2) // n
    return np.sqrt(mag2[: m * n].reshape(m, n).mean(axis=1))


def welch(x, fs, nperseg=1024):
    """Power spectral density (g^2/Hz) by Welch's method, NumPy only."""
    nperseg = min(nperseg, len(x))
    step = nperseg // 2
    win = np.hanning(nperseg)
    scale = fs * np.sum(win ** 2)
    segs = [x[i:i + nperseg] for i in range(0, len(x) - nperseg + 1, step)]
    p = np.mean([np.abs(np.fft.rfft((s - s.mean()) * win)) ** 2 for s in segs], axis=0)
    p = p / scale
    p[1:-1] *= 2
    return np.fft.rfftfreq(nperseg, 1 / fs), p


def select(a, t0, t1):
    return a[(a[:, 0] >= t0) & (a[:, 0] < t1)]


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    sess = Path(sys.argv[1])
    data = load_imu(sess / "imu.csv")
    events = load_events(sess / "events.csv")
    if "accel" not in data:
        print("no accelerometer data found")
        return 1

    print("\n1. TIMING")
    rates = {k: timing_report(k, v) for k, v in data.items()}
    acc = data["accel"]
    fs = rates["accel"]

    rest = [(a, b) for lab, a, b in events if lab == "rest"] or [(acc[0, 0], acc[-1, 0])]
    # The first rest segment is the baseline. Later rest segments are checked
    # as false alarms in section 3.
    base = select(acc, *rest[0])

    print("\n2. NOISE FLOOR AT REST (first rest segment)")
    v = base[:, 1:4]
    mag = np.linalg.norm(v.mean(axis=0))
    print(f"  mean gravity vector magnitude: {mag:.4f} g   (should be about 1.0)")
    for i, ax in enumerate("xyz"):
        print(f"  {ax}: std {v[:, i].std() * 1e3:7.3f} mg")
    f, p = welch(v[:, 2] if len(v) > 1024 else v[:, 0], fs)
    for lo, hi in [(1, 10), (10, 30), (30, min(100, fs / 2 - 1))]:
        if hi > lo:
            band = (f >= lo) & (f < hi)
            if band.any():
                print(f"  noise density {lo:>3.0f}-{hi:<3.0f} Hz: "
                      f"{np.sqrt(p[band].mean()) * 1e6:9.1f} ug/rtHz  (z axis)")

    if not events:
        print("\n(no events.csv: rest-only session, nothing more to compare)")
        return 0

    print("\n3. EVENT DETECTABILITY")
    base_rms = window_rms(base[:, 1:4], fs)
    thr = float(np.quantile(base_rms, 1 - FALSE_ALARM))
    print(f"  threshold = {thr * 1e3:.3f} mg  "
          f"({FALSE_ALARM:.0%} false-alarm level from first rest segment)")
    print(f"  {'label':<10s} {'secs':>6s} {'rms mg':>9s} {'vs rest':>9s} "
          f"{'detected':>9s} {'peak/thr':>9s}  verdict")
    base_med = float(np.median(base_rms))
    results = {}
    for label in dict.fromkeys(l for l, _, _ in events):
        segs = [select(acc, a, b)[:, 1:4] for l, a, b in events if l == label]
        rms = np.concatenate([window_rms(s, fs) for s in segs if len(s) > fs * WINDOW_S])
        rate = float(np.mean(rms > thr))
        db = 20 * np.log10(np.median(rms) / base_med)
        secs = sum(len(s) for s in segs) / fs
        peak = float(np.percentile(rms, 99)) / thr
        if label == "rest":
            verdict = "(false-alarm check)"
        elif rate > VISIBLE_RATE:
            verdict = "VISIBLE (sustained)"
        elif rate > BURST_RATE and peak > BURST_PEAK:
            verdict = "VISIBLE (bursts)"
        elif rate > 0.15:
            verdict = "weak"
        else:
            verdict = "not visible"
        results[label] = rms
        print(f"  {label:<10s} {secs:6.0f} {np.median(rms) * 1e3:9.3f} {db:+8.1f}dB "
              f"{rate:9.0%} {peak:8.1f}x  {verdict}")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        figs = sess / "figs"
        figs.mkdir(exist_ok=True)

        fig, ax = plt.subplots(figsize=(12, 4))
        t0 = acc[0, 0]
        rms_all = window_rms(acc[:, 1:4], fs)
        ax.plot(t0 + np.arange(len(rms_all)) * WINDOW_S - t0, rms_all * 1e3, lw=0.6)
        ax.axhline(thr * 1e3, color="r", ls="--", lw=0.8, label="rest threshold")
        for label, a, b in events:
            if label != "rest":
                ax.axvspan(a - t0, b - t0, alpha=0.2, label=label)
        ax.set_yscale("log")
        ax.set_xlabel("seconds")
        ax.set_ylabel("vibration RMS (mg)")
        ax.legend(ncol=4, fontsize=7)
        fig.tight_layout()
        fig.savefig(figs / "timeline.png", dpi=130)

        fig, ax = plt.subplots(figsize=(8, 4))
        for label in results:
            segs = [select(acc, a, b)[:, 3] for l, a, b in events if l == label]
            seg = np.concatenate([s for s in segs if len(s) > 1024])
            if len(seg) > 1024:
                ff, pp = welch(seg, fs)
                ax.loglog(ff[1:], np.sqrt(pp[1:]) * 1e6, label=label)
        ax.set_xlabel("Hz")
        ax.set_ylabel("ug / sqrt(Hz)  (z axis)")
        ax.legend(fontsize=7)
        fig.tight_layout()
        fig.savefig(figs / "spectra.png", dpi=130)
        print(f"\nfigures saved to {figs}/")
    except ImportError:
        print("\n(matplotlib not available: skipped figures)")

    print("\nCaveat: this is a first look at a single session. 'VISIBLE' means the event\n"
          "beats the rest noise, not that a model can tell events apart yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
