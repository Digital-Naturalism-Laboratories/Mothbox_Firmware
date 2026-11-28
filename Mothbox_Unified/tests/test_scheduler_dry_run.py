#!/usr/bin/python3
"""
Dry runs of the REAL Scheduler.py, start to finish, on any computer.

Each scenario boots Scheduler.py in a child process with the hardware, the
clock and the OS faked: /run, /sys, /proc, /etc, /boot/firmware, /home/pi and
/var/lib/mothbox point into a temp folder, every subprocess / os.system call
is recorded instead of run, and time.sleep() just moves a fake clock forward.
So a whole night -- boot, session, health checks, power-off -- runs in a
second, and the test reads back exactly what the box would have done:

    session states, wake alarms written to the RTC, every command, light
    switching, and the control files the display reads.

    python3 tests/test_scheduler_dry_run.py              # all scenarios
    python3 tests/test_scheduler_dry_run.py -v friend    # one, with its log
"""
import datetime as _dt
import json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCHEDULER = os.path.join(ROOT, "Scheduler.py")
CSV = os.path.join(os.path.dirname(ROOT), "mothbox_custom_Unified", "mothbox_settings.csv")

HI_ON = {"Active": 1, "HI": 1, "Debug": 0}
HI_OFF = {"Active": 1, "HI": 0, "Debug": 0}

SCENARIOS = {
    # the field case: 00:00 + 1400 min daily, photo every 2 min, switched on at 12:07
    "friend": dict(start=(2026, 11, 5, 12, 7), csv={"hour": "0", "runtime": "1400", "photo_interval": "2"}),
    # shipped schedule (19;21;23;2;4), switched on between windows
    "standby": dict(start=(2026, 11, 5, 20, 30), csv={"runtime": "60"}),
    # switched on 4 minutes before the 19:00 window
    "prewindow": dict(start=(2026, 11, 5, 18, 56), csv={"runtime": "60"}),
    # 55-minute hourly windows: a 5-minute dark gap the box stays up through
    "gap": dict(start=(2026, 11, 5, 19, 30), csv={"hour": "19;20", "runtime": "55"}),
    # typos that used to crash the Scheduler at boot
    "typo": dict(start=(2026, 11, 5, 20, 30), csv={"hour": "19 21", "runtime": "0"}),
    # Active switch off
    "off": dict(start=(2026, 11, 5, 19, 30), switches={"Active": 0}),
    # HI POWER: same loop, never powers off; leaves when the HI switch goes off
    "hipower": dict(start=(2026, 11, 5, 19, 30), csv={"runtime": "60"}, switches={"HI": 1},
                    hw_reads=[HI_ON] * 100 + [HI_OFF] * 3),
    # hi_power_always (a DIY has no HI switch): Active alone runs HI POWER; the
    # HI switch reading 0 must not end it, the Active switch going off does
    "hipower_setting": dict(start=(2026, 11, 5, 19, 30), csv={"runtime": "60", "hi_power_always": "True"},
                            hw_reads=[HI_OFF] * 100 + [{"Active": 0, "HI": 0, "Debug": 0}] * 3),
    # a capture hung for 400 s and no photo arrives: kill it, raise PHOTOS STOPPED
    "health": dict(start=(2026, 11, 5, 19, 0), csv={"runtime": "60"},
                   ps="  4242  400 /usr/bin/python /home/pi/Desktop/Mothbox/TakePhoto.py\n"),
    # TakePhoto refuses to shoot below safetygb - 1 GB free: say so, not just "stopped"
    "sd_full": dict(start=(2026, 11, 5, 19, 0), csv={"runtime": "60"}, sd_free_gb=6),
    # a 10-min gap means power off -- but the final backup takes 6 min, so by
    # the time it's done the next window is close: stay up instead
    "slow_backup": dict(start=(2026, 11, 5, 19, 30), csv={"hour": "19;20", "runtime": "50"}, backup_s=360),
    # 1440 min every day: the windows never leave a gap
    "always_on": dict(start=(2026, 11, 5, 12, 0), csv={"hour": "0", "runtime": "1440"}, stop_after_s=6 * 3600),
    # Debug switch on
    "debug": dict(start=(2026, 11, 5, 12, 0), switches={"Debug": 1}),
}

