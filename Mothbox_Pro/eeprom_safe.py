#!/usr/bin/python3
"""
eeprom_safe.py  --  Safe, one-time, self-verifying Pi 5 EEPROM provisioning.

Why this exists
---------------
`rpi-eeprom-config --apply` does NOT write the EEPROM immediately. It stages
`pieeprom.upd` into /boot/firmware, and recovery.bin flashes the SPI chip on the
NEXT boot. If power is interrupted during that flash (RTC-wake power cut, fast
shutdown, first-boot resize reboot) the bootloader EEPROM corrupts. The old
scheduler staged an update and then powered off on the same boot -- the ~50%
corruption you were seeing.

This module guarantees:
  1. EEPROM work happens only ONCE (persistent flag).
  2. A needed change is staged, flushed, and applied by a CLEAN reboot -- never a
     halt / wake-alarm shutdown -- so recovery.bin flashes on stable power.
  3. Only POWER_OFF_ON_HALT / WAKE_ON_GPIO are changed; BOOT_ORDER and every
     other key are preserved verbatim (the old regex mangled BOOT_ORDER=0xf461->0).
  4. It refuses to run until the first-boot resize has finished.
  5. After the apply-reboot it VERIFIES the running config actually took, bounds
     retries (no reboot loop), and on failure cancels the stuck update and blinks
     a distinctive error pattern on the onboard LED.

Two failure modes, and what signals them
-----------------------------------------
  * EEPROM fully corrupt  -> Pi never boots. Software can't signal this; it shows
    as a dark Pi and needs Raspberry Pi Imager bootloader recovery. (ROM-level
    LED/HDMI diagnostics are the only indicator here.)
  * Flash didn't take but Pi still boots (config wrong -> won't sleep, drains
    battery). THIS is what blink_error_pattern() catches, because Python runs.

Usage in the scheduler (Pi 5 branch), replacing the old EEPROM block:

    import sys
    from eeprom_safe import ensure_eeprom_configured
    if rpiModel == 5:
        status = ensure_eeprom_configured()
        if status == "rebooting":
            sys.exit(0)            # rebooting to flash on stable power -- stop here
        # "ok"     -> EEPROM verified correct, continue
        # "failed" -> flash won't take; error already blinked, continue degraded
        # "defer"  -> resize not settled; continue, try again next boot

At the top of BOTH run_shutdown_pi5() and run_shutdown_pi5_FAST():

    from eeprom_safe import should_reboot_before_poweroff, reboot_to_apply
    if should_reboot_before_poweroff():
        reboot_to_apply()          # never power off with a live staged flash
"""

import os
import re
import sys
import time
import subprocess

# ---- Persistent state on the ROOT fs (survives reboot; resets on reflash) ----
STATE_DIR       = "/var/lib/mothbox"
EEPROM_FLAG     = os.path.join(STATE_DIR, "eeprom_provisioned")  # success
EEPROM_FAILED   = os.path.join(STATE_DIR, "eeprom_failed")       # gave up
EEPROM_ATTEMPTS = os.path.join(STATE_DIR, "eeprom_attempts")     # retry counter

STAGED_UPDATE = "/boot/firmware/pieeprom.upd"  # where recovery.bin looks
MAX_ATTEMPTS  = 3                              # bound the reboot-to-apply cycles

# Only these keys are touched. Everything else in the config is preserved.
DESIRED = {
    "POWER_OFF_ON_HALT": "1",
    "WAKE_ON_GPIO":      "0",
}

_KEYVAL = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")


# ---------------------------------------------------------------- small helpers
def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_attempts():
    try:
        with open(EEPROM_ATTEMPTS) as f:
            return int((f.read().strip() or "0"))
    except (OSError, ValueError):
        return 0


def _write_attempts(n):
    _atomic_write(EEPROM_ATTEMPTS, str(n) + "\n")


def _clear_attempts():
    try:
        os.remove(EEPROM_ATTEMPTS)
    except OSError:
        pass


