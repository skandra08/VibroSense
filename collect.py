"""Record MacBook IMU data to disk.

  python3 collect.py --list                     # check the sensor exists (no root)
  sudo python3 collect.py rest --seconds 600    # noise-floor recording
  sudo python3 collect.py protocol              # guided, labeled events

Each run makes data/<timestamp>-<mode>/ containing:
  imu.csv     sensor,t,x,y,z   (accel in g, gyro in deg/s, t in seconds)
  events.csv  label,t_start,t_end   (protocol mode only; same clock as imu.csv)
  meta.json   machine, macOS version, settings
"""

import argparse
import csv
import json
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

import sensor

# (label, seconds, instruction). "rest" segments are the noise baseline.
PROTOCOL = [
    ("rest", 60, "Hands off the laptop and desk. Stay still and quiet."),
    ("type", 30, "Type normally on this laptop."),
    ("rest", 20, "Hands off. Stay still."),
    ("tap_desk", 30, "Tap the desk next to the laptop about once every 2 seconds."),
    ("rest", 20, "Hands off. Stay still."),
    ("walk_near", 30, "Walk back and forth near the desk."),
    ("rest", 20, "Sit down again. Stay still."),
    ("door", 40, "Open and close a nearby door about 5 times, with pauses."),
    ("rest", 20, "Stay still."),
    ("pickup", 40, "Pick the laptop up, hold it, and put it down. Repeat about 4 times."),
    ("rest", 30, "Hands off. Stay still."),
]
COUNTDOWN_S = 5


def sysctl(name):
    try:
        return subprocess.check_output(["sysctl", "-n", name], text=True).strip()
    except Exception:
        return "unknown"


def machine_info():
    return {
        "model": sysctl("hw.model"),
        "chip": sysctl("machdep.cpu.brand_string"),
        "macos": platform.mac_ver()[0],
        "python": platform.python_version(),
    }


def give_back_to_user(path):
    """We run under sudo, so hand the files back to the person who ran it."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid:
        for p in [path, *path.rglob("*")]:
            os.chown(p, int(uid), int(gid))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", nargs="?", choices=["rest", "protocol"])
    ap.add_argument("--list", action="store_true", help="list SPU devices and exit")
    ap.add_argument("--seconds", type=int, default=600, help="rest mode duration")
    ap.add_argument("--interval-us", type=int, default=1000,
                    help="requested driver report interval (default 1000 = 1 kHz)")
    ap.add_argument("--keep-every", type=int, default=1,
                    help="keep 1 of every N reports (default 1 = keep all)")
    ap.add_argument("--out", default="data", help="output directory")
    args = ap.parse_args()

    if args.list:
        devs = sensor.list_devices()
        print(f"{len(devs)} AppleSPUHIDDevice entries")
        for page, usage in sorted(set(devs)):
            tag = ""
            if page == sensor.PAGE_VENDOR:
                tag = sensor.SENSOR_NAMES.get(usage, "")
            print(f"  usage page 0x{page:04X}  usage {usage:<4} {tag}")
        print("IMU present:", sensor.has_imu())
        return 0
    if not args.mode:
        ap.error("choose a mode: rest or protocol (or --list)")
    if os.geteuid() != 0:
        print("This needs root to wake and open the sensor. Re-run with sudo.")
        return 1

    session = Path(args.out) / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.mode}"
    session.mkdir(parents=True)
    imu_file = open(session / "imu.csv", "w", newline="", buffering=1 << 20)
    imu = csv.writer(imu_file)
    imu.writerow(["sensor", "t", "x", "y", "z"])

    counts = {"accel": 0, "gyro": 0}
    latest_t = [0.0]  # newest accel timestamp: labels use this clock, not wall time

    def on_sample(name, t, x, y, z):
        counts[name] += 1
        if name == "accel":
            latest_t[0] = t
        imu.writerow([name, f"{t:.6f}", f"{x:.6f}", f"{y:.6f}", f"{z:.6f}"])

    reader = sensor.ImuReader(on_sample, args.interval_us, args.keep_every)
    opened = reader.start()
    if set(opened) != {"accel", "gyro"}:
        print("Could not open the sensor.")
        print("  opened:", opened, " errors (IOReturn):", reader.open_errors)
        imu_file.close()
        return 2

    meta = {**machine_info(), "mode": args.mode, "interval_us": args.interval_us,
            "keep_every": args.keep_every, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    (session / "meta.json").write_text(json.dumps(meta, indent=2))

    t_end = time.monotonic() + 5
    while counts["accel"] == 0 and time.monotonic() < t_end:
        reader.pump(0.25)
    if counts["accel"] == 0:
        print("Sensor opened but no data arrived in 5 s.")
        imu_file.close()
        return 3
    print(f"Streaming. Saving to {session}/")

    stop = threading.Event()
    events_file = None

    def run_protocol():
        nonlocal events_file
        events_file = open(session / "events.csv", "w", newline="", buffering=1)
        ev = csv.writer(events_file)
        ev.writerow(["label", "t_start", "t_end"])
        for label, secs, text in PROTOCOL:
            if stop.is_set():
                return
            print(f"\nNext: {label}  ({secs}s)  {text}")
            for n in range(COUNTDOWN_S, 0, -1):
                print(f"  starting in {n}...", end="\r", flush=True)
                time.sleep(1)
            print("\a  >>> GO <<<                ")
            t0 = latest_t[0]
            time.sleep(secs)
            ev.writerow([label, f"{t0:.6f}", f"{latest_t[0]:.6f}"])
        stop.set()

    worker = None
    if args.mode == "protocol":
        worker = threading.Thread(target=run_protocol, daemon=True)
        worker.start()

    t_start = time.monotonic()
    try:
        if args.mode == "rest":
            end = t_start + args.seconds
            next_report = t_start + 10
            while time.monotonic() < end:
                reader.pump(0.25)
                if time.monotonic() >= next_report:
                    el = time.monotonic() - t_start
                    print(f"  {el:5.0f}s  accel {counts['accel'] / el:7.1f} Hz"
                          f"  gyro {counts['gyro'] / el:7.1f} Hz")
                    next_report += 10
        else:
            while not stop.is_set():
                reader.pump(0.25)
    except KeyboardInterrupt:
        print("\nStopped early.")
        stop.set()

    el = max(time.monotonic() - t_start, 1e-9)
    imu_file.close()
    if events_file:
        events_file.close()
    print(f"\nDone. accel {counts['accel']} samples ({counts['accel'] / el:.1f} Hz), "
          f"gyro {counts['gyro']} samples ({counts['gyro'] / el:.1f} Hz)")
    give_back_to_user(session)
    print(f"Analyze with:  python3 analyze.py {session}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
