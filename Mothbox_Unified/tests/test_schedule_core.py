#!/usr/bin/python3
"""
The schedule has to mean the same thing everywhere: real windows of activity,
merged when they touch, ending at their own end -- never boot + runtime. These
are the field scenarios that used to go wrong, plus the settings typos that
used to crash the Scheduler at boot.

    python3 tests/test_schedule_core.py
"""
import datetime as dt
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import schedule_core as sc

failures = 0
def check(label, got, want):
    global failures
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"     got:  {got!r}\n     want: {want!r}")
        failures += 1

def T(day, h, m=0, month=11):
    return dt.datetime(2026, month, day, h, m)

def sched(hour, runtime, minute="0", weekday="1;2;3;4;5;6;7"):
    s = sc.parse({"weekday": weekday, "hour": hour, "minute": minute, "runtime": runtime})
    assert not s.errors, s.errors
    return s

A, W, S = sc.ACTIVE, sc.WAITING, sc.SLEEP

# --- the field case: 00:00 + 1400 min every day, switched on at 12:07 --------
friend = sched("0", 1400)
check("switched on at 12:07 -> active until 23:20 the SAME day (not 11:27 the next)",
      sc.plan_at(friend, T(5, 12, 7)), (A, T(5, 23, 20), None))
check("at 23:20 the 40-min gap is too long to stay up -> sleep, next window 00:00",
      sc.plan_at(friend, T(5, 23, 20)), (S, None, T(6, 0)))
check("...and the alarm wakes it 2 min early, at 23:58",
      sc.wake_time(sc.plan_at(friend, T(5, 23, 20)).next_start), T(5, 23, 58))
check("woken at 23:58 -> waiting, dark, for the 00:00 start",
      sc.plan_at(friend, T(5, 23, 58)), (W, None, T(6, 0)))

# --- back-to-back hours ----------------------------------------------------------
check("19;20;21;22 x 59 min: 1-min gaps merge -> one session 19:00-22:59",
      sc.plan_at(sched("19;20;21;22", 59), T(5, 20, 30)), (A, T(5, 22, 59), None))
check("19;20;21;22 x 60 min: touching windows merge -> one session 19:00-23:00",
      sc.plan_at(sched("19;20;21;22", 60), T(5, 19, 1)), (A, T(5, 23, 0), None))
h55 = sched("19;20;21;22", 55)
check("x 55 min: a 5-min gap does NOT merge -> the session ends at 19:55",
      sc.plan_at(h55, T(5, 19, 30)), (A, T(5, 19, 55), None))
check("...and the box stays up, dark, through the 5-min gap",
      sc.plan_at(h55, T(5, 19, 55)), (W, None, T(5, 20, 0)))
check("...then the next window starts on time",
      sc.plan_at(h55, T(5, 20, 0)), (A, T(5, 20, 55), None))
check("an 8-min gap still stays up", sc.plan_at(sched("19;20", 52), T(5, 19, 52)).state, W)
check("a 9-min gap powers off", sc.plan_at(sched("19;20", 51), T(5, 19, 51)).state, S)

# --- the shipped default: 19;21;23;2;4 ------------------------------------------------
default = sched("19;21;23;2;4", 60)
check("default schedule, 20:00 -> sleep until 21:00", sc.plan_at(default, T(5, 20, 0)), (S, None, T(5, 21)))
check("default schedule, 23:30 -> active until midnight", sc.plan_at(default, T(5, 23, 30)), (A, T(6, 0), None))
check("default schedule, 00:00 -> sleep until 02:00", sc.plan_at(default, T(6, 0, 0)), (S, None, T(6, 2)))
check("23 and 0 x 60 min merge across midnight -> 23:00-01:00",
      sc.plan_at(sched("23;0", 60), T(5, 23, 59)), (A, T(6, 1, 0), None))

# --- days of the week (Nov 2026: the 8th is a Sunday, the 9th a Monday) ----------------------
check("(test calendar) 8 Nov 2026 is a Sunday", T(8, 0).weekday(), 6)
sunday_late = sched("23", 120, weekday="7")
check("Sunday 23:00 + 2 h is still running at Monday 00:30",
      sc.plan_at(sunday_late, T(9, 0, 30)), (A, T(9, 1, 0), None))
check("...but nothing on Monday night: sleep until next Sunday 23:00",
      sc.plan_at(sunday_late, T(9, 23, 30)), (S, None, T(15, 23)))
check("weekday 0 is Sunday too (as in cron)", sc.parse({"weekday": "0", "hour": "1", "minute": "0", "runtime": 5}).weekdays, [6])
mon_long = sched("0", 1500, weekday="1")
check("Monday 00:00 + 25 h -> still active Tuesday 00:30, ends 01:00",
      sc.plan_at(mon_long, T(10, 0, 30)), (A, T(10, 1, 0), None))

