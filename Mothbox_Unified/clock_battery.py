#!/usr/bin/python3
"""
clock_battery.py -- look after the Pi 5's RTC coin cell.

WHY IT MATTERS
--------------
Every wake-up hinges on the real-time clock. Whenever the main battery is
unplugged or runs flat -- for weeks or months between deployments -- only the
coin cell keeps that clock running. If it dies the box wakes at the wrong time
or not at all, and in the field there is usually no wifi to fix the clock.

TWO KINDS OF CELL, OPPOSITE NEEDS
---------------------------------
    rechargeable (ML2020 / ML2032, the official Pi battery)
        must be trickle-charged, or each unplugged spell drains it a bit more
        until the clock dies
    regular (CR2032 and other non-rechargeable lithium cells)
        must NEVER be charged -- a charged primary cell can swell, leak or vent
        inside the box. Raspberry Pi: "We don't recommend using a primary
        (non-rechargeable) lithium cell"

So the setting `rtc_battery` in mothbox_settings.csv says which is fitted.

HOW CHARGING IS CONTROLLED (measured on a Pi 5, Nov 2026)
---------------------------------------------------------
- /sys/class/rtc/rtc0/charging_voltage is read-only, but it reads the live
  firmware register every time, so it is a trustworthy readback.
- The kernel sets the register at every boot from config.txt
  (dtparam=rtc_bbat_vchg=...; absent = charging off).
- The same firmware call the kernel uses can be made from userspace:
      vcmailbox 0x00038087 8 8 4 <microvolts>      (SET_RTC_REG, reg 4)
  and it takes effect immediately.

The image ships with that dtparam REMOVED, so every boot starts with charging
off -- safe for either cell. This script then turns charging on only for a box
whose setting says `rechargeable`. A regular cell can only be charged if
someone writes "rechargeable" for it; nothing else turns charging on. Nothing
here edits config.txt (FAT, see mothbox_paths.py) and nothing reboots.

MEASURING THE CELL
------------------
battery_voltage only means something with charging OFF. With charging on it
reads the charger's own output: a box with its cell unplugged read 3.005 V.
With charging off it reads the real cell (2.95 V), or ~0 V with none plugged
in. So each boot: charging off, wait for the reading to settle, read, then set
charging as the setting says. That also catches a missing or broken cell on a
box that normally charges.

THE CLOCK ITSELF
----------------
If the RTC holds a date before this firmware was built, it lost power: the
cell is dead or missing. The schedule then runs on a wrong clock. That is
flagged as CLOCK LOST until the time is set again (NTP or manualTime).
"""
import datetime as _dt
import os
import subprocess
import time

RTC_DIR = "/sys/class/rtc/rtc0"

RECHARGEABLE = "rechargeable"
REGULAR = "regular"
CHARGE_UV = 3_000_000           # what Raspberry Pi specifies for its ML2020 cell

SET_RTC_REG_TAG = "0x00038087"
REG_CHARGE_VOLTS = 4

# Thresholds (volts, read with charging OFF)
NO_CELL_V = 0.5                 # unplugged read 0.0017 V
LOW_V = 2.7                     # healthy cells read ~2.95-3.3 V
# A rechargeable cell charged here sits at or below 3.0 V. A reading this far
# above it is a fresh regular cell -- don't charge, say so.
LOOKS_REGULAR_V = 3.15

SETTLE_MIN_S = 2.0              # first reading after charging goes off
SETTLE_MAX_S = 15.0
SETTLE_STEP_S = 1.0
SETTLE_TOL_V = 0.02             # two readings this close = settled

# The RTC can't legitimately be earlier than this (the firmware didn't exist).
CLOCK_FLOOR = _dt.datetime(2026, 11, 1, tzinfo=_dt.timezone.utc)

ALERT_NO_CELL = "NO CLOCK BATTERY"
ALERT_LOW = "CLOCK BATTERY LOW"
ALERT_LOOKS_REGULAR = "REGULAR CELL? NOT CHARGING"
ALERT_CHARGE_NOT_SET = "CLOCK CHARGING NOT SET"
ALERT_SETTING = "SETTING ERROR: rtc_battery"
ALERT_CLOCK_LOST = "CLOCK LOST - SET TIME"


