#!/usr/bin/python3
"""
HI POWER must follow the schedule for lights and photos, never leave on a
single bad switch read, and leave cleanly when the HI switch really is off.

    python3 tests/test_hipower.py
"""
import os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import hipower

failures = 0
def check(label, got, want):
    global failures
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"     got:  {got!r}\n     want: {want!r}")
        failures += 1

HI_ON  = {"Active": 1, "HI": 1, "Debug": 0}
HI_OFF = {"Active": 1, "HI": 0, "Debug": 0}

def drive(windows, switch_reads, start_in_window=False, switch_off_confirm=3):
    """One poll per entry in `windows`; switches are read every poll."""
    events = []
    win = iter(windows)
    sw = iter(switch_reads)
    result = hipower.run(
        in_window=lambda: next(win),
        read_switches=lambda: next(sw, HI_ON),
        on_window_start=lambda: events.append("start"),
        on_window_end=lambda: events.append("end"),
        on_switch_change=lambda: events.append("leave"),
        start_in_window=start_in_window,
        poll_s=1, switch_check_s=1, switch_off_confirm=switch_off_confirm,
        sleep=lambda s: None, log=lambda m: None, max_polls=len(windows),
    )
    return events, result

# --- schedule ---
events, _ = drive([False, True, True, False, False, True], [])
check("lights/photos follow the windows across a whole day", events, ["start", "end", "start"])
events, _ = drive([True, True, False], [], start_in_window=True)
check("booted inside a window: no duplicate start, one end", events, ["end"])
events, _ = drive([False] * 5, [])
check("outside the schedule all day: nothing switches on", events, [])

def boom():
    raise RuntimeError("clock")
ev = []
hipower.run(in_window=boom, read_switches=lambda: HI_ON,
            on_window_start=lambda: ev.append("start"), on_window_end=lambda: ev.append("end"),
            on_switch_change=lambda: ev.append("leave"), start_in_window=True,
            poll_s=1, switch_check_s=99, sleep=lambda s: None, log=lambda m: None, max_polls=3)
check("a failing schedule check keeps the current state", ev, [])

# --- switches ---
events, result = drive([True] * 6, [HI_OFF, HI_ON, HI_OFF, HI_OFF, HI_ON], start_in_window=True)
check("isolated 'HI off' reads (flaky I2C) never leave HI POWER", (events, result), ([], "max_polls"))
events, result = drive([True] * 6, [HI_OFF, HI_OFF, HI_OFF], start_in_window=True)
check("three 'HI off' reads in a row leave HI POWER", (events, result), (["leave"], "switch_changed"))
events, _ = drive([True] * 6, [{"Active": 1, "HI": 1, "Debug": 1}] * 3, start_in_window=True)
check("Debug switched on also leaves HI POWER (DEBUG wins)", events, ["leave"])
events, _ = drive([True] * 6, [{"Active": 0, "HI": 1, "Debug": 0}] * 3, start_in_window=True)
check("Active switched off also leaves HI POWER", events, ["leave"])
check("fail-safe read (Active only) counts as 'not HI'",
      hipower.hi_power_still_selected({"Active": 1}), False)

def bad_read():
    raise OSError("i2c")
ev = []
r = hipower.run(in_window=lambda: True, read_switches=bad_read,
                on_window_start=lambda: None, on_window_end=lambda: None,
                on_switch_change=lambda: ev.append("leave"), start_in_window=True,
                poll_s=1, switch_check_s=1, sleep=lambda s: None, log=lambda m: None, max_polls=6)
check("switch read exceptions never leave HI POWER", (ev, r), ([], "max_polls"))

# --- idle flag ---
with tempfile.TemporaryDirectory() as d:
    flag = os.path.join(d, "idle")
    hipower.set_idle(True, flag)
    check("set_idle(True) creates the flag", os.path.exists(flag), True)
    hipower.set_idle(False, flag)
    check("set_idle(False) removes it", os.path.exists(flag), False)
    hipower.set_idle(False, flag)
    check("removing a missing flag is harmless", os.path.exists(flag), False)

# --- the cron scripts honour the flag before doing any work ---
for script, marker in (("Attract_On.py", "mothbox_hw.attract_on()"), ("TakePhoto.py", "from picamera2")):
    src = open(os.path.join(ROOT, script)).read()
    check(f"{script} exits on the idle flag before touching hardware",
          0 <= src.find(hipower.IDLE_FLAG) < src.find(marker), True)

if failures:
    print(f"\n{failures} check(s) FAILED")
    sys.exit(1)
print("\nALL CHECKS PASSED")
