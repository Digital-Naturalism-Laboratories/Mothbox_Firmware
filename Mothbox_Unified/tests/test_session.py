#!/usr/bin/python3
"""
The session loop must follow the real windows -- open on time, close at the
window's end, stay up through short gaps, power off for long ones -- and the
one-word session state must be the only thing that lets cron light up or
photograph. HI POWER is the same loop that never powers off.

Drives session.run() with the real schedule_core and a fake clock.

    python3 tests/test_session.py
"""
import datetime as dt
import os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import schedule_core as sc
import session

failures = 0
def check(label, got, want):
    global failures
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"     got:  {got!r}\n     want: {want!r}")
        failures += 1

def T(day, h, m=0, s=0):
    return dt.datetime(2026, 11, day, h, m, s)

class Box:
    """Fake clock + recorded callbacks."""
    def __init__(self, start, hour="19;21;23;2;4", runtime=60, keep_awake=False,
                 power_off_result="powered_off", switches=None, raise_in=None):
        self.t = start
        self.sched = sc.parse({"weekday": "1;2;3;4;5;6;7", "hour": hour, "minute": "0", "runtime": runtime})
        self.events = []
        self.states = []
        self.refreshes = 0
        self.power_off_result = power_off_result
        self.switches = list(switches or [])
        self.raise_in = raise_in or set()
        self.keep_awake = keep_awake

    def now(self):
        return self.t
    def sleep(self, s):
        self.t += dt.timedelta(seconds=s)
    def set_state(self, s):
        self.states.append(s)
    def plan_at(self, t):
        if "plan" in self.raise_in:
            raise RuntimeError("clock")
        return sc.plan_at(self.sched, t)
    def on_activate(self, plan):
        self.events.append(("on", self.t))
        if "activate" in self.raise_in:
            raise RuntimeError("lights")
    def on_deactivate(self, plan):
        self.events.append(("off", self.t))
        self.deactivated_with = plan
    def power_off(self, from_session):
        self.events.append(("power_off", self.t, from_session))
        if "power_off" in self.raise_in:
            raise OSError("rtc")
        result = self.power_off_result
        if isinstance(result, list):
            result = result.pop(0) if result else "powered_off"
        return result
    def every_10_min(self, state):
        self.refreshes += 1
    def read_switches(self):
        sw = self.switches.pop(0) if self.switches else {"Active": 1, "HI": 1, "Debug": 0}
        if isinstance(sw, Exception):
            raise sw
        return sw

    def run(self, until=None, **kw):
        polls = kw.pop("max_polls", 20000)
        hi = self.keep_awake and self.switches is not None
        return session.run(
            plan_at=self.plan_at, now=self.now, set_state=self.set_state,
            on_activate=self.on_activate, on_deactivate=self.on_deactivate,
            power_off=self.power_off, keep_awake=self.keep_awake,
            every_10_min=self.every_10_min,
            read_switches=self.read_switches if self.keep_awake else None,
            on_switch_change=lambda: self.events.append(("switch_change", self.t)),
            sleep=self.sleep, log=lambda m: None, max_polls=polls, **kw)

# --- the field case: 00:00 + 1400 min, switched on at 12:07 -------------------------
b = Box(T(5, 12, 7), hour="0", runtime=1400)
result = b.run()
check("switched on 12:07: lights and photos on straight away", b.events[0], ("on", T(5, 12, 7)))
check("...the window closes at 23:20 the same day and the box powers off (3 s lookahead)",
      (result, b.events[-1]), ("powered_off", ("power_off", T(5, 23, 19, 57), True)))
check("...state: active, then shutting_down", b.states, ["active", "shutting_down"])
check("...a health check + display refresh every 10 minutes", b.refreshes, 67)

# --- a 5-min gap: stay up, dark, then start on time ------------------------------------
b = Box(T(5, 19, 30), hour="19;20", runtime=55)
b.run(max_polls=200)
check("55-min hourly: closes at 19:55, reopens at 20:00 -- no power-off",
      [e[:2] for e in b.events[:3]], [("on", T(5, 19, 30)), ("off", T(5, 19, 54, 57)), ("on", T(5, 19, 59, 57))])
