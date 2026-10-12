# VibroSense

**What can the undocumented motion sensor in an Apple Silicon MacBook actually sense?**

Recent MacBooks contain a 6-axis inertial measurement unit (accelerometer plus
gyroscope) that Apple exposes through no public API. It is reachable through
IOKit HID as `AppleSPUHIDDevice` on vendor usage page `0xFF00`, and the driver
identifies the part as a Bosch BMI286.

This repo is a from-scratch reader for it, a recorder, and an analysis tool for
answering a question the existing projects do not: **what is this sensor's real
resolution and bandwidth, and what can it therefore detect?**

See [FINDINGS.md](FINDINGS.md) for measurements so far.

## Quick start

No root required (see [Privileges](#privileges)):

```
python3 collect.py --list              # is the IMU on this Mac?
python3 collect.py --probe             # what every SPU sensor delivers
python3 collect.py rest --seconds 600  # noise floor: hard desk, leave the room
python3 collect.py protocol            # guided labelled events, about 6 min
python3 analyze.py data/<session>      # timing, noise floor, detectability
```

Requires Python 3 and NumPy. matplotlib is optional and adds figures.

## Files

| File | What it does |
|---|---|
| [sensor.py](sensor.py) | IOKit HID reader: accelerometer, gyroscope, hardware timestamps, plus `probe()` for every SPU sensor |
| [collect.py](collect.py) | Records sessions to `data/`: a rest-noise mode and a guided labelled-event mode |
| [analyze.py](analyze.py) | Sample-rate and timing quality, noise floor, spectra, per-event detectability |
| [tests/make_synthetic.py](tests/make_synthetic.py) | Generates a fake session so the analysis can be tested without hardware |

Recordings are written to `data/`, which is git-ignored because the files are
large and can capture activity around the machine.

## How the sensor is read

1. The IMU appears in the IOKit registry as `AppleSPUHIDDevice` entries on
   vendor usage page `0xFF00`: usage 3 is the accelerometer, usage 9 the gyroscope.
2. `AppleSPUHIDDriver` is asked to report by setting `SensorPropertyReportingState`,
   `SensorPropertyPowerState` and `ReportInterval`.
3. Each device is opened with `IOHIDDeviceCreate` / `IOHIDDeviceOpen`, and an
   input-report callback is registered so macOS delivers every report with a
   `mach_absolute_time` hardware timestamp.
4. Each report is 22 bytes. Bytes 6 to 17 hold x, y, z as little-endian int32 in
   Q16 fixed point: divide by 65536 for g (accelerometer) or deg/s (gyroscope).

## Privileges

Reading works **without root** as the active console user on macOS 26.6.2.
macOS keeps the IMU streaming for its own use (about 806 reports/s with nothing
of ours running, and WindowServer holds it open), so the driver property writes
that would wake it probably fail silently without root and it does not matter.

Earlier projects report that sudo is required. If no data arrives for you, try
`sudo python3 collect.py ...` and please open an issue with your model and
macOS version.

## Prior work

- [olvvier/apple-silicon-accelerometer](https://github.com/olvvier/apple-silicon-accelerometer)
  first documented this access path, and this reader follows the report format it
  describes. Several apps build on it (Knock, spank, Haptyk).
- ["Et Tu, MacBook?" (arXiv 2609.21569)](https://arxiv.org/html/2609.21569v1)
  shows the same sensor leaks keystrokes at 89.1 to 97.5% character accuracy
  without root, and can profile users and desk surfaces. Per the paper, Apple
  confirmed the leak in May 2026 and a patch was in development at submission.
  **This repo deliberately does not implement keystroke recovery.**

## Hardware tested

| Model | Chip | macOS | Works |
|---|---|---|---|
| MacBook Pro (Mac16,8) | M4 Pro | 26.6.2 | yes, 802 Hz, no root |

Reports for other models are welcome. The upstream project lists Intel Macs,
the M1 MacBook Pro (2020) and the Mac Studio M4 Max as incompatible.

## Caveats

- Undocumented interface. A macOS update can break it at any time.
- One machine measured so far, so nothing here is known to generalise.
- The per-event detectability analysis has been validated on synthetic data and
  has not yet been run on a real labelled session.

## Ethics

Built for measuring and understanding this sensor. Record only your own devices,
or with the explicit consent of whoever owns the machine and shares the space.

## License

MIT, see [LICENSE](LICENSE).
