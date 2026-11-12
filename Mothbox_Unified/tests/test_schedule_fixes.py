#!/usr/bin/python3
"""
6.0.5 scheduling / photo-timing fixes. Runs the REAL functions, lifted out of
Scheduler.py and TakePhoto.py (both are top-level scripts that touch hardware,
so they can't simply be imported).

    python3 tests/test_schedule_fixes.py
"""
import ast, os, re, sys, tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

failures = 0
def check(label, got, want):
    global failures
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"     got:  {got!r}\n     want: {want!r}")
        failures += 1

def source(name):
    return open(os.path.join(ROOT, name)).read()

def lift(name, funcs, env):
    """exec just the named top-level functions of a script into env."""
    tree = ast.parse(source(name))
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in funcs]
    assert {n.name for n in body} == set(funcs), f"missing from {name}: {set(funcs) - {n.name for n in body}}"
    exec(compile(ast.Module(body=body, type_ignores=[]), name, "exec"), env)
    return env

def function_source(name, func):
    tree = ast.parse(source(name))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func)
    return ast.get_source_segment(source(name), node)

# --- 1. powering off: the wake alarm is the LAST thing before the halt -------------
# 6.0.x armed the wake alarm first and then ran a final backup that can take
# 15 min: with a short gap the alarm fired while the box was still on, was
# used up, and the box powered off with nothing to wake it. (The session state
# that replaced the boot lock is tested in test_session.py; what the windows
# mean -- including the field case -- in test_schedule_core.py.)
body = function_source("Scheduler.py", "power_off")
halt = body.find('os.system("sudo shutdown -h now")')
last_alarm = body.rfind("set_wakeup_alarm(", 0, halt)
check("power_off: the real wake alarm is set after the final backup, display and diagnostics",
      0 <= max(body.find("final_backup_before_shutdown()"), body.find("UpdateDisplay.py"),
               body.find("Diagnostics.py")) < last_alarm < halt, True)
between = body[last_alarm:halt]
check("power_off: nothing slow runs between setting that alarm and the halt",
      [w for w in ("run_helper", "run_script", "subprocess", "final_backup", "sleep(") if w in between], [])
first_alarm = body.find("set_wakeup_alarm(")
check("power_off: the alarm set first is only the deadman's fallback, kept off the display",
      ("record=False" in body[first_alarm:body.find(")", first_alarm) + 1],
       first_alarm < body.find("arm_shutdown_deadman()")), (True, True))
check("power_off: staying up after all disarms the deadman",
      "disarm_shutdown_deadman(deadman)" in body[body.find('return "aborted"') - 600:body.find('return "aborted"')], True)
for gone in ("run_shutdown_pi5", "should_abort_shutdown", "is_now_in_schedule", "calculate_next_event",
             "schedule_shutdown", "BOOT_LOCK"):
    check(f"Scheduler.py: {gone} is gone (one schedule, one session state)", gone in source("Scheduler.py"), False)

# --- 2 + 3. photo interval -------------------------------------------------
class Clock:
    t = 1_800_000_000.0
    @classmethod
    def time(cls):
        return cls.t
state = Path(tempfile.mkdtemp())
(state / "photo_interval.txt").write_text("photo_interval=2\n")
last = state / "last_photo_time.txt"
tp = lift("TakePhoto.py", ["get_photo_interval", "is_photo_due", "record_photo_taken"],
          {"CONTROL_ROOT": state, "LAST_PHOTO_FILE": last, "os": os, "time": Clock,
           "print": lambda *a, **k: None})

def due_with_last(offset_s):
    last.write_text(str(Clock.t + offset_s))
    return tp["is_photo_due"]()
check("no photo yet -> due", (last.unlink() if last.exists() else None, tp["is_photo_due"]())[1], True)
check("last photo 80 s ago -> not due", due_with_last(-80), False)
check("last photo 3 min ago -> due", due_with_last(-180), True)
check("last photo stamped a day in the FUTURE (clock moved back) -> due", due_with_last(86400), True)
check("a few seconds of clock skew is not 'the future' -> still not due", due_with_last(20), False)
check("the stamp is the start time it is given",
      (tp["record_photo_taken"](1234.5), float(last.read_text()))[1], 1234.5)

# photo_interval=2 with a 40 s capture, cron firing every minute for 10 minutes
def photos_in_10_min(capture_s):
    if last.exists():
        last.unlink()
    taken, start = 0, Clock.t
    for minute in range(10):
        Clock.t = start + 60 * minute
        if tp["is_photo_due"]():
            started = Clock.t
            Clock.t += capture_s                   # the photo takes a while...
            tp["record_photo_taken"](started)      # ...and is stamped when it STARTED
            taken += 1
    Clock.t = start
    return taken
check("photo_interval=2 with 40 s captures really gives a photo every 2 min", photos_in_10_min(40), 5)

for name in ("TakePhoto.py",):
    check(f"{name} stamps the start time, not the finish",
          "record_photo_taken(PHOTO_STARTED)" in source(name), True)

# --- 4. the shipped safetygb file is read by the key the scripts ask for -------
shipped = Path(ROOT).parent / "mothbox_custom_Unified" / "system" / "controls" / "safetygb.txt"
key = shipped.read_text().split("=", 1)[0].strip()
for name in ("TakePhoto.py", "Backup_Files.py"):
    asked = re.search(r'"safetygb\.txt",\s*"(\w+)"', source(name)).group(1)
    check(f"{name} asks for the key safetygb.txt actually contains", asked, key)

if failures:
    print(f"\n{failures} check(s) FAILED")
    sys.exit(1)
print("\nALL CHECKS PASSED")
