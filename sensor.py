"""Read the undocumented accelerometer + gyroscope on Apple Silicon MacBooks.

How it works, step by step:
  1. The IMU shows up in the IOKit registry as `AppleSPUHIDDevice` entries.
     The vendor usage page 0xFF00 carries it: usage 3 = accelerometer,
     usage 9 = gyroscope.
  2. The sensor is asleep until we ask its driver (`AppleSPUHIDDriver`) to
     report. We do that by setting three properties on the driver.
  3. We open each device as a HID device and register a callback. macOS then
     calls us for every report, with a hardware timestamp.
  4. Each report is 22 bytes. Bytes 6..17 hold x, y, z as little-endian int32
     in Q16 fixed point: divide by 65536 to get g (accel) or deg/s (gyro).

Opening the device and waking the driver need root. Listing devices does not.
"""

import ctypes
import ctypes.util
import struct
import time

PAGE_VENDOR = 0xFF00
USAGE_ACCEL = 3
USAGE_GYRO = 9
SENSOR_NAMES = {USAGE_ACCEL: "accel", USAGE_GYRO: "gyro"}

# Every SPU device we have identified, for probe(). Names in quotes are our
# guesses from the driver's own `model` property; unlabelled ones are unknown.
SPU_DEVICES = {
    (0xFF00, 3): "accelerometer (Bosch BMI286)",
    (0xFF00, 9): "gyroscope (Bosch BMI286)",
    (0xFF00, 4): "ambient light (Redbird VD6286)",
    (0xFF00, 5): "temperature? (TI TMP114)",
    (0x0020, 138): "lid angle (MagAlpha ma781)",
}

REPORT_LEN = 22      # bytes per IMU report
XYZ_OFFSET = 6       # where the three int32 values start
Q16 = 65536.0        # fixed-point scale for both sensors
REPORT_BUF = 4096    # buffer handed to IOKit for incoming reports

CF_UTF8 = 0x08000100
CF_SINT32 = 3
CF_SINT64 = 4


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