# ---------------------------------------------------------------------------
# Pure decisions
# ---------------------------------------------------------------------------
def parse_setting(value):
    """
    The rtc_battery setting -> (kind, error).

    kind is RECHARGEABLE, REGULAR, or None when the row is missing (an older
    settings file): then charging is left as config.txt set it, which is how
    every box behaved before this setting existed. A value that can't be read
    counts as REGULAR -- never charge a cell we aren't sure about.
    """
    if value is None:
        return None, False
    v = str(value).strip().lower()
    if v in ("rechargeable", "ml2020", "ml2032", "ml-2020", "ml-2032", "charge", "yes", "true"):
        return RECHARGEABLE, False
    if v in ("regular", "cr2032", "cr-2032", "primary", "non-rechargeable", "no", "false"):
        return REGULAR, False
    return REGULAR, True


def want_charging(kind, cell_v):
    """
    The charging voltage to set (microvolts), or None to leave it as it is.

    Charging only ever goes on for a cell set as rechargeable that is actually
    there and doesn't read like a fresh regular cell.
    """
    if kind is None:
        return None
    if kind == REGULAR:
        return 0
    if cell_v is None or cell_v < NO_CELL_V or cell_v > LOOKS_REGULAR_V:
        return 0
    return CHARGE_UV


def alerts_for(kind, cell_v, setting_error, wanted_uv, actual_uv):
    """Display alerts for one check, most important first."""
    alerts = []
    if setting_error:
        alerts.append(ALERT_SETTING)
    if cell_v is not None:
        if cell_v < NO_CELL_V:
            alerts.append(ALERT_NO_CELL)
        elif cell_v < LOW_V:
            alerts.append(ALERT_LOW)
        elif kind == RECHARGEABLE and cell_v > LOOKS_REGULAR_V:
            alerts.append(ALERT_LOOKS_REGULAR)
    if wanted_uv is not None and actual_uv is not None and actual_uv != wanted_uv:
        alerts.append(ALERT_CHARGE_NOT_SET)
    return alerts


def clock_lost(rtc_epoch_s, floor=CLOCK_FLOOR):
    """True if the RTC reads earlier than the firmware could have been made."""
    if rtc_epoch_s is None:
        return False
    return rtc_epoch_s < floor.timestamp()


def settled_reading(read_v, sleep, min_s=SETTLE_MIN_S, max_s=SETTLE_MAX_S,
                    step_s=SETTLE_STEP_S, tol_v=SETTLE_TOL_V):
    """
    Keep reading until two readings in a row agree (an unplugged cell's
    reading has to decay from the charger's 3.0 V), at most max_s.
    Returns the last reading, or None if nothing could be read.
    """
    sleep(min_s)
    waited = min_s
    prev = read_v()
    while waited < max_s:
        sleep(step_s)
        waited += step_s
        cur = read_v()
        if prev is not None and cur is not None and abs(cur - prev) <= tol_v:
            return cur
        prev = cur
    return prev