# --- schedules that never leave a gap -----------------------------------------------
check("1440 min every day -> always on (no session end)", sc.plan_at(sched("0", 1440), T(5, 12)), (A, None, None))
check("every hour x 60 min -> always on", sc.plan_at(sched(";".join(map(str, range(24))), 60), T(5, 3, 3)), (A, None, None))
check("always on -> no next window to wake for", sc.next_window_start(sched("0", 1440), T(5, 12)), None)
check("describe() says so", "always on" in sc.describe(sched("0", 1440), T(5, 12)), True)
check("a 1439-min day leaves a 1-min gap, which merges -> always on",
      sc.plan_at(sched("0", 1439), T(5, 12)), (A, None, None))
check("a 1435-min day leaves a 5-min gap -> ends 23:55, then waits dark until 00:00",
      (sc.plan_at(sched("0", 1435), T(5, 12)), sc.plan_at(sched("0", 1435), T(5, 23, 55))),
      ((A, T(5, 23, 55), None), (W, None, T(6, 0))))

# --- switching the box on at awkward moments -----------------------------------------
check("4 min before a window -> waiting until it opens", sc.plan_at(default, T(5, 18, 56)), (W, None, T(5, 19)))
check("8 min before -> still waiting", sc.plan_at(default, T(5, 18, 52)).state, W)
check("9 min before -> sleep (and wake at 18:58)",
      (sc.plan_at(default, T(5, 18, 51)).state, sc.wake_time(sc.plan_at(default, T(5, 18, 51)).next_start)),
      (S, T(5, 18, 58)))
check("in the last minute of a window -> active until it ends",
      sc.plan_at(default, T(5, 19, 59)), (A, T(5, 20, 0), None))
check("exactly at a window's end it is over", sc.plan_at(default, T(5, 20, 0)).state, S)
check("exactly at a window's start it is on", sc.plan_at(default, T(5, 21, 0)).state, A)
check("two start minutes an hour (0;30 x 20 min) -> 19:30-19:50 is its own window",
      sc.plan_at(sched("19", 20, minute="0;30"), T(5, 19, 35)), (A, T(5, 19, 50), None))
check("next window after the current one, for the safety wake alarm",
      sc.next_window_start(default, T(5, 19, 30)), T(5, 21, 0))

# --- settings validation: a typo must never crash the box -------------------------------
def p(**kw):
    raw = {"weekday": "1;2;3;4;5;6;7", "hour": "19;21", "minute": "0", "runtime": 60}
    raw.update(kw)
    return sc.parse(raw)
check("'19 21' (space instead of ;) -> hour error, default hours used",
      (p(hour="19 21").errors, p(hour="19 21").hours), (["hour"], [2, 4, 19, 21, 23]))
for bad in (0, -5, "", None, "abc", "59.5", True):
    check(f"runtime {bad!r} -> runtime error, 60 min", (p(runtime=bad).errors, p(runtime=bad).runtime_min), (["runtime"], 60))
check("a missing runtime row -> runtime error, 60 min", sc.parse({"weekday": "1", "hour": "1", "minute": "0"}).runtime_min, 60)
check("runtime '1400' as text is fine", (p(runtime="1400").errors, p(runtime="1400").runtime_min), ([], 1400))
check("runtime of a month is capped at a week", p(runtime=50000).runtime_min, sc.MAX_RUNTIME_MIN)
check("minute 75 -> minute error", p(minute="75").errors, ["minute"])
check("hour 24 -> hour error", p(hour="22;24").errors, ["hour"])
check("weekday 8 -> weekday error", p(weekday="8").errors, ["weekday"])
check("an empty hour cell -> hour error", p(hour="").errors, ["hour"])
check("several bad settings are all named", p(hour="x", runtime=0).errors, ["hour", "runtime"])
check("trailing ';' and spaces after ';' are fine", (p(hour="19; 21;").errors, p(hour="19; 21;").hours), ([], [19, 21]))
check("commas (the Scheduler's own internal form) are fine", p(hour="19,21").hours, [19, 21])
check("hour given as the number 0 is fine", p(hour=0).hours, [0])
check("a valid schedule has no errors", p().errors, [])
check("as_settings writes the schedule in use back in CSV form",
      sc.as_settings(p(hour="21;19", weekday="7;1")),
      {"weekday": "1;7", "hour": "19;21", "minute": "0", "runtime": 60})

# --- unix time ----------------------------------------------------------------------
check("to_epoch round-trips through the local clock",
      dt.datetime.fromtimestamp(sc.to_epoch(T(5, 21, 0))), T(5, 21, 0))

if failures:
    print(f"\n{failures} check(s) FAILED")
    sys.exit(1)
print("\nALL CHECKS PASSED")
