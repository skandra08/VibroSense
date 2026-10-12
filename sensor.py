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

    def _make_callback(self, sensor):
        # IOHIDReportWithTimeStampCallback(context, result, sender, type,
        #                                  reportID, report, length, timestamp)
        proto = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_long, ctypes.c_uint64)

        def on_report(_ctx, _result, _sender, _rtype, _rid, report, length, ts):
            if length != REPORT_LEN:
                return
            self._counts[sensor] += 1
            if self._counts[sensor] % self.keep_every:
                return
            raw = bytes(report[:REPORT_LEN])
            x, y, z = struct.unpack_from("<3i", raw, XYZ_OFFSET)
            self.on_sample(sensor, ts * self._to_sec, x / Q16, y / Q16, z / Q16)

        return proto(on_report)

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