check("...states active -> waiting -> active", b.states[:3], ["active", "waiting", "active"])
check("...and the window's end is told when the next one opens (not 'still active')",
      (b.deactivated_with.state, b.deactivated_with.next_start), ("waiting", T(5, 20, 0)))

# --- switched on 4 minutes before a window ------------------------------------------------
b = Box(T(5, 18, 56))
b.run(max_polls=30)
check("4 min early: waits dark, then opens at the window start",
      (b.states[:2], b.events[0]), (["waiting", "active"], ("on", T(5, 18, 59, 57))))

# --- the shipped default: power off between windows -------------------------------------
b = Box(T(5, 19, 30))
b.run()
check("default schedule: powers off at the end of the 19:00 window",
      (b.events[-1], b.states), (("power_off", T(5, 19, 59, 57), True), ["active", "shutting_down"]))

b = Box(T(5, 20, 30))
b.run()
check("booted between windows (the Scheduler's standby path): powers off, no session",
      (b.events, b.states), ([("power_off", T(5, 20, 30), False)], ["shutting_down"]))

# --- power_off changes its mind, or fails -------------------------------------------------
b = Box(T(5, 19, 30), power_off_result=["aborted"])
b.run(max_polls=400)
check("power_off 'aborted' (next window got close) -> waiting, and the loop carries on",
      b.states[:3], ["active", "shutting_down", "waiting"])

b = Box(T(5, 20, 30), raise_in={"power_off"})
r = b.run(max_polls=5)
check("power_off raising never counts as powered off: it is retried",
      (r, [e[0] for e in b.events].count("power_off") > 1, b.states[-1]), ("max_polls", True, "waiting"))

# --- callbacks and schedule errors don't kill the loop ---------------------------------------
b = Box(T(5, 19, 30), raise_in={"activate"})
r = b.run()
check("lights failing to come on doesn't stop the session ending on time",
      (r, b.events[-1][:2]), ("powered_off", ("power_off", T(5, 19, 59, 57))))
b = Box(T(5, 19, 30), raise_in={"plan"})
check("a failing schedule check changes nothing", (b.run(max_polls=5), b.states, b.events), ("max_polls", [], []))

# --- DebugMode.py run by hand mid-session ---------------------------------------------------
b = Box(T(5, 19, 30))
check("shutdown disabled -> stop managing the box, touch nothing",
      (b.run(shutdown_enabled=lambda: False), b.states, b.events), ("shutdown_disabled", [], []))

# --- HI POWER: the same loop, never powers off ------------------------------------------------
b = Box(T(5, 19, 30), keep_awake=True)
b.run(max_polls=400)
kinds = [e[0] for e in b.events]
check("HI POWER: window closes at 20:00 -> dark but awake, opens again at 21:00",
      (kinds[:3], b.events[1][1], b.events[2][1]), (["on", "off", "on"], T(5, 19, 59, 57), T(5, 20, 59, 57)))
check("HI POWER never powers off", "power_off" in kinds, False)

HI_ON = {"Active": 1, "HI": 1, "Debug": 0}
HI_OFF = {"Active": 1, "HI": 0, "Debug": 0}
def hi(switches):
    b = Box(T(5, 19, 30), keep_awake=True, switches=switches)
    r = b.run(max_polls=len(switches) + 3, switch_check_s=0)
    return r, [e[0] for e in b.events if e[0] == "switch_change"]
check("isolated 'HI off' reads (flaky I2C) never leave HI POWER", hi([HI_OFF, HI_ON, HI_OFF, HI_OFF, HI_ON]), ("max_polls", []))
check("three 'HI off' reads in a row leave HI POWER", hi([HI_OFF, HI_OFF, HI_OFF]), ("switch_changed", ["switch_change"]))
check("Debug switched on also leaves (DEBUG wins)", hi([{"Active": 1, "HI": 1, "Debug": 1}] * 3)[0], "switch_changed")
check("Active switched off also leaves", hi([{"Active": 0, "HI": 1, "Debug": 0}] * 3)[0], "switch_changed")
check("switch read exceptions never leave", hi([OSError("i2c")] * 6), ("max_polls", []))
check("the fail-safe read (Active only) counts as 'not HI'", session.hi_power_still_selected({"Active": 1}), False)