REMAP = ("/run/", "/sys/", "/proc/", "/etc/", "/boot/firmware/", "/home/pi/", "/var/lib/mothbox/")


# ===========================================================================
# The child: one scenario, everything faked
# ===========================================================================
def child(name, out_path):
    import builtins, glob, io, types
    from pathlib import Path

    sc = SCENARIOS[name]
    root = tempfile.mkdtemp(prefix="mothbox_dry_")
    clock = [time.mktime(sc["start"] + (0, 0, 0, -1))]
    deadline = clock[0] + 3 * 24 * 3600
    events = []

    def stamp():
        return _dt.datetime.fromtimestamp(clock[0]).strftime("%m-%d %H:%M:%S")

    def rec(kind, detail):
        events.append([stamp(), kind, detail])

    def remap(p):
        if isinstance(p, (str, os.PathLike)):
            s = os.fspath(p)
            if isinstance(s, str) and s.startswith(REMAP):
                return root + s
        return p

    # ---- the fake filesystem ---------------------------------------------------
    def write(rel, text):
        path = root + rel
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    os.makedirs(root + "/run", exist_ok=True)
    os.makedirs(root + "/home/pi/Desktop/Mothbox/logs", exist_ok=True)
    os.makedirs(root + "/sys/bus/i2c/drivers/ov64a40/10-0036", exist_ok=True)   # camera bound
    write("/proc/cpuinfo", "Model\t: Raspberry Pi 5 Model B Rev 1.0\nSerial\t: 10000000a1b2c3d4\n")
    write("/proc/meminfo", "MemTotal:        8245000 kB\n")
    write("/proc/uptime", "60.0 50.0\n")
    write("/etc/os-release", "VERSION_CODENAME=bookworm\n")
    write("/sys/class/rtc/rtc0/wakealarm", "")
    shutil.copy(os.path.join(ROOT, "wordlist.csv"), root + "/home/pi/Desktop/Mothbox/wordlist.csv")
    rows = []
    for line in open(CSV, encoding="utf-8"):
        key = line.split(",", 1)[0].strip()
        if key in sc.get("csv", {}):
            parts = line.split(",", 2)
            parts[1] = sc["csv"][key]
            line = ",".join(parts)
        rows.append(line)
    write("/boot/firmware/mothbox_custom/mothbox_settings.csv", "".join(rows))
    shutil.copytree(os.path.join(os.path.dirname(CSV), "system"), root + "/boot/firmware/mothbox_custom/system")
    switches = {"Active": 1, "Debug": 0, "HI": 0, "U1": 0, "C1": 0}
    switches.update(sc.get("switches", {}))
    write("/var/lib/mothbox/state/switches.txt", "".join(f"{k}={v}\n" for k, v in switches.items()))

    # ---- open() and os.* that take paths -----------------------------------------
    real_open = builtins.open

    class AlarmFile(io.StringIO):
        def __init__(self, path):
            super().__init__()
            self.path = path
        def close(self):
            rec("alarm", self.getvalue().strip())
            with real_open(self.path, "w") as f:
                f.write(self.getvalue())
            super().close()
        def __exit__(self, *a):
            self.close()

    def fake_open(file, mode="r", *a, **kw):
        path = remap(file)
        if isinstance(path, str) and path.endswith("/sys/class/rtc/rtc0/since_epoch"):
            return io.StringIO(str(int(clock[0])))
        if isinstance(path, str) and path.endswith("/sys/class/rtc/rtc0/wakealarm") and "w" in mode:
            return AlarmFile(path)
        return real_open(path, mode, *a, **kw)
    builtins.open = fake_open

    def wrap1(fn):
        return lambda p, *a, **kw: fn(remap(p), *a, **kw)
    def wrap2(fn):
        return lambda p, q, *a, **kw: fn(remap(p), remap(q), *a, **kw)
    for fn in ("remove", "unlink", "makedirs", "listdir", "access", "chmod", "stat"):
        setattr(os, fn, wrap1(getattr(os, fn)))
    for fn in ("replace", "rename"):
        setattr(os, fn, wrap2(getattr(os, fn)))
    for fn in ("exists", "isdir", "isfile", "getsize"):
        setattr(os.path, fn, wrap1(getattr(os.path, fn)))
    glob.glob = wrap1(glob.glob)
    # The SD card's free space is part of the scenario, not this computer's disk
    free = int(sc.get("sd_free_gb", 100) * 2**30)
    shutil.disk_usage = lambda p: shutil._ntuple_diskusage(128 * 2**30, 128 * 2**30 - free, free)
    real_which = shutil.which
    shutil.which = lambda c, *a, **kw: "/usr/bin/" + c if c == "rpicam-hello" else real_which(c, *a, **kw)
    os.kill = lambda pid, sig: rec("kill", f"{pid} signal {sig}")
    os.killpg = lambda pid, sig: rec("killpg", f"{pid} signal {sig}")

    # ---- commands --------------------------------------------------------------
    def text_of(cmd):
        return cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)

    def fake_run(cmd, *a, **kw):
        t = text_of(cmd)
        rec("cmd", t)
        out, rc = "", 0
        if "Backup_Files.py --final" in t:
            clock[0] += sc.get("backup_s", 0)           # a slow USB drive
        if t.startswith("pgrep"):
            rc = 1                                      # no TakePhoto.py running
        elif "rpicam-hello" in t:
            out = "Camera started, 500 ms"
        elif t.startswith("ps -eo"):
            out = sc.get("ps", "")
        as_text = kw.get("text") or kw.get("universal_newlines")
        return subprocess.CompletedProcess(cmd, rc, out if as_text else out.encode(),
                                           "" if as_text else b"")

    class FakePopen:
        pid_seq = [9000]
        def __init__(self, cmd, *a, **kw):
            self.args = cmd
            FakePopen.pid_seq[0] += 1
            self.pid = FakePopen.pid_seq[0]
            self.returncode = 0
            rec("popen", text_of(cmd))
            if "UpdateDisplay.py" in text_of(cmd):
                # what the screen would show: the files UpdateDisplay.py reads
                def kv(rel):
                    try:
                        with real_open(root + "/var/lib/mothbox/state/" + rel) as f:
                            return dict(l.strip().split("=", 1) for l in f if "=" in l)
                    except OSError:
                        return {}
                shown = kv("session.txt")
                shown["mode"] = kv("mode.txt").get("mode")
                rec("display", json.dumps(shown, sort_keys=True))
        def communicate(self, *a, **kw):
            return b"", b""
        def wait(self, *a, **kw):
            return 0
        def poll(self):
            return 0
        def kill(self):
            pass

    subprocess.run = fake_run
    subprocess.Popen = FakePopen
    subprocess.check_output = lambda cmd, *a, **kw: fake_run(cmd, *a, **kw).stdout
    subprocess.call = lambda cmd, *a, **kw: fake_run(cmd, *a, **kw).returncode
    os.system = lambda cmd: (rec("system", cmd), 0)[1]

    # ---- the clock -------------------------------------------------------------
    class FakeDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock[0], tz)

    class StopDryRun(BaseException):
        pass

    def fake_sleep(s):
        clock[0] += max(0.0, float(s))
        if "stop_after_s" in sc and clock[0] > deadline_stop:
            raise StopDryRun()
        if clock[0] > deadline:
            raise RuntimeError("runaway: the fake clock passed 3 days")
    deadline_stop = clock[0] + sc.get("stop_after_s", 0)
    _dt.datetime = FakeDateTime
    time.time = lambda: clock[0]
    time.sleep = fake_sleep

    # ---- fake hardware and helper modules --------------------------------------------
    def module(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    class NoOp:
        BCM = OUT = IN = HIGH = 1
        LOW = PUD_UP = PUD_DOWN = 0
        def __getattr__(self, name):
            return lambda *a, **kw: None
    gpio = NoOp()
    module("RPi", GPIO=gpio)
    sys.modules["RPi.GPIO"] = gpio

    module("eeprom_safe", ensure_eeprom_configured=lambda *a, **kw: "ok",
           should_reboot_before_poweroff=lambda: False,
           reboot_to_apply=lambda: rec("system", "reboot_to_apply"))
    module("firstboot_guard", wait_for_firstboot_settle=lambda *a, **kw: None,
           filesystem_needs_expansion=lambda *a, **kw: False)

    reads = list(sc.get("hw_reads", []))
    def read_switches():
        return dict(reads.pop(0)) if reads else dict(switches)
    module("mothbox_hw",
           detect_hardware=lambda *a, **kw: {"hardware": "pro", "detected_by": "dry run",
                                             "i2c_found": [0x20, 0x21], "voltage_sensor": "ina219",
                                             "light_sensor": "ltr-f216a"},
           read_switches=read_switches,
           attract_on=lambda: rec("hw", "attract_on"),
           all_lights_off=lambda: rec("hw", "all_lights_off"))

    state_root = Path(root + "/var/lib/mothbox/state")
    legacy_root = Path(root + "/boot/firmware/mothbox_custom/system/controls")
    def resolve_read(p):
        p = Path(p)
        if p.exists():
            return p
        legacy = legacy_root / p.name
        return legacy if legacy.exists() else p
    def ensure_state_dir():
        state_root.mkdir(parents=True, exist_ok=True)
        return state_root
    module("mothbox_paths", STATE_ROOT=state_root, LEGACY_ROOT=legacy_root,
           resolve_read=resolve_read, ensure_state_dir=ensure_state_dir)

    import random
    class FakeRandom:
        seed = staticmethod(random.seed)
        @staticmethod
        def choice(seq, n):
            return [random.choice(list(seq)) for _ in range(n)]
    module("numpy", random=FakeRandom())

    sys.path.insert(0, ROOT)
    import session
    real_write_state = session.write_state
    def write_state(state, path=session.STATE_FILE):
        rec("state", state)
        real_write_state(state, path)
    session.write_state = write_state

    # ---- run it ----------------------------------------------------------------------
    try:
        code = compile(real_open(SCHEDULER).read(), SCHEDULER, "exec")
        exec(code, {"__name__": "__main__", "__file__": SCHEDULER})
        ended = "returned"
    except SystemExit as e:
        ended = f"exit({e.code})"
    except StopDryRun:
        ended = "still running"
    except BaseException as e:
        import traceback
        ended = "crashed: " + "".join(traceback.format_exception(e))[-1500:]

    def control(rel):
        try:
            with real_open(root + rel) as f:
                return f.read().strip()
        except OSError:
            return None
    files = {k: control(v) for k, v in {
        "mode": "/var/lib/mothbox/state/mode.txt",
        "session": "/var/lib/mothbox/state/session.txt",
        "nextwake": "/var/lib/mothbox/state/nextwake.txt",
        "schedule_errors": "/var/lib/mothbox/state/schedule_errors.txt",
        "hours": "/var/lib/mothbox/state/hours.txt",
        "runtime": "/var/lib/mothbox/state/runtime.txt",
        "run_session": "/run/mothbox_session",
        "run_health": "/run/mothbox_health",
    }.items()}
    with real_open(out_path, "w") as f:
        json.dump({"ended": ended, "events": events, "files": files}, f)
    shutil.rmtree(root, ignore_errors=True)


# ===========================================================================
# The parent: run each scenario in its own process and check what happened
# ===========================================================================
failures = 0
def check(label, got, want):
    global failures
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"     got:  {got!r}\n     want: {want!r}")
        failures += 1