def eeprom_flash_pending():
    """True if a bootloader EEPROM update is staged and waiting for a reboot."""
    return os.path.exists(STAGED_UPDATE)


def should_reboot_before_poweroff():
    """
    True if a flash is staged AND we haven't already given up. Used to guard the
    shutdown paths so we never power off mid-flash -- but we also never loop
    forever once provisioning has been marked failed.
    """
    return eeprom_flash_pending() and not os.path.exists(EEPROM_FAILED)


def _firstboot_resize_done():
    """False if the first-boot resize might still reboot us (Bookworm initramfs)."""
    try:
        with open("/boot/firmware/cmdline.txt") as f:
            cmdline = f.read()
    except OSError:
        return True
    return not any(m in cmdline for m in ("init_resize", "raspberrypi-sys-mods/firstboot"))


# ------------------------------------------------------------ EEPROM read/write
def _parse_config(raw):
    cfg = {}
    for line in raw.splitlines():
        m = _KEYVAL.match(line)
        if m:
            cfg[m.group(1)] = m.group(2)
    return cfg


def _editable_config_text():
    """Full current config text from rpi-eeprom-config -- the edit source."""
    raw = subprocess.check_output(["sudo", "rpi-eeprom-config"]).decode("utf-8")
    return raw


def _live_config():
    """
    The RUNNING EEPROM config, for verification. Prefer vcgencmd (reflects what
    the ROM actually loaded this boot); fall back to rpi-eeprom-config.
    """
    for cmd in (["vcgencmd", "bootloader_config"], ["sudo", "rpi-eeprom-config"]):
        try:
            raw = subprocess.check_output(cmd).decode("utf-8")
            cfg = _parse_config(raw)
            if cfg:
                return cfg
        except Exception:
            continue
    return {}


def _matches_desired(cfg):
    return all(cfg.get(k) == v for k, v in DESIRED.items())


def _stage_update(raw_config_text):
    """
    Edit ONLY the DESIRED keys, preserving every other line (BOOT_ORDER, headers,
    comments...), apply, then hard-flush pieeprom.upd to the FAT boot partition.
    """
    seen = set()
    out_lines = []
    for line in raw_config_text.splitlines():
        m = _KEYVAL.match(line)
        if m and m.group(1) in DESIRED:
            key = m.group(1)
            out_lines.append(f"{key}={DESIRED[key]}")
            seen.add(key)
        else:
            out_lines.append(line)
    for key, val in DESIRED.items():
        if key not in seen:
            out_lines.append(f"{key}={val}")

    _atomic_write("/tmp/eeprom_config.txt", "\n".join(out_lines).rstrip("\n") + "\n")
    subprocess.run(
        ["sudo", "rpi-eeprom-config", "--apply", "/tmp/eeprom_config.txt"],
        check=True,
    )
    # An unflushed .upd is a classic cause of a corrupt flash next boot.
    subprocess.run(["sync"], check=False)
    time.sleep(1)
    subprocess.run(["sync"], check=False)


def _cancel_staged_update():
    """Remove any pending EEPROM update so a failed unit boots on its old config."""
    subprocess.run(["sudo", "rpi-eeprom-update", "-r"], check=False)


# --------------------------------------------------------------- error signalling
def _find_status_led():
    base = "/sys/class/leds"
    for name in ("ACT", "led0", "PWR", "led1"):
        p = os.path.join(base, name)
        if os.path.isdir(p):
            return p
    try:
        entries = sorted(os.listdir(base))
        if entries:
            return os.path.join(base, entries[0])
    except OSError:
        pass
    return None


def _led_write(led_path, fname, value):
    try:
        with open(os.path.join(led_path, fname), "w") as f:
            f.write(value)
        return True
    except OSError:
        return False


