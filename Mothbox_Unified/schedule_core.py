#!/usr/bin/python3
"""
schedule_core.py -- what the schedule means, in one place.

The settings give START times (weekday x hour x minute) and a runtime. This
module turns them into real windows of activity, [start, end) in local time,
and answers the questions the rest of the firmware asks:

    parse(raw)               validate the settings; bad values fall back to defaults
    plan_at(schedule, now)   what should the box be doing right now, and until when?
    next_window_start(...)   when does the next window open (for the wake alarm)?

Before 6.1, three separate pieces of code answered those questions
(is_now_in_schedule, should_abort_shutdown, and python-crontab for the wake
alarm) and they disagreed in edge cases. Worse, a session ran for `runtime`
minutes from whenever the box booted, so a box switched on mid-window ran
straight past the end of it (Nov 2026: switched on at 12:07 on a 00:00 +
1400 min schedule, it ran until 11:27 the next day, through a gap that should
have been dark).

The rules
---------
* A session ends at the end of its window -- never at boot + runtime.
* Windows less than MERGE_GAP_MIN apart merge into one continuous session, so
  hourly starts with a 59 or 60 minute runtime simply keep going.
* If the next window opens within STAY_AWAKE_MIN, the box stays up and dark
  instead of powering off. A shutdown plus a boot costs about as much power,
  and a wake alarm that close could fire while the box is still shutting down.
* Otherwise it powers off and wakes EARLY_WAKE_MIN before the next window, so
  it has booted and is waiting when the window opens.

Everything is naive local time -- datetime.now(), the same clock cron and the
display use (TimezoneUpdater points /etc/localtime at the user's timezone).
This module is pure: no files, no hardware, so tests can drive it with any
`now` they like.
"""
import datetime as dt
import time
from collections import namedtuple

MERGE_GAP_MIN = 5          # windows closer than this merge into one session
STAY_AWAKE_MIN = 8         # next window within this -> stay up and dark, don't power off
EARLY_WAKE_MIN = 2         # the wake alarm fires this long before a window opens
DEFAULT_RUNTIME_MIN = 60   # used when runtime is missing, 0, negative or not a number
MAX_RUNTIME_MIN = 7 * 24 * 60   # anything longer than a week is "always on" anyway
LOOKAHEAD_DAYS = 8         # schedules repeat weekly, so 8 days always holds the next window

# What a setting falls back to when it can't be used (the shipped CSV's values)
DEFAULTS = {"weekday": "1;2;3;4;5;6;7", "hour": "19;21;23;2;4", "minute": "0"}

ACTIVE, WAITING, SLEEP = "active", "waiting", "sleep"

# weekdays: Python numbering, Monday=0 ... Sunday=6
# errors:   names of the settings that were unusable and fell back to defaults
Schedule = namedtuple("Schedule", "weekdays hours minutes runtime_min errors")