def run(name, verbose=False):
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "result.json")
        p = subprocess.run([sys.executable, __file__, "--child", name, out],
                           capture_output=True, text=True, timeout=300)
        if verbose or not os.path.exists(out):
            print(p.stdout[-20000:], p.stderr[-5000:])
        if not os.path.exists(out):
            raise SystemExit(f"scenario {name}: the child produced no result")
        with open(out) as f:
            return json.load(f)


def epoch(y, mo, d, h, mi):
    return str(int(time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))))


def states(r):
    """Session states in order, repeats collapsed (the loop and power_off both say shutting_down)."""
    out = []
    for e in r["events"]:
        if e[1] == "state" and (not out or out[-1] != e[2]):
            out.append(e[2])
    return out


def find(r, kind, text):
    return [e for e in r["events"] if e[1] == kind and text in e[2]]


def last_index(r, kind, text=""):
    idx = [i for i, e in enumerate(r["events"]) if e[1] == kind and text in e[2]]
    return idx[-1] if idx else -1


def halted_cleanly(r, label, wake):
    """Powered off with the wake alarm as the last thing before the halt."""
    ev = r["events"]
    halt = last_index(r, "system", "shutdown -h now")
    alarm = last_index(r, "alarm")
    check(f"{label}: halts with the wake alarm set for {wake[1]} as its last step",
          (halt > alarm >= 0, ev[alarm][2] if alarm >= 0 else None,
           [e[1] for e in ev[alarm + 1:halt] if e[1] in ("cmd", "popen")]),
          (True, wake[0], []))