def blink_error_pattern(repeats=6, on=0.12, off=0.12, gap=0.9):
    """
    Distinctive fast TRIPLE-blink burst on the onboard status LED to flag a bad /
    failed EEPROM apply. Runs only when the Pi boots -- a fully corrupt bootloader
    leaves the Pi dark and cannot be signalled from software. Needs root (the
    scheduler already runs as root).

    Override by passing your own callable to ensure_eeprom_configured(blink_error=...)
    e.g. one that calls /home/pi/Desktop/Mothbox/scripts/blink_standby.py with a
    count you reserve for 'EEPROM error' (distinct from your camera '8').
    """
    led = _find_status_led()
    if not led:
        print("[eeprom] No controllable onboard LED found for error signal.")
        return
    _led_write(led, "trigger", "none")
    try:
        for _ in range(repeats):
            for _ in range(3):                 # triple-blink = EEPROM error
                _led_write(led, "brightness", "1")
                time.sleep(on)
                _led_write(led, "brightness", "0")
                time.sleep(off)
            time.sleep(gap)
    finally:
        # Restore a sensible default so normal activity indication resumes.
        if not _led_write(led, "trigger", "mmc0"):
            _led_write(led, "trigger", "heartbeat")


def _mark_failed(live_cfg, blink_error):
    diag = "desired={}\nlive_snapshot={}\ntime={}\n".format(
        DESIRED,
        {k: live_cfg.get(k) for k in DESIRED},
        time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    _atomic_write(EEPROM_FAILED, diag)
    _cancel_staged_update()          # so the shutdown guard won't loop forever
    _clear_attempts()
    print("[eeprom] MARKED FAILED. Diagnostic written to", EEPROM_FAILED)
    (blink_error or blink_error_pattern)()


def _set_success():
    _atomic_write(EEPROM_FLAG, "ok\n")
    _clear_attempts()
    try:
        os.remove(EEPROM_FAILED)
    except OSError:
        pass


def reboot_to_apply():
    """Clean reboot so recovery.bin flashes the staged EEPROM on stable power."""
    subprocess.run(["sync"], check=False)
    subprocess.run(["sudo", "reboot"], check=False)
    time.sleep(120)                  # block; never fall through to shutdown
    sys.exit(0)


# --------------------------------------------------------------- main entry point
def ensure_eeprom_configured(blink_error=None):
    """
    Returns:
      "ok"        -- EEPROM verified correct (or just confirmed). Continue boot.
      "rebooting" -- staged an update and is rebooting to apply. STOP immediately.
      "failed"    -- flash won't take after MAX_ATTEMPTS; error blinked, staged
                     update cancelled. Continue booting in a degraded state.
      "defer"     -- first-boot resize not finished; skip this boot, retry next.
    """
    if os.path.exists(EEPROM_FLAG):
        return "ok"

    if os.path.exists(EEPROM_FAILED):
        print("[eeprom] Provisioning previously FAILED -- re-signalling, not retrying.")
        (blink_error or blink_error_pattern)()
        return "failed"

    if not _firstboot_resize_done():
        print("[eeprom] First-boot resize not finished yet -- deferring provisioning.")
        return "defer"

    live = _live_config()

    # ---- Verification / success: the running EEPROM already matches. ----
    if _matches_desired(live):
        print("[eeprom] Verified: running EEPROM matches desired settings.")
        _set_success()
        return "ok"

    # ---- Mismatch: decide whether to (re)try or give up. ----
    attempts = _read_attempts()
    if attempts >= MAX_ATTEMPTS:
        print(f"[eeprom] Flash did not take after {attempts} attempt(s) -- giving up.")
        _mark_failed(live, blink_error)
        return "failed"

    attempts += 1
    _write_attempts(attempts)
    print(f"[eeprom] Applying settings (attempt {attempts}/{MAX_ATTEMPTS}); "
          f"rebooting cleanly to flash...")
    try:
        _stage_update(_editable_config_text())
    except Exception as e:
        print(f"[eeprom] Failed to stage update: {e}")
        if attempts >= MAX_ATTEMPTS:
            _mark_failed(live, blink_error)
            return "failed"
        # fall through to reboot anyway is unsafe; just report and let next boot retry
        return "defer"

    reboot_to_apply()
    return "rebooting"


if __name__ == "__main__":
    print("[eeprom] result =", ensure_eeprom_configured())