# --- the state file -------------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    f = os.path.join(d, "mothbox_session")
    check("no state file -> nothing allowed (a fresh boot is dark)", session.allows(session.LIGHTS_AND_PHOTOS, f), False)
    for state, photos, backups in (("booting", False, False), ("waiting", False, True), ("active", True, True),
                                   ("shutting_down", False, False), ("debug", True, True)):
        session.write_state(state, f)
        check(f"'{state}': photos/lights {photos}, cron backups {backups}",
              (session.allows(session.LIGHTS_AND_PHOTOS, f), session.allows(session.BACKUPS, f)), (photos, backups))
    h = os.path.join(d, "mothbox_health")
    session.write_health("PHOTOS STOPPED", h)
    check("health alert round-trips", session.read_health(h), "PHOTOS STOPPED")
    session.write_health(None, h)
    check("a cleared health alert reads as none", session.read_health(h), None)

# --- health verdicts ----------------------------------------------------------------
M = 60
check("too early in the session to judge", session.photos_stalled(5 * M, 0, 1, None), False)
check("10 min in, a photo 1 min ago -> fine", session.photos_stalled(10 * M, 0, 1, 9 * M), False)
check("10 min in, no photo at all -> stalled", session.photos_stalled(10 * M, 0, 1, None), True)
check("photos stopped 12 min ago -> stalled", session.photos_stalled(60 * M, 0, 1, 48 * M), True)
check("interval 10: 25 min without a photo is still normal", session.photos_stalled(60 * M, 0, 10, 35 * M), False)
check("interval 10: 31 min without one is not", session.photos_stalled(60 * M, 0, 10, 29 * M), True)
check("a last photo in the future (clock moved back) counts as recent",
      session.photos_stalled(60 * M, 0, 1, 90 * M), False)
check("alert: stalled", session.health_alert(True, True, False), "PHOTOS STOPPED")
check("alert: stalled because the SD card is full", session.health_alert(True, True, True), "SD FULL - NO PHOTOS")
check("alert: no extra alert when the camera is already reported missing", session.health_alert(True, False, False), None)
check("alert: healthy", session.health_alert(False, True, True), None)
ps = """  101  400 /bin/sh -c /home/pi/Desktop/Mothbox/TakePhoto.py >> /home/pi/Desktop/Mothbox/logs/TakePhoto_log.txt 2>&1
  102  399 /usr/bin/python /home/pi/Desktop/Mothbox/TakePhoto.py
  103   20 /usr/bin/python /home/pi/Desktop/Mothbox/TakePhoto.py
  104 9000 /usr/bin/python3 -u /home/pi/Desktop/Mothbox/Scheduler.py
  105  900 /bin/sh -c sleep 45; python /home/pi/Desktop/Mothbox/Flash_Off.py"""
check("stuck TakePhoto runs are found (and nothing else)", session.stuck_photo_pids(ps), [101, 102])

# --- the cron scripts obey the session state, and nothing uses the old flags -------------------
def src(name):
    return open(os.path.join(ROOT, name)).read()
for script, gate, marker in (("Attract_On.py", "session.LIGHTS_AND_PHOTOS", "mothbox_hw.attract_on()"),
                             ("TakePhoto.py", "session.LIGHTS_AND_PHOTOS", "from picamera2"),
                             ("Backup_Files.py", "session.BACKUPS", "def rsync_photos_to_backup")):
    s = src(script)
    check(f"{script} checks the session state before doing anything",
          0 <= s.find(gate) < s.find(marker), True)
check("Backup_Files.py runs one at a time (the final backup can't race a cron one)",
      "fcntl.flock" in src("Backup_Files.py"), True)
for name in sorted(os.listdir(ROOT)):
    if name.endswith(".py") and name != "session.py":      # session.py documents what it replaced
        s = src(name)
        check(f"{name} no longer uses the boot lock / HI POWER idle flag",
              ("boot_script_running" in s, "mothbox_hipow_idle" in s), (False, False))

if failures:
    print(f"\n{failures} check(s) FAILED")
    sys.exit(1)
print("\nALL CHECKS PASSED")