def _load():
    """Load IOKit / CoreFoundation / libc and declare the C signatures we use."""
    iokit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit") or "IOKit")
    cf = ctypes.cdll.LoadLibrary(
        ctypes.util.find_library("CoreFoundation") or "CoreFoundation")
    libc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("c") or "libc.dylib")

    iokit.IOServiceMatching.restype = ctypes.c_void_p
    iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
    iokit.IOServiceGetMatchingServices.restype = ctypes.c_int
    iokit.IOServiceGetMatchingServices.argtypes = [
        ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    iokit.IOIteratorNext.restype = ctypes.c_uint
    iokit.IOIteratorNext.argtypes = [ctypes.c_uint]
    iokit.IOObjectRelease.argtypes = [ctypes.c_uint]
    iokit.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
    iokit.IORegistryEntryCreateCFProperty.argtypes = [
        ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    iokit.IORegistryEntrySetCFProperty.restype = ctypes.c_int
    iokit.IORegistryEntrySetCFProperty.argtypes = [
        ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    iokit.IOHIDDeviceCreate.restype = ctypes.c_void_p
    iokit.IOHIDDeviceCreate.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    iokit.IOHIDDeviceOpen.restype = ctypes.c_int
    iokit.IOHIDDeviceOpen.argtypes = [ctypes.c_void_p, ctypes.c_int]
    iokit.IOHIDDeviceRegisterInputReportWithTimeStampCallback.restype = None
    iokit.IOHIDDeviceRegisterInputReportWithTimeStampCallback.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
        ctypes.c_void_p, ctypes.c_void_p]
    iokit.IOHIDDeviceScheduleWithRunLoop.restype = None
    iokit.IOHIDDeviceScheduleWithRunLoop.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]

    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFNumberCreate.restype = ctypes.c_void_p
    cf.CFNumberCreate.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    cf.CFNumberGetValue.restype = ctypes.c_bool
    cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    cf.CFRunLoopRunInMode.restype = ctypes.c_int32
    cf.CFRunLoopRunInMode.argtypes = [
        ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]

    libc.mach_timebase_info.restype = ctypes.c_int
    libc.mach_timebase_info.argtypes = [ctypes.POINTER(_Timebase)]
    return iokit, cf, libc


def _cfstr(cf, s):
    return cf.CFStringCreateWithCString(None, s.encode(), CF_UTF8)


def _int_prop(iokit, cf, svc, key):
    ref = iokit.IORegistryEntryCreateCFProperty(svc, _cfstr(cf, key), None, 0)
    if not ref:
        return None
    v = ctypes.c_long()
    cf.CFNumberGetValue(ref, CF_SINT64, ctypes.byref(v))
    return v.value


def _services(iokit, class_name):
    """Yield every IOKit service of a class. The caller must release each one."""
    it = ctypes.c_uint()
    iokit.IOServiceGetMatchingServices(
        0, iokit.IOServiceMatching(class_name), ctypes.byref(it))
    while True:
        svc = iokit.IOIteratorNext(it.value)
        if not svc:
            return
        yield svc


def list_devices():
    """Return [(usage_page, usage)] for every SPU HID device. No root needed."""
    iokit, cf, _ = _load()
    found = []
    for svc in _services(iokit, b"AppleSPUHIDDevice"):
        page = _int_prop(iokit, cf, svc, "PrimaryUsagePage") or 0
        usage = _int_prop(iokit, cf, svc, "PrimaryUsage") or 0
        found.append((page, usage))
        iokit.IOObjectRelease(svc)
    return found


def has_imu():
    devs = list_devices()
    return ((PAGE_VENDOR, USAGE_ACCEL) in devs
            and (PAGE_VENDOR, USAGE_GYRO) in devs)


def probe(seconds=4.0):
    """Open every SPU sensor and report what it delivers.

    Returns a list of dicts: usage page, usage, our name for it, whether it
    opened, reports per second, and the report lengths seen. Useful for finding
    out which sensors exist on a given Mac, since Apple documents none of them.
    """
    reader = ImuReader(lambda *a: None)
    iokit, cf = reader._iokit, reader._cf
    seen = {}

    def tally(key):
        def cb(_ctx, _result, _sender, _rtype, _rid, report, length, _ts):
            d = seen[key]
            d["reports"] += 1
            d["lengths"].add(length)
            d["last"] = bytes(report[: min(length, 24)]).hex()
        return cb

    for svc in _services(iokit, b"AppleSPUHIDDevice"):
        page = _int_prop(iokit, cf, svc, "PrimaryUsagePage") or 0
        usage = _int_prop(iokit, cf, svc, "PrimaryUsage") or 0
        key = (page, usage)
        seen[key] = {"page": page, "usage": usage, "reports": 0,
                     "lengths": set(), "last": None,
                     "name": SPU_DEVICES.get(key, "unknown"), "opened": False}
        hid = iokit.IOHIDDeviceCreate(reader._alloc, svc)
        if hid and iokit.IOHIDDeviceOpen(hid, 0) == 0:
            seen[key]["opened"] = True
            buf = (ctypes.c_uint8 * REPORT_BUF)()
            cb = reader._callback_proto()(tally(key))
            reader._keepalive += [buf, cb]
            iokit.IOHIDDeviceRegisterInputReportWithTimeStampCallback(
                hid, buf, REPORT_BUF, cb, None)
            iokit.IOHIDDeviceScheduleWithRunLoop(
                hid, cf.CFRunLoopGetCurrent(), reader._mode)
        iokit.IOObjectRelease(svc)

    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        reader.pump(0.25)
    elapsed = max(time.monotonic() - t0, 1e-9)
    for d in seen.values():
        d["hz"] = d["reports"] / elapsed
        d["lengths"] = sorted(d["lengths"])
    return sorted(seen.values(), key=lambda d: (d["page"], d["usage"]))


class ImuReader:
    """Stream accelerometer and gyroscope samples to a callback.

    on_sample(sensor, t, x, y, z) is called for each report:
      sensor: "accel" (x, y, z in g) or "gyro" (in deg/s)
      t:      hardware timestamp in seconds (mach absolute time)

    report_interval_us: how often the driver reports (1000 = ask for 1 kHz).
    keep_every: keep 1 of every N reports. 1 keeps everything.
    """

    def __init__(self, on_sample, report_interval_us=1000, keep_every=1):
        self.on_sample = on_sample
        self.report_interval_us = report_interval_us
        self.keep_every = keep_every
        self.opened = []          # sensor names we managed to open
        self.open_errors = {}     # sensor name -> IOReturn code
        self._keepalive = []      # ctypes objects that must not be garbage collected
        self._counts = {"accel": 0, "gyro": 0}

        self._iokit, self._cf, libc = _load()
        tb = _Timebase()
        libc.mach_timebase_info(ctypes.byref(tb))
        self._to_sec = (tb.numer / tb.denom) * 1e-9
        self._mode = ctypes.c_void_p.in_dll(self._cf, "kCFRunLoopDefaultMode")
        self._alloc = ctypes.c_void_p.in_dll(self._cf, "kCFAllocatorDefault")

    @staticmethod
    def _callback_proto():
        """The C signature macOS calls us back with.

        IOHIDReportWithTimeStampCallback(context, result, sender, type,
                                        reportID, report, length, timestamp)
        """
        return ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_long, ctypes.c_uint64)

    def _make_callback(self, sensor):
        def on_report(_ctx, _result, _sender, _rtype, _rid, report, length, ts):
            if length != REPORT_LEN:
                return
            self._counts[sensor] += 1
            if self._counts[sensor] % self.keep_every:
                return
            raw = bytes(report[:REPORT_LEN])
            x, y, z = struct.unpack_from("<3i", raw, XYZ_OFFSET)
            self.on_sample(sensor, ts * self._to_sec, x / Q16, y / Q16, z / Q16)

        return self._callback_proto()(on_report)

    def _wake_drivers(self):
        for svc in _services(self._iokit, b"AppleSPUHIDDriver"):
            for key, val in (("SensorPropertyReportingState", 1),
                             ("SensorPropertyPowerState", 1),
                             ("ReportInterval", self.report_interval_us)):
                num = ctypes.c_int32(val)
                ref = self._cf.CFNumberCreate(None, CF_SINT32, ctypes.byref(num))
                self._iokit.IORegistryEntrySetCFProperty(
                    svc, _cfstr(self._cf, key), ref)
            self._iokit.IOObjectRelease(svc)

    def start(self):
        """Wake the sensor, open the devices, register callbacks."""
        self._wake_drivers()
        for svc in _services(self._iokit, b"AppleSPUHIDDevice"):
            page = _int_prop(self._iokit, self._cf, svc, "PrimaryUsagePage")
            usage = _int_prop(self._iokit, self._cf, svc, "PrimaryUsage")
            sensor = SENSOR_NAMES.get(usage or 0) if page == PAGE_VENDOR else None
            if sensor:
                hid = self._iokit.IOHIDDeviceCreate(self._alloc, svc)
                kr = self._iokit.IOHIDDeviceOpen(hid, 0) if hid else -1
                if kr == 0:
                    buf = (ctypes.c_uint8 * REPORT_BUF)()
                    cb = self._make_callback(sensor)
                    self._keepalive += [buf, cb]
                    self._iokit.IOHIDDeviceRegisterInputReportWithTimeStampCallback(
                        hid, buf, REPORT_BUF, cb, None)
                    self._iokit.IOHIDDeviceScheduleWithRunLoop(
                        hid, self._cf.CFRunLoopGetCurrent(), self._mode)
                    self.opened.append(sensor)
                else:
                    self.open_errors[sensor] = kr
            self._iokit.IOObjectRelease(svc)
        return self.opened

    def pump(self, seconds=0.25):
        """Let macOS deliver pending reports. Call this in a loop."""
        self._cf.CFRunLoopRunInMode(self._mode, seconds, False)

    def run(self, seconds, should_stop=lambda: False):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not should_stop():
            self.pump(0.25)