def main():
    verbose = "-v" in sys.argv
    only = [a for a in sys.argv[1:] if a in SCENARIOS]

    def want(name):
        return not only or name in only

    if want("friend"):
        r = run("friend", verbose)
        check("friend: Scheduler ran to the end without crashing", r["ended"], "returned")
        check("friend: booting -> active -> shutting_down", states(r), ["booting", "active", "shutting_down"])
        on = find(r, "hw", "attract_on")
        check("friend: lights on as soon as boot finishes (12:07 is inside 00:00-23:20)",
              on[0][0][:8] if on else None, "11-05 12")
        halt = find(r, "system", "shutdown -h now")
        check("friend: powers off at 23:20 the SAME day -- not 11:27 the next",
              halt[0][0][:11] if halt else None, "11-05 23:19")
        halted_cleanly(r, "friend", (epoch(2026, 11, 5, 23, 58), "23:58, 2 min before the 00:00 window"))
        check("friend: final backup ran before the wake alarm was set",
              last_index(r, "cmd", "Backup_Files.py --final") < last_index(r, "alarm"), True)
        refreshes = find(r, "popen", "UpdateDisplay.py --refresh")
        check("friend: display refreshed every 10 min through the session (not twice at boot)",
              len(refreshes), 67)
        check("friend: a health check (ps) with every refresh", len(find(r, "cmd", "ps -eo")), 67)
        boot_screen = json.loads(find(r, "display", "")[0][2])
        check("friend: the boot screen says SESSION ENDS 23:20",
              (boot_screen.get("phase"), boot_screen.get("session_end")), ("active", epoch(2026, 11, 5, 23, 20)))
        check("friend: shut down exactly once", len(find(r, "system", "shutdown -h now")), 1)
        check("friend: the box reads STANDBY with the next wake at the end",
              (r["files"]["mode"], r["files"]["nextwake"], r["files"]["session"].splitlines()[0]),
              ("mode=STANDBY", "nextwake=" + epoch(2026, 11, 5, 23, 58), "phase=standby"))
        check("friend: no schedule errors", r["files"]["schedule_errors"], "errors=")

    if want("standby"):
        r = run("standby", verbose)
        check("standby: boot between windows -> quits after powering off", r["ended"], "exit(None)")
        check("standby: booting -> shutting_down, no session", states(r), ["booting", "shutting_down"])
        check("standby: 2 quick 'all good' blinks", len(find(r, "cmd", "blink_standby.py 2 0.125")), 1)
        check("standby: no lights, no final backup",
              (find(r, "hw", "attract_on"), find(r, "cmd", "--final")), ([], []))
        halted_cleanly(r, "standby", (epoch(2026, 11, 5, 20, 58), "20:58 for the 21:00 window"))

    if want("prewindow"):
        r = run("prewindow", verbose)
        check("prewindow: booting -> waiting -> active -> shutting_down",
              states(r), ["booting", "waiting", "active", "shutting_down"])
        on = find(r, "hw", "attract_on")
        check("prewindow: dark until the window, lights on just before 19:00",
              on[0][0] if on else None, "11-05 18:59:57")
        check("prewindow: an 'all good' blink so nobody thinks it's dead while it waits",
              len(find(r, "cmd", "blink_standby.py 2 0.125")), 1)
        shown = [json.loads(e[2]) for e in find(r, "display", "")]
        check("prewindow: boot screen says STARTS 19:00, then SESSION ENDS 20:00 when it opens",
              [(d.get("phase"), d.get("next_start") if d.get("phase") == "waiting" else d.get("session_end"))
               for d in shown[:2]],
              [("waiting", epoch(2026, 11, 5, 19, 0)), ("active", epoch(2026, 11, 5, 20, 0))])
        halted_cleanly(r, "prewindow", (epoch(2026, 11, 5, 20, 58), "20:58 for the 21:00 window"))

    if want("gap"):
        r = run("gap", verbose)
        check("gap: active -> waiting through the 5-min gap -> active -> off",
              states(r), ["booting", "active", "waiting", "active", "shutting_down"])
        check("gap: the window's backup runs in the background during the gap",
              [e[0] for e in find(r, "popen", "Backup_Files.py --final")], ["11-05 19:54:57"])
        check("gap: lights back on for 20:00", [e[0] for e in find(r, "hw", "attract_on")][1:],
              ["11-05 19:59:57"])
        in_gap = [json.loads(e[2]) for e in find(r, "display", "") if "11-05 19:54:57" <= e[0] < "11-05 19:59:57"]
        check("gap: the screen during the gap says STARTS 20:00",
              [(d.get("phase"), d.get("next_start")) for d in in_gap], [("waiting", epoch(2026, 11, 5, 20, 0))])
        at_open = [json.loads(e[2]) for e in find(r, "display", "") if e[0] == "11-05 19:59:57"]
        check("gap: and at 20:00 it says SESSION ENDS 20:55",
              [(d.get("phase"), d.get("session_end")) for d in at_open], [("active", epoch(2026, 11, 5, 20, 55))])
        check("gap: only one power-off, after the second window",
              [e[0][:11] for e in find(r, "system", "shutdown -h now")], ["11-05 20:54"])
        halted_cleanly(r, "gap", (epoch(2026, 11, 6, 18, 58), "18:58 tomorrow"))

    if want("typo"):
        r = run("typo", verbose)
        check("typo: 'hour 19 21' + 'runtime 0' no longer crash the Scheduler", r["ended"], "exit(None)")
        check("typo: both settings named for the display", r["files"]["schedule_errors"], "errors=hour,runtime")
        check("typo: the defaults actually in use are what the display shows (it re-sorts into night order)",
              (r["files"]["hours"], r["files"]["runtime"]), ("hours=2;4;19;21;23", "runtime=60"))
        halted_cleanly(r, "typo", (epoch(2026, 11, 5, 20, 58), "20:58 (default hours)"))

    if want("off"):
        r = run("off", verbose)
        check("off: shuts down straight away", (r["ended"], states(r)), ("exit(None)", ["booting", "shutting_down"]))
        check("off: OFF means OFF -- no blinks, no lights",
              (find(r, "cmd", "blink_standby"), find(r, "hw", "attract_on")), ([], []))
        check("off: mode stays OFF", r["files"]["mode"], "mode=OFF")
        halted_cleanly(r, "off", (epoch(2026, 11, 5, 20, 58), "the next window, to re-check the switch"))

    if want("hipower"):
        r = run("hipower", verbose)
        check("hipower: never powers off", find(r, "system", "shutdown -h now"), [])
        check("hipower: dark between windows, lights again at 21:00",
              states(r)[:4], ["booting", "active", "waiting", "active"])
        check("hipower: the window's backup still runs at 20:00",
              [e[0] for e in find(r, "popen", "Backup_Files.py --final")], ["11-05 19:59:57"])
        shown = [json.loads(e[2]) for e in find(r, "display", "") if "11-05 19:59:57" <= e[0] < "11-05 20:59:57"]
        check("hipower: between windows the screen says STARTS 21:00 (mode HI POWER)",
              {(d.get("phase"), d.get("next_start"), d.get("mode")) for d in shown},
              {("waiting", epoch(2026, 11, 5, 21, 0), "HI_POW")})
        check("hipower: reboots once the HI switch has read off 3 times",
              (r["ended"], [e[2] for e in find(r, "system", "reboot")]), ("returned", ["sudo reboot"]))

    if want("hipower_setting"):
        r = run("hipower_setting", verbose)
        check("hipower_setting: HI switch off, but hi_power_always makes Active run as HI POWER",
              r["files"]["mode"], "mode=HI_POW")
        check("hipower_setting: never powers off", find(r, "system", "shutdown -h now"), [])
        check("hipower_setting: dark between windows, lights again at 21:00",
              states(r)[:4], ["booting", "active", "waiting", "active"])
        reboots = find(r, "system", "reboot")
        check("hipower_setting: the HI switch reading 0 doesn't end it; the Active switch going off does",
              (r["ended"], [e[2] for e in reboots], reboots[0][0][:8] if reboots else None),
              ("returned", ["sudo reboot"], "11-05 21"))

    if want("health"):
        r = run("health", verbose)
        check("health: the hung capture is killed", [e[2] for e in find(r, "kill", "4242")][:1], ["4242 signal 9"])
        check("health: ...and the flash forced off after it",
              len(find(r, "cmd", "Flash_Off.py")) >= 1, True)
        check("health: no photos for 10 min -> PHOTOS STOPPED for the display",
              r["files"]["run_health"], "PHOTOS STOPPED")
        check("health: but it never reboots", find(r, "system", "reboot"), [])

    if want("sd_full"):
        r = run("sd_full", verbose)
        check("sd_full: no photos because the SD card is full -> says SD FULL",
              r["files"]["run_health"], "SD FULL - NO PHOTOS")

    if want("slow_backup"):
        r = run("slow_backup", verbose)
        check("slow_backup: stays up when the next window got close during the backup",
              states(r), ["booting", "active", "shutting_down", "waiting", "active", "shutting_down"])
        check("slow_backup: ...the deadman is disarmed so it can't kill the next window",
              len(find(r, "killpg", "signal 15")), 1)
        check("slow_backup: ...and the 20:00 window opens on time",
              [e[0] for e in find(r, "hw", "attract_on")][1:], ["11-05 19:59:57"])
        check("slow_backup: one real power-off, after the 20:00 window",
              [e[0][:11] for e in find(r, "system", "shutdown -h now")], ["11-05 20:55"])   # its backup takes 6 min too
        halted_cleanly(r, "slow_backup", (epoch(2026, 11, 6, 18, 58), "18:58 tomorrow"))

    if want("always_on"):
        r = run("always_on", verbose)
        check("always_on: active and still running 6 h later, never powering off",
              (r["ended"], states(r), find(r, "system", "shutdown -h now")), ("still running", ["booting", "active"], []))
        check("always_on: no wake alarm needed", find(r, "alarm", ""), [])
        check("always_on: the display says the session never ends",
              r["files"]["session"].splitlines()[:2], ["phase=active", "session_end=-1"])

    if want("debug"):
        r = run("debug", verbose)
        check("debug: 'debug' state (manual TakePhoto works), no session, no power-off",
              (r["ended"], states(r), find(r, "system", "shutdown -h now")), ("returned", ["booting", "debug"], []))
        check("debug: DebugMode.py ran after the state was set",
              last_index(r, "state", "debug") < last_index(r, "cmd", "DebugMode.py"), True)

    if failures:
        print(f"\n{failures} check(s) FAILED")
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--child":
        child(sys.argv[2], sys.argv[3])
    else:
        main()
