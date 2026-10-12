# VibroSense

What can the undocumented motion sensor in an Apple Silicon MacBook sense?

The accelerometer and gyroscope sit behind `AppleSPUHIDDevice` in IOKit. This
repo reads them with a small reader written from scratch, records labeled
vibration events, and (later) trains models to recognize them. Planned uses:
motion and tamper alarms, on-lap vs on-desk detection, presence and activity
sensing through the desk, and possibly breathing and heartbeat from laptop contact.

## Files
- `sensor.py`   IOKit reader (accelerometer + gyroscope, hardware timestamps)
- `collect.py`  records sessions to `data/` (rest noise test, or guided labeled events)
- `analyze.py`  timing, noise floor, and per-event detectability
- `tests/make_synthetic.py`  fake session to test the analysis without hardware

## Usage
```
python3 collect.py --list                     # no root: is the IMU there?
sudo python3 collect.py rest --seconds 60     # quick smoke test
python3 analyze.py data/<session>
sudo python3 collect.py rest --seconds 600    # noise floor: laptop on a hard desk, leave the room
sudo python3 collect.py protocol              # guided events (about 6 minutes)
python3 analyze.py data/<session>
```
Reading the sensor needs root. Run from the repo root. Recordings go to `data/`,
which is git-ignored.

**Go** if at least two of typing / tapping / walking / doors / pickup read as
VISIBLE and the rest false-alarm check stays near 1%.
**No-go** if only large motions (pickup) are visible. Then the sensor is a motion
detector, not a vibration sensor, and we change direction.

## Findings so far
Tested on a MacBook Pro (Mac16,8, M4 Pro), macOS 26.6.2. First recording: 60 s
with the laptop in a moving car, so it is NOT a noise floor.

- **Timing is excellent.** About 802 Hz on both sensors, median interval 1.247 ms,
  p99 1.248 ms, no gaps over 60 s.
- **Gravity checks out.** Mean vector magnitude 0.985 g, almost all on z.
- **Effective bandwidth is low.** Power falls about 50x between 5-10 Hz and 30-40 Hz,
  and above ~100 Hz the output sits at the sensor's own rounding step
  (15 ug per count, about 0.22 ug/rtHz). Likely a digital low-pass filter near
  30-50 Hz (an inference, not confirmed). Keystroke-level detail is probably
  out; footsteps, thumps, pickups and body motion fit.
- **Still unknown:** the true noise floor at rest (needs a desk recording), and
  whether the filter bandwidth is adjustable.

## Caveats
- Needs root. It sets driver properties to wake the sensor; these should reset
  on reboot (unverified).
- Undocumented, so a macOS update can break it.
- Results are from one machine so far.
- The event-detectability analysis has only been run on synthetic data.
