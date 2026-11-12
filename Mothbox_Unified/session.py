#!/usr/bin/python3
"""
session.py -- one word for what the box is doing right now, and the loop that
keeps it in step with the schedule.

THE STATE
---------
/run/mothbox_session holds one word, written only by Scheduler.py:

    booting        Scheduler.py is starting up
    waiting        awake and dark until the next window opens: switched on just
                   before a window, a short gap between windows, or HI POWER
                   between windows
    active         inside a window
    shutting_down  on the way to power-off
    debug          DEBUG / PARTY (cron is stopped; scripts run by hand still work)

Cron's Attract_On.py and TakePhoto.py only do anything in `active` (and in
`debug`, so running them by hand to test still works). Backup_Files.py also
runs in `waiting`. No file at all counts as "not active": /run is emptied at
every boot, so a fresh boot stays dark until the Scheduler says otherwise.

This replaces the boot lock (/run/boot_script_running) and the HI POWER idle
flag (/run/mothbox_hipow_idle). Those were separate flags with separate
writers, and each script combined them its own way. In 6.0.4 one path set the
boot lock and never cleared it, and a box stayed awake for 12 hours taking
nothing.

THE LOOP
--------
run() is what Scheduler.py hands control to once startup is done, in ACTIVE
and in HI POWER mode (HI POWER is the same loop, it just never powers off).
Every few seconds it asks the schedule what the box should be doing and moves
between states. Everything that touches hardware comes in as a callback, so
the tests drive it with fakes.

HEALTH
------
Every 10 minutes the Scheduler checks that photos are actually arriving (see
photos_stalled / stuck_photo_pids). It only ever reports -- an alert on the
display and in the log -- and kills a capture that has hung. It never
reboots: a wrong verdict must not be able to start a reboot loop.
"""
import datetime as _dt
import os
import time

STATE_FILE = "/run/mothbox_session"
HEALTH_FILE = "/run/mothbox_health"     # the health check's display alert; empty = fine

BOOTING = "booting"
WAITING = "waiting"
ACTIVE = "active"
SHUTTING_DOWN = "shutting_down"
DEBUG = "debug"

LIGHTS_AND_PHOTOS = (ACTIVE, DEBUG)     # Attract_On.py, TakePhoto.py
BACKUPS = (ACTIVE, WAITING, DEBUG)      # Backup_Files.py from cron

# The schedule is read this far ahead, so the state is already `active` when
# cron fires at a window's first minute. Anything else that asks "where are we
# in the schedule?" during a session must use the same lookahead, or it can
# disagree with the loop for those few seconds.
LOOKAHEAD_S = 3


# ---------------------------------------------------------------------------
# The state file
# ---------------------------------------------------------------------------
def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text + "\n")
    os.replace(tmp, path)


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


def write_state(state, path=STATE_FILE):
    _write(path, state)


def read_state(path=STATE_FILE):
    """The current state, or None if there is none (or it can't be read)."""
    return _read(path)


def allows(states, path=STATE_FILE):
    """True if the current state is one of `states` (a missing file never is)."""
    return read_state(path) in states


def write_health(alert, path=HEALTH_FILE):
    _write(path, alert or "")


def read_health(path=HEALTH_FILE):
    return _read(path)


# ---------------------------------------------------------------------------
# Health checks (pure: the Scheduler gathers the facts)
# ---------------------------------------------------------------------------
STALL_FLOOR_MIN = 10        # never call photos stopped sooner than this
STUCK_PHOTO_S = 300         # TakePhoto's own watchdog gives up at 150 s


def photos_stalled(now_s, active_since_s, interval_min, last_photo_s):
    """
    True if photos should have been arriving and haven't.

    Allows three photo intervals (and at least 10 minutes) both since the
    window opened and since the last successful photo. A last photo time in
    the future (the clock moved back) counts as recent: TakePhoto re-stamps
    it with the next photo.
    """
    limit = max(3 * max(1, int(interval_min)), STALL_FLOOR_MIN) * 60
    if now_s - active_since_s < limit:
        return False
    if last_photo_s is None:
        return True
    return now_s - last_photo_s >= limit


def health_alert(stalled, camera_ok, storage_low):
    """The display alert for a health check, or None."""
    if not stalled or not camera_ok:
        # A missing camera already has its own CAMERA DISCONNECTED alert
        return None
    if storage_low:
        return "SD FULL - NO PHOTOS"
    return "PHOTOS STOPPED"


def stuck_photo_pids(ps_output, max_age_s=STUCK_PHOTO_S):
    """
    PIDs of TakePhoto.py runs older than max_age_s, from the output of
    `ps -eo pid=,etimes=,args=`.
    """
    pids = []
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or "Mothbox/TakePhoto.py" not in parts[2]:
            continue
        try:
            pid, age = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if age > max_age_s:
            pids.append(pid)
    return pids