# state:         ACTIVE  -- inside a window; lights and photos
#                WAITING -- awake and dark; the next window opens within STAY_AWAKE_MIN
#                SLEEP   -- power off and wake for next_start
# session_end:   end of the current (merged) window; None if it never ends (always on)
# next_start:    start of the next window (WAITING / SLEEP); None while ACTIVE
Plan = namedtuple("Plan", "state session_end next_start")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _int_list(value, lo, hi):
    """
    '19;21;23' -> [19, 21, 23], or None if the value can't be used.

    Semicolons are the documented separator. Commas are accepted too only
    because the Scheduler itself turns ';' into ',' internally and the switch
    schedule is built with commas -- they are not a format users should write.
    Anything else that isn't a whole number in [lo, hi] makes the whole
    setting unusable, e.g. '19 21' (a space where the ';' should be).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parts = [str(value)]
    elif isinstance(value, str):
        parts = [p.strip() for p in value.replace(",", ";").split(";")]
        parts = [p for p in parts if p]
    else:
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if not nums or any(not lo <= n <= hi for n in nums):
        return None
    return sorted(set(nums))


def _runtime(value):
    """Minutes as a positive whole number, capped at a week; None if unusable."""
    if isinstance(value, bool):
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return min(n, MAX_RUNTIME_MIN)


def parse(raw):
    """
    raw: a dict with 'weekday', 'hour', 'minute', 'runtime' -- straight from
    the CSV (strings) or the switch schedule. Each unusable setting falls back
    to its default on its own and is named in Schedule.errors, so the box
    still runs and the display can say exactly which setting to fix.
    """
    errors = []

    def setting(name, lo, hi):
        got = _int_list(raw.get(name), lo, hi)
        if got is None:
            errors.append(name)
            got = _int_list(DEFAULTS[name], lo, hi)
        return got

    days = setting("weekday", 0, 7)          # 1=Monday ... 7=Sunday (0 is Sunday too, as in cron)
    hours = setting("hour", 0, 23)
    minutes = setting("minute", 0, 59)
    runtime = _runtime(raw.get("runtime"))
    if runtime is None:
        errors.append("runtime")
        runtime = DEFAULT_RUNTIME_MIN
    weekdays = sorted({(d - 1) % 7 for d in days})
    return Schedule(weekdays, hours, minutes, runtime, errors)


def as_settings(sched):
    """The schedule actually in use, written back in the CSV's own format."""
    return {
        "weekday": ";".join(str(d + 1) for d in sched.weekdays),
        "hour": ";".join(str(h) for h in sched.hours),
        "minute": ";".join(str(m) for m in sched.minutes),
        "runtime": sched.runtime_min,
    }


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
def windows(sched, frm, to):
    """
    The merged [start, end) windows that overlap frm..to, in order.

    Raw windows (one per start time) are generated from far enough back that
    a long window which began before `frm` is included, then merged wherever
    two are less than MERGE_GAP_MIN apart -- before any filtering, so a chain
    of touching windows always merges completely.
    """
    run = dt.timedelta(minutes=sched.runtime_min)
    gap = dt.timedelta(minutes=MERGE_GAP_MIN)
    day = (frm - run - gap).date() - dt.timedelta(days=1)
    raw = []
    while day <= to.date():
        if day.weekday() in sched.weekdays:
            for h in sched.hours:
                for m in sched.minutes:
                    start = dt.datetime.combine(day, dt.time(h, m))
                    raw.append((start, start + run))
        day += dt.timedelta(days=1)
    raw.sort()
    merged = []
    for start, end in raw:
        if merged and start - merged[-1][1] < gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged if e > frm and s < to]


def plan_at(sched, now):
    """What the box should be doing at `now`. See Plan."""
    horizon = now + dt.timedelta(days=LOOKAHEAD_DAYS)
    for start, end in windows(sched, now, horizon + dt.timedelta(minutes=sched.runtime_min)):
        if start <= now < end:
            # No gap anywhere in the next 8 days means no gap ever: the
            # schedule repeats every week.
            return Plan(ACTIVE, None if end >= horizon else end, None)
        if start > now:
            if start - now <= dt.timedelta(minutes=STAY_AWAKE_MIN):
                return Plan(WAITING, None, start)
            return Plan(SLEEP, None, start)
    return Plan(SLEEP, None, None)          # unreachable with a parsed schedule


def next_window_start(sched, now):
    """
    Start of the first window that opens after `now` -- the window after the
    current one if `now` is inside a window. None if the box is always on.
    """
    horizon = now + dt.timedelta(days=LOOKAHEAD_DAYS)
    for start, _end in windows(sched, now, horizon + dt.timedelta(minutes=sched.runtime_min)):
        if start > now:
            return start
    return None


def wake_time(window_start):
    """When the wake alarm should fire for a window opening at window_start."""
    return window_start - dt.timedelta(minutes=EARLY_WAKE_MIN)


def to_epoch(local):
    """Naive local datetime -> unix time, using the system timezone (DST-aware)."""
    return int(time.mktime(local.timetuple()))


def describe(sched, now, hours=24):
    """One line for the log: the real windows in the next `hours` hours."""
    upto = now + dt.timedelta(hours=hours)
    horizon = now + dt.timedelta(days=LOOKAHEAD_DAYS)
    parts = []
    for start, end in windows(sched, now, horizon + dt.timedelta(minutes=sched.runtime_min)):
        if start >= upto:
            break
        if end >= horizon:
            parts.append("always on (the windows never leave a gap)")
            break
        parts.append(f"{start:%a %H:%M}-{end:%H:%M}")
    return ", ".join(parts) if parts else "none"
