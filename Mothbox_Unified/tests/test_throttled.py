#!/usr/bin/python3
"""
The throttle report must decode the Pi's flag bits correctly, never crash
Diagnostics when vcgencmd misbehaves, and reach each photo's EXIF block.

    python3 tests/test_throttled.py
"""
import importlib.util, os, subprocess, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location(
    "readThrottled", os.path.join(ROOT, "scripts", "readThrottled.py"))
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)

failures = 0
def check(label, got, want):
    global failures
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"     got:  {got!r}\n     want: {want!r}")
        failures += 1

# --- decoding -------------------------------------------------------------
check("healthy box",
      rt.describe(0x0),
      "throttled: 0x0  (nothing since boot)")
check("under-voltage now and earlier",
      rt.describe(0x50005),
      "throttled: 0x50005  (now: under-voltage, throttled | since boot: under-voltage, throttled)")
check("hot box that has since cooled: history only",
      rt.describe(0xe0000),
      "throttled: 0xe0000  (since boot: freq capped, throttled, soft temp limit)")
check("soft temp limit right now",
      rt.describe(0x80008),
      "throttled: 0x80008  (now: soft temp limit | since boot: soft temp limit)")
check("vcgencmd unavailable",
      rt.describe(None),
      "throttled: unavailable")

# --- reading vcgencmd -----------------------------------------------------
def fake_run(stdout=None, raises=None):
    def run(*args, **kwargs):
        if raises:
            raise raises
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
    return run

real_run = rt.subprocess.run
try:
    rt.subprocess.run = fake_run(stdout="throttled=0x50005\n")
    check("parses vcgencmd output", rt.read_throttled(), 0x50005)
    rt.subprocess.run = fake_run(stdout="VCHI initialization failed\n")
    check("garbage output -> None, no crash", rt.read_throttled(), None)
    rt.subprocess.run = fake_run(raises=FileNotFoundError("vcgencmd"))
    check("no vcgencmd -> None, no crash", rt.read_throttled(), None)
    rt.subprocess.run = fake_run(raises=subprocess.TimeoutExpired("vcgencmd", 10))
    check("vcgencmd hangs -> None, no crash", rt.read_throttled(), None)
finally:
    rt.subprocess.run = real_run

# --- EXIF ----------------------------------------------------------------
# mothbox_exif imports piexif at the top, but the log parser never uses it, so
# stand in an empty module where piexif isn't installed (a dev Mac).
try:
    import piexif  # noqa: F401
except ImportError:
    fake = types.ModuleType("piexif")
    fake.helper = types.ModuleType("piexif.helper")
    sys.modules["piexif"] = fake
    sys.modules["piexif.helper"] = fake.helper
import mothbox_exif

block = """----------------- Mothbox Diagnostics!-------------------
Current time: 2026-10-12 20:00:30
Run label: Shutdown_Check

Vin Voltage: 12.100 V, Current: 1.135 A   [ina219]
5v to pi: 5.2863
throttled: 0x50000  (since boot: under-voltage)
cputemp: 85.9
--end diagnostics------------------------------------
"""
_, values = mothbox_exif._parse_last_run(block)
check("EXIF picks up the flag", values.get("throttled"), "0x50000")
check("EXIF neighbours unaffected",
      (values.get("vin_v"), values.get("current_a"), values.get("pi_5v"), values.get("cpu_temp_c")),
      (12.1, 1.135, 5.2863, 85.9))
_, values = mothbox_exif._parse_last_run(
    block.replace("throttled: 0x50000  (since boot: under-voltage)", "throttled: unavailable"))
check("EXIF leaves the key out when unavailable", "throttled" in values, False)

if failures:
    print(f"\n{failures} check(s) FAILED")
    sys.exit(1)
print("\nALL CHECKS PASSED")