# ---------------------------------------------------------------------------
# Hardware
# ---------------------------------------------------------------------------
def _read_int(name, rtc_dir=RTC_DIR):
    try:
        with open(os.path.join(rtc_dir, name)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def read_cell_v(rtc_dir=RTC_DIR):
    uv = _read_int("battery_voltage", rtc_dir)
    return None if uv is None else uv / 1e6


def read_charging_uv(rtc_dir=RTC_DIR):
    return _read_int("charging_voltage", rtc_dir)


def read_rtc_epoch(rtc_dir=RTC_DIR):
    return _read_int("since_epoch", rtc_dir)


def set_charging_uv(uv):
    """Set the trickle-charge voltage now (0 = off). True if the call ran."""
    cmd = ["vcmailbox", SET_RTC_REG_TAG, "8", "8", str(REG_CHARGE_VOLTS), str(int(uv))]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def check(setting_value, read_v=read_cell_v, read_chg=read_charging_uv,
          set_chg=set_charging_uv, sleep=time.sleep):
    """
    The boot check: measure the cell with charging off, then set charging.

    Returns a dict: kind, setting_error, cell_v, before_uv, wanted_uv,
    charging_uv (the readback), alerts.
    """
    kind, setting_error = parse_setting(setting_value)
    before = read_chg()

    cell_v = None
    if read_v() is not None:                 # this Pi has the RTC files at all
        if before:
            set_chg(0)
        cell_v = settled_reading(read_v, sleep)

    wanted = want_charging(kind, cell_v)
    if wanted is None:
        wanted_now = before                  # old settings file: put it back as it was
    else:
        wanted_now = wanted
    if wanted_now is not None and read_chg() != wanted_now:
        set_chg(wanted_now)
    actual = read_chg()

    return dict(kind=kind, setting_error=setting_error, cell_v=cell_v,
                before_uv=before, wanted_uv=wanted_now, charging_uv=actual,
                alerts=alerts_for(kind, cell_v, setting_error, wanted_now, actual))


# ---------------------------------------------------------------------------
# What the display and the next boot read
# ---------------------------------------------------------------------------
STATUS_FILE = "clock_battery.txt"           # in the state dir: key=value
HISTORY_FILE = "clock_battery_log.csv"      # one row per boot
HISTORY_MAX_BYTES = 200_000


def status_lines(result, lost, now_s):
    alerts = list(result.get("alerts", []))
    if lost:
        alerts.insert(0, ALERT_CLOCK_LOST)
    cell = result.get("cell_v")
    return (f"checked={int(now_s)}\n"
            f"setting={result.get('kind') or 'unset'}\n"
            f"cell_v={'' if cell is None else f'{cell:.3f}'}\n"
            f"charging_uv={'' if result.get('charging_uv') is None else result['charging_uv']}\n"
            f"clock_lost={1 if lost else 0}\n"
            f"alert={' | '.join(alerts)}\n")


def append_history(path, now_s, result, lost):
    new = not os.path.exists(path)
    try:
        if not new and os.path.getsize(path) > HISTORY_MAX_BYTES:
            with open(path) as f:
                lines = f.readlines()
            keep = [lines[0]] + lines[len(lines) // 2:]
            with open(path + ".tmp", "w") as f:
                f.writelines(keep)
            os.replace(path + ".tmp", path)
        with open(path, "a") as f:
            if new:
                f.write("time,setting,cell_v,charging_uv,clock_lost\n")
            cell = result.get("cell_v")
            f.write(f"{_dt.datetime.fromtimestamp(now_s):%Y-%m-%d %H:%M:%S},"
                    f"{result.get('kind') or 'unset'},"
                    f"{'' if cell is None else f'{cell:.3f}'},"
                    f"{'' if result.get('charging_uv') is None else result['charging_uv']},"
                    f"{1 if lost else 0}\n")
    except OSError:
        pass


def read_status(state_dir):
    out = {}
    try:
        with open(os.path.join(state_dir, STATUS_FILE)) as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def display_alerts(state_dir):
    """The alerts UpdateDisplay.py shows, from the last check."""
    alert = read_status(state_dir).get("alert", "")
    return [a.strip() for a in alert.split("|") if a.strip()]


def write_status(state_dir, result, lost, now_s):
    path = os.path.join(state_dir, STATUS_FILE)
    with open(path + ".tmp", "w") as f:
        f.write(status_lines(result, lost, now_s))
    os.replace(path + ".tmp", path)


def clear_clock_lost(state_dir, now_s):
    """The time has been set again (NTP or manualTime): drop CLOCK LOST."""
    st = read_status(state_dir)
    if st.get("clock_lost") != "1":
        return False
    alerts = [a for a in display_alerts(state_dir) if a != ALERT_CLOCK_LOST]
    result = dict(kind=None if st.get("setting") == "unset" else st.get("setting"),
                  cell_v=float(st["cell_v"]) if st.get("cell_v") else None,
                  charging_uv=int(st["charging_uv"]) if st.get("charging_uv") else None,
                  alerts=alerts)
    write_status(state_dir, result, False, now_s)
    return True


def was_clock_lost(state_dir):
    return read_status(state_dir).get("clock_lost") == "1"


if __name__ == "__main__":
    # By hand:  sudo python3 clock_battery.py [rechargeable|regular]
    import sys
    r = check(sys.argv[1] if len(sys.argv) > 1 else None)
    print(r)
    print("RTC lost power" if clock_lost(read_rtc_epoch()) else "RTC time looks sane")
