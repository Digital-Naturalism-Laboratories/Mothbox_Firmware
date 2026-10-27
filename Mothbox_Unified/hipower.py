#!/usr/bin/python3
"""
hipower.py -- HI POWER mode: stay awake and connected, but only attract and
photograph during the scheduled windows.

In ACTIVE mode the box sleeps between windows: outside a window it blinks,
arms the RTC alarm and powers off. HI POWER (the Pro's HI switch, with Debug
off) never powers off and keeps wifi up, so later it can upload images or
process them on board. The lights and the camera still follow the schedule.

How the schedule is enforced while the box stays up
---------------------------------------------------
cron keeps firing Attract_On.py and TakePhoto.py every minute, exactly as in
a normal session. Outside a window the Scheduler creates IDLE_FLAG in /run,
and both scripts exit early when it exists -- the same pattern as the boot
lock. /run is a tmpfs, so a stale flag can never survive a reboot.

run() is the loop Scheduler.py hands control to. All hardware and schedule
access comes in through callbacks so tests can drive it with fakes.
"""
import os
import time

IDLE_FLAG = "/run/mothbox_hipow_idle"            # outside a window: no lights, no photos


def set_idle(idle, flag=IDLE_FLAG):
    """Create (idle=True) or remove (idle=False) the outside-the-window flag."""
    if idle:
        with open(flag, "w") as f:
            f.write("hi power: outside scheduled window\n")
    else:
        try:
            os.remove(flag)
        except FileNotFoundError:
            pass


def is_idle(flag=IDLE_FLAG):
    return os.path.exists(flag)


def hi_power_still_selected(switches):
    """The switch combination that selects HI POWER: Active on, HI on, Debug off."""
    return (int(switches.get("Active", 0)) == 1 and int(switches.get("HI", 0)) == 1
            and int(switches.get("Debug", 0)) == 0)


def run(in_window, read_switches, on_window_start, on_window_end, on_switch_change,
        start_in_window, poll_s=30, switch_check_s=60, switch_off_confirm=3,
        sleep=time.sleep, log=print, max_polls=None):
    """
    Stay in HI POWER until the switches say otherwise.

    in_window()           -> bool, is now inside a scheduled window
    read_switches()       -> dict of switch states (mothbox_hw.read_switches)
    on_window_start()     -> a window just began
    on_window_end()       -> a window just ended
    on_switch_change()    -> HI POWER is no longer selected; caller reboots
    start_in_window       -> the state the Scheduler already set up at boot

    Switches are re-read every switch_check_s and must read "not HI POWER"
    switch_off_confirm times in a row before we leave, so one bad I2C read
    (which falls back to plain ACTIVE) cannot knock a box out of HI POWER.
    max_polls only exists for tests.
    """
    state = start_in_window
    off_reads = 0
    since_switch_check = 0
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        try:
            now_in = bool(in_window())
        except Exception as e:
            log(f"[hipower] schedule check failed ({e}) -- keeping the current state")
            now_in = state
        if now_in and not state:
            log("[hipower] scheduled window started -- lights and photos on")
            on_window_start()
        elif state and not now_in:
            log("[hipower] scheduled window ended -- lights and photos off, staying awake")
            on_window_end()
        state = now_in

        since_switch_check += poll_s
        if since_switch_check >= switch_check_s:
            since_switch_check = 0
            try:
                selected = hi_power_still_selected(read_switches())
            except Exception as e:
                log(f"[hipower] switch read failed ({e}) -- treating as unchanged")
                selected = True
            off_reads = 0 if selected else off_reads + 1
            if off_reads >= switch_off_confirm:
                log("[hipower] HI POWER switch combination no longer set -- leaving HI POWER")
                on_switch_change()
                return "switch_changed"
        sleep(poll_s)
    return "max_polls"
