# Findings

All measurements from one machine: MacBook Pro (Mac16,8), M4 Pro, macOS 26.6.2,
2026-10-12. Nothing here is known to generalise to other models.

## 1. Timing is excellent

Two recordings, 60 s and 206 s, both with no root:

| | samples | rate | median interval | p99 interval | gaps > 3x median |
|---|---|---|---|---|---|
| accelerometer | 48,458 / 165,340 | 801.8 / 801.7 Hz | 1.247 ms | 1.248 ms | 0 |
| gyroscope | 48,435 / 165,341 | 801.8 / 801.7 Hz | 1.247 ms | 1.248 ms | 0 |

Interval jitter is about a microsecond and no report was dropped in either run.
Hardware timestamps (`mach_absolute_time`) make this usable for spectral work.

The driver advertises `sensor_rates = "50 100 200 400 800"`, so 800 Hz is the
maximum. We request a 1000 us report interval and receive ~802 Hz.

## 2. Effective bandwidth is far below the sample rate

This is the main result. Sampling at 802 Hz implies content up to 400 Hz, but
the signal stops well before that. Power spectral density of the z axis, from a
quiet 10 s window of the 206 s recording:

| band | noise density (ug/rtHz) |
|---|---|
| 1 to 5 Hz | 9,844 |
| 5 to 10 Hz | 13,554 |
| 10 to 20 Hz | 3,633 |
| 20 to 30 Hz | 1,189 |
| 30 to 40 Hz | 272 |
| 40 to 50 Hz | 81 |
| 50 to 70 Hz | 18 |
| 70 to 100 Hz | 2.65 |
| 100 to 150 Hz | 0.28 |
| 150 to 400 Hz | 0.22 |

Power falls roughly 50-fold between the 5 to 10 Hz band and the 30 to 40 Hz band.
Above about 100 Hz the output sits at **0.22 ug/rtHz**, which matches the
quantisation step: 1 LSB is 1/65536 g, about 15.3 ug. In other words there is no
signal above 100 Hz at all, only rounding.

**Interpretation (inference, not confirmed):** the stream is low-pass filtered
somewhere around 30 to 50 Hz and then sampled at 800 Hz. The alternative, that
the environment simply contained nothing above 100 Hz, is hard to reconcile with
these recordings having been made in a moving car, which produces broadband
engine and road vibration. A sharp mechanical impulse (a desk knock) would
settle it, since an impulse excites all frequencies. **Not yet done.**

No exposed driver property appears to control the filter. The accelerometer
driver's properties include `sensor_rates` and `ReportInterval` but nothing
bandwidth-related.

## 3. The recordings are not a noise floor

Both sessions were taken with the laptop in a moving car, so they measure
vehicle vibration, not the sensor's own noise. Per-axis standard deviation ranged
from 17 to 154 mg and the gyroscope showed 1 to 6 deg/s of real rotation.

A proper noise floor needs a hard desk in a quiet room with nobody present.
**Still to do, and it gates everything about event detectability.**

## 4. The axes and scaling are physically correct

- Mean gravity vector magnitude 0.9848 g and 0.9854 g in the two recordings,
  almost all on z, consistent with a level laptop.
- Two opposite turns at about 24 s and 30 s into the second recording give a
  consistent speed estimate from lateral acceleration and yaw rate
  (v = a / omega): 14.6 m/s and 14.1 m/s, about 32 mph both times.

Two independent sensors agreeing on an external physical quantity is good
evidence that both the scaling and the axis convention are right.

## 5. The sensor is always on, and we are not the only reader

- The accelerometer driver's event counter advances by about **806 per second
  with nothing of ours running**, so macOS streams it continuously.
- `IOUserClientCreator` shows **WindowServer** (9 clients) and **locationd**
  holding the SPU drivers open; `audioanalyticsd` and `cameracaptured` appear on
  the devices.
- Our own reader appears in that list while running (`pid N, Python`) and
  disappears when it exits.

That last point means the registry can be polled to see which processes are
reading the motion sensors. No tool appears to do this, and the keystroke paper
notes that macOS's Input Monitoring permission does not cover this path.

## 6. Other SPU sensors on this machine

From `python3 collect.py --probe`, all opened without root:

| page | usage | reports/s | report bytes | identification |
|---|---|---|---|---|
| 0xFF00 | 3 | 801.7 | 22 | accelerometer, Bosch BMI286 |
| 0xFF00 | 9 | 802.4 | 22 | gyroscope, Bosch BMI286 |
| 0xFF00 | 4 | 5 to 10 | 122 | ambient light, Redbird VD6286, format not decoded |
| 0xFF00 | 5 | **999.9** | 14 | driver says TI TMP114 (a temperature part), but 1 kHz is implausible for temperature, so this is unexplained |
| 0x0020 | 138 | 10 | 3 | lid angle, MagAlpha ma781, read 116 degrees when open |
| 0xFF00 | 255 | 0 | - | unknown, silent |
| 0xFF0C | 1 | 1 to 2 | 5 | unknown |
| 0xFF0C | 5 | 0 | - | unknown |

The 1 kHz device on usage 5 is the most interesting open question here.

## 7. A heartbeat search failed, as expected

Ballistocardiography (heartbeat from body contact) was tested on the 206 s car
recording by looking for a stable 0.8 to 2.5 Hz rhythm across 19 overlapping
20 s windows.

Most axes gave peaks scattered by 11 to 29 bpm, which is noise. The
accelerometer z axis put 14 of 19 windows between 81 and 100 bpm with a broad
93 bpm peak at only 4x the surrounding noise. A car's suspension bounces at
roughly 1 to 1.5 Hz (60 to 90 per minute), directly on top of the heartbeat
band, so the most likely explanation is the vehicle. The respiration-band check
returned 7 cycles/min, too slow for an adult at rest, which supports that.

**Conclusion: no heartbeat detected, and this recording cannot answer the
question.** A seated test in a quiet room with a reference pulse would.

## Open questions

1. What is the sensor's actual noise floor at rest? (Needs a desk recording.)
2. Is the ~40 Hz bandwidth limit a filter or just the environment? (Needs a knock test.)
3. Can the filter bandwidth be changed at all?
4. What is the 1 kHz, 14-byte device on usage 5?
5. How does the ambient light report decode?
6. Does any of this hold on other Apple Silicon models?