# ---------------------------------------------------------------------------
# HI POWER switches
# ---------------------------------------------------------------------------
def hi_power_still_selected(switches):
    """The switch combination that selects HI POWER: Active on, HI on, Debug off."""
    return (int(switches.get("Active", 0)) == 1 and int(switches.get("HI", 0)) == 1
            and int(switches.get("Debug", 0)) == 0)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def _sleep_for(plan, now, lookahead_s, poll_s):
    """Until the next change is due (so a window opens on time), never longer than poll_s."""
    if plan is None:
        return poll_s
    target = plan.session_end if plan.state == "active" else plan.next_start
    if target is None:
        return poll_s
    secs = (target - now).total_seconds() - lookahead_s
    return max(0.5, min(poll_s, secs))


def run(plan_at, now, set_state, on_activate, on_deactivate, power_off,
        keep_awake=False, every_10_min=None, read_switches=None, on_switch_change=None,
        shutdown_enabled=lambda: True, sleep=time.sleep, log=print,
        poll_s=30, lookahead_s=LOOKAHEAD_S, refresh_s=600, switch_check_s=60,
        switch_off_confirm=3, max_polls=None):
    """
    Keep the box in step with the schedule until it powers off.

    plan_at(t)           schedule_core.plan_at for local datetime t
    now()                local datetime
    set_state(s)         write the session state
    on_activate(plan)    a window opened: lights on, display
    on_deactivate(plan)  a window closed and the box stays up: lights off,
                         backup, display (plan says when the next one opens)
    power_off(from_session)
                         everything up to and including `shutdown -h now`;
                         returns "powered_off", or "aborted" if by the time it
                         got there the next window was too close to power off
                         for -- the loop then waits for it.
    keep_awake           HI POWER: never power off between windows
    every_10_min(state)  health check + display refresh
    read_switches(), on_switch_change()
                         HI POWER: leave when the switches stop selecting it --
                         only after switch_off_confirm reads in a row, so one bad
                         I2C read (which falls back to plain ACTIVE) can't do it
    shutdown_enabled()   False once DebugMode.py has been run by hand: leave the
                         box exactly as it is and stop managing it

    The schedule is read lookahead_s ahead, so the state is already `active`
    when cron fires at the window's first minute.

    A callback that raises is logged and the loop carries on: the Scheduler
    dying here would leave the box awake and dark for good. A power_off that
    raises counts as "stayed up" and is simply tried again on the next poll.

    Returns why it stopped: "powered_off", "switch_changed",
    "shutdown_disabled", or "max_polls" (tests).
    """
    def safely(what, fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            log(f"[session] {what} failed: {e}")
            return None

    state = None
    last_refresh = None
    last_switch = None
    off_reads = 0
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        if not shutdown_enabled():
            log("[session] shutdown disabled (DebugMode.py was run) -- leaving the box as it is")
            return "shutdown_disabled"

        try:
            plan = plan_at(now() + _dt.timedelta(seconds=lookahead_s))
        except Exception as e:
            log(f"[session] schedule check failed ({e}) -- keeping the current state")
            plan = None

        if plan is not None and plan.state == "active":
            if state != ACTIVE:
                set_state(ACTIVE)
                state = ACTIVE
                until = plan.session_end.strftime("%a %H:%M") if plan.session_end else "further notice (always on)"
                log(f"[session] window open -- lights and photos on until {until}")
                safely("starting the window", on_activate, plan)
                last_refresh = now()
        elif plan is not None and plan.state == "sleep" and not keep_awake:
            log(f"[session] next window {plan.next_start:%a %H:%M} is too far off to stay up -- powering off")
            set_state(SHUTTING_DOWN)
            if safely("powering off", power_off, state == ACTIVE) == "powered_off":
                return "powered_off"
            set_state(WAITING)
            state = WAITING
            log("[session] not powering off after all -- staying up, dark")
            last_refresh = now()
        elif plan is not None:
            # waiting -- or HI POWER between windows, however long the gap
            if state == ACTIVE:
                set_state(WAITING)
                state = WAITING
                log(f"[session] window closed -- dark until {plan.next_start:%a %H:%M}")
                safely("ending the window", on_deactivate, plan)
                last_refresh = now()
            elif state != WAITING:
                set_state(WAITING)
                state = WAITING
                log(f"[session] waiting, dark, for the window at {plan.next_start:%a %H:%M}")

        t = now()
        if every_10_min is not None:
            if last_refresh is None:
                last_refresh = t
            elif (t - last_refresh).total_seconds() >= refresh_s:
                last_refresh = t
                safely("the health check / display refresh", every_10_min, state)

        if read_switches is not None and (last_switch is None or
                                          (t - last_switch).total_seconds() >= switch_check_s):
            last_switch = t
            try:
                selected = hi_power_still_selected(read_switches())
            except Exception as e:
                log(f"[session] switch read failed ({e}) -- treating as unchanged")
                selected = True
            off_reads = 0 if selected else off_reads + 1
            if off_reads >= switch_off_confirm:
                log("[session] HI POWER switch combination no longer set -- leaving HI POWER")
                safely("leaving HI POWER", on_switch_change)
                return "switch_changed"

        sleep(_sleep_for(plan, now(), lookahead_s, poll_s))
    return "max_polls"
