#!/usr/bin/python

"""
This script will schedule the next wakeups for the Mothbox
It should work on a Pi5 whose EEPROM is configured

 sudo -E rpi-eeprom-config --edit

 POWER_OFF_ON_HALT=1
WAKE_ON_GPIO=0
It also tries to set the EEPROM correctly too! So you don't have to do anything!

It should work on a Pi4 if it has a pijuice attached and installed

--------------------------------------------------------------------------
EEPROM SAFETY NOTE (see eeprom_safe.py, must be alongside this script)
--------------------------------------------------------------------------
`rpi-eeprom-config --apply` does not write the EEPROM immediately -- it stages
an update that is flashed by recovery.bin on the NEXT boot. The old version of
this script staged that update and then powered the device off on the SAME
boot (via the shutdown functions + RTC wake-alarm), which
could interrupt the SPI flash mid-write and corrupt the bootloader -- exactly
the "reflash the bootloader, put the SD card back in, works" symptom.

This version:
  1. Delegates EEPROM provisioning to eeprom_safe.ensure_eeprom_configured(),
     which is gated by a persistent flag so it only ever runs once, edits ONLY
     the keys we care about (preserving BOOT_ORDER etc.), and applies staged
     changes via a CLEAN reboot -- never a halt/wake-alarm power-off.
  2. Verifies the live/running config after that reboot before declaring
     success, with bounded retries and an onboard-LED error blink if the
     flash genuinely won't take.
  3. Guards power_off() so the device will reboot-to-apply instead of
     powering off whenever an EEPROM flash is still staged and pending.
--------------------------------------------------------------------------
"""

###------ Session state: booting ------###
# Nothing cron starts -- lights, photos, backups -- runs until the session
# state says so (see session.py). Written first, before the slow imports.
import session
session.write_state(session.BOOTING)
#-------------------#


import time
from time import sleep
import csv
import time
import datetime
#from datetime import datetime
import subprocess
from subprocess import Popen  # For executing external scripts
import os
import shutil
import glob
import numpy as np
import sys
import logging
import re
import RPi.GPIO as GPIO
import fcntl

# EEPROM safety module -- must live alongside this script (or on PYTHONPATH)
from eeprom_safe import (
    ensure_eeprom_configured,
    should_reboot_before_poweroff,
    reboot_to_apply,
)

# First-boot filesystem-expansion guard -- must live alongside this script too
from firstboot_guard import wait_for_firstboot_settle, filesystem_needs_expansion
import schedule_core
import clock_battery

# Hardware abstraction (Mothbox Pro PCB vs DIY relay build) -- alongside too
try:
    import mothbox_hw
except Exception as _e:
    print(f"[Scheduler] mothbox_hw unavailable ({_e}); scripts will assume Mothbox Pro")
    mothbox_hw = None

# -----Scheduler Functions-------------------
def configure_display_for_mode(mode):
    """
    Default boot target is multi-user (headless) -- set once via:
        sudo systemctl set-default multi-user.target
    This function only starts the desktop when explicitly needed (DEBUG/PARTY).
    """
    desktop_modes = {"DEBUG", "PARTY"}

    if mode in desktop_modes:
        print(f"Mode is {mode} -- starting graphical desktop for this session")
        subprocess.run(
            ["sudo", "systemctl", "start", "graphical.target"],
            check=False
        )
    else:
        print(f"Mode is {mode} -- running headless (multi-user default)")
        # Nothing to do -- headless is already the boot default


def configure_wifi_for_mode(mode):
    """
    Enable wifi for modes that need it, disable for everything else.
    Uses existing MothPower scripts.
    """
    MOTHPOWER = "/home/pi/Desktop/Mothbox/scripts/MothPower"
    wifi_modes = {"DEBUG", "PARTY", "HI_POW"}

    if mode in wifi_modes:
        print(f"Mode is {mode} -- enabling wifi")
        subprocess.run(["bash", f"{MOTHPOWER}/stop_lowpower.sh"], check=False)
        subprocess.run(["bash", f"{MOTHPOWER}/powerup_wifi.sh"], check=False)
    else:
        print(f"Mode is {mode} -- disabling wifi")
        subprocess.run(["bash", f"{MOTHPOWER}/lowpower.sh"], check=False)
        
def get_pi_ram_mb():
    """Returns total RAM in MB by reading /proc/meminfo, or None on failure."""
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    kb = int(line.split()[1])
                    return kb // 1024
    except Exception:
        pass
    return None


def get_os_codename():
    """
    Returns the Debian/Raspbian OS codename (e.g. 'bookworm', 'bullseye')
    by reading /etc/os-release, or 'unknown' on failure.
    """
    try:
        with open("/etc/os-release", "r") as f:
            for line in f:
                if line.startswith("VERSION_CODENAME="):
                    return line.split("=", 1)[1].strip().strip('"').lower()
    except Exception:
        pass
    return "unknown"


def _uptime_s():
    """Seconds since boot -- the number that makes a boot-time race obvious in the log."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return -1.0


def _kernel_camera_log():
    """Last few kernel lines about the camera sensor, to tell 'not ready' from 'probe failed'."""
    try:
        r = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=10)
        lines = [l for l in (r.stdout or "").splitlines()
                 if "ov64a40" in l or "imx" in l.lower() or "cfe" in l.lower()]
        return lines[-6:]
    except Exception:
        return []


def gps_devices_attached(timeout_s=2.0):
    """
    Ask gpsd how many devices it actually has, so a box with no GPS (nearly all
    of them) does not spend 10 s every boot waiting for one. Asking gpsd is the
    authoritative check: looking for /dev/ttyAMA0 and friends false-positives on
    any Pi, GPS or not.

    Returns the device count, or -1 if gpsd could not be reached at all.
    """
    import socket, json
    try:
        with socket.create_connection(("127.0.0.1", 2947), timeout=timeout_s) as sk:
            sk.settimeout(timeout_s)
            sk.sendall(b'?DEVICES;\n')
            deadline = time.time() + timeout_s
            buf = ""
            while time.time() < deadline:
                try:
                    chunk = sk.recv(4096).decode("utf-8", "replace")
                except Exception:
                    break
                if not chunk:
                    break
                buf += chunk
                for line in buf.splitlines():
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("class") == "DEVICES":
                        return len(msg.get("devices", []))
            return 0
    except Exception:
        return -1


_SENSOR_DRIVER = re.compile(r"/(ov\d+\w*|imx\d+\w*|ar\d{4}\w*|arducam[\w-]*)$", re.I)


def _camera_sensor_bound():
    """
    Ask the KERNEL whether a camera sensor has bound to its driver, rather than
    inferring it from how long boot has taken.
      True  -- a sensor is bound: the camera is present and probed
      False -- a sensor driver exists but nothing is bound: still probing, or probe failed
      None  -- no sensor driver in sysfs; cannot tell, so just try the camera
    """
    try:
        seen_driver = False
        for d in glob.glob("/sys/bus/i2c/drivers/*"):
            if not _SENSOR_DRIVER.search(d):
                continue
            seen_driver = True
            for e in os.listdir(d):
                if re.fullmatch(r"\d+-[0-9a-f]{4}", e):
                    return True
        return False if seen_driver else None
    except Exception:
        return None


def _camera_probe_failed():
    """True if the kernel already tried and failed to probe the sensor (bad cable)."""
    return any(("probe with driver" in l and "failed" in l) or "Failed to read chip id" in l
               for l in _kernel_camera_log())


def check_camera(total_wait_s=40, retry_every_s=5):
    """
    Check that a camera is connected and can actually stream, RETRYING until
    total_wait_s before giving up.

    The retry is the whole point. This runs as little as ~15 s into boot, and
    the sensor's I2C probe on the CSI bus is not always finished by then --
    libcamera then correctly reports "no cameras available" because at that
    instant there are none. A single shot at that moment produced a false
    "camera missing" warning (8 blinks) on a perfectly good box, while the
    same command run by hand a minute later always worked (Sept 2026).

    Streaming for 500 ms rather than just listing cameras is also deliberate:
    a ribbon unplugged AFTER boot still appears in the list, because the
    overlay registered the sensor at boot, and only fails when streaming
    starts with 'Remote I/O error'.

    On final failure the kernel's own view is printed, which distinguishes a
    probe that never happened from one that failed with -121 (bad cable).

    Returns True if a camera is detected, False otherwise.
    """
    tool = next((t for t in ("rpicam-hello", "libcamera-hello") if shutil.which(t)), None)
    if tool is None:
        print("[WARN] Neither rpicam-hello nor libcamera-hello found -- cannot verify camera.")
        return False

    deadline = time.time() + total_wait_s
    attempt = 0
    last = ""
    while True:
        attempt += 1

        # Ask the kernel before spending any time waiting. This turns the two
        # common cases into instant answers and leaves only the genuinely
        # ambiguous one -- driver loaded, sensor not bound yet -- to the retry.
        bound = _camera_sensor_bound()
        if bound is False and _camera_probe_failed():
            print(f"[WARN] Kernel reports the camera sensor probe FAILED "
                  f"({_uptime_s():.0f}s into boot) -- waiting will not help. "
                  f"Check the ribbon cable seating and orientation.")
            for l in _kernel_camera_log():
                print(f"       kernel: {l[-160:]}")
            return False
        if bound is False:
            if time.time() >= deadline:
                break
            print(f"[-]    Sensor not bound yet at {_uptime_s():.0f}s into boot "
                  f"(attempt {attempt}) -- driver still probing; retrying in {retry_every_s}s...")
            time.sleep(retry_every_s)
            continue

        try:
            r = subprocess.run([tool, "-n", "-t", "500"], capture_output=True, text=True, timeout=25)
            last = (r.stdout + r.stderr).strip()
            low = last.lower()
            # A camera that is BUSY is a camera that is PRESENT. If TakePhoto
            # (or anything else) holds it at this instant, the test fails for a
            # reason that has nothing to do with the cable. Reporting that as
            # "disconnected" is the false positive that cost whole sessions.
            if any(k in low for k in ("device or resource busy", "__init__ sequence did not complete",
                                      "failed to acquire camera", "camera in use", "resource temporarily unavailable")):
                print(f"[OK]   Camera is present but busy ({tool}) -- counting that as connected.")
                return True
            if r.returncode == 0 and "no cameras available" not in low:
                print(f"[OK]   Camera detected via {tool} "
                      f"(attempt {attempt}, {_uptime_s():.0f}s into boot).")
                return True
        except subprocess.TimeoutExpired:
            last = f"{tool} timed out after 25s"
        except Exception as e:
            last = f"{e.__class__.__name__}: {e}"

        if time.time() >= deadline:
            break
        print(f"[-]    No camera yet at {_uptime_s():.0f}s into boot (attempt {attempt}) -- "
              f"the driver may still be probing; retrying in {retry_every_s}s...")
        time.sleep(retry_every_s)

    print(f"[WARN] {tool}: no camera after {attempt} attempt(s) over {total_wait_s}s "
          f"({_uptime_s():.0f}s into boot).")
    print(f"       Last output: {last[:200]}")
    for l in _kernel_camera_log():
        print(f"       kernel: {l[-160:]}")
    return False


def determinePiModel():

    # Check Raspberry Pi model using CPU info
    cpuinfo = open("/proc/cpuinfo", "r")
    model  = None
    serial = None
    themodel = None

    for line in cpuinfo:
        if line.startswith("Model"):
            model = line.split(":")[1].strip()
        if line.startswith("Serial"):
            serial = line.split(":")[1].strip()
    cpuinfo.close()

    ram_mb   = get_pi_ram_mb()
    os_name  = get_os_codename()

    ram_str    = f"{ram_mb} MB RAM" if ram_mb is not None else "unknown RAM"
    serial_str = serial if serial else "unknown serial"

    # Execute function based on model
    if model:
        print(f"Model:  {model}")
        print(f"RAM:    {ram_str}")
        print(f"Serial: {serial_str}")
        print(f"OS:     {os_name}")
        if "Pi 4" in model:
            themodel = 4
        elif "Pi 5" in model:
            themodel = 5
        else:
            print("Unknown Raspberry Pi model detected. Going to treat as model 5")
            themodel = 5
    else:
        print("Error: Could not read Raspberry Pi model information.")
        print(f"RAM:    {ram_str}")
        print(f"Serial: {serial_str}")
        print(f"OS:     {os_name}")
        themodel = 5
    return themodel


def read_csv_into_lists(filename, encoding="utf-8"):
    """
    Reads a CSV file with headers into separate lists for each column, handling diacritical marks.

    Args:
        filename: The path to the CSV file.
        encoding: The character encoding of the CSV file (default: 'utf-8').

    Returns:
        A dictionary where keys are column names (strings) and values are lists of data (strings).
    """
    data = {}
    with open(filename, "r", newline="", encoding=encoding) as csvfile:
        reader = csv.reader(csvfile)
        # Read header row
        headers = next(reader)
        # Initialize empty lists for each column
        for header in headers:
            data[header] = []
        # Read data rows and populate corresponding lists by column index
        for row in reader:
            for i, value in enumerate(row):
                if value:  # Only append non-empty values
                    data[headers[i]].append(value)
    return data


def get_serial_number():
    """
    This function retrieves the Raspberry Pi's serial number from the CPU info file.
    """
    try:
        with open("/proc/cpuinfo", "r") as cpuinfo:
            for line in cpuinfo:
                if line.startswith("Serial"):
                    return line.split(":")[1].strip()
    except (IOError, IndexError):
        return None


def word_to_seed(word, encoding="utf-8"):
    """Converts a serial number string to a stable unique seed for numpy."""
    import hashlib
    hash_bytes = hashlib.md5(word.encode(encoding)).digest()
    # Convert first 4 bytes of hash to an integer within numpy's valid seed range
    seed = int.from_bytes(hash_bytes[:4], byteorder='big') % (2**32)
    return seed

def word_to_seed_old(word, encoding="utf-8"):
    """Converts a word to a number suitable for np.random.seed using encoding, sum, and modulo.
    Args:
        word: The string to be converted.
        encoding: The character encoding of the word (default: 'utf-8').

    Returns:
        An integer seed value within the valid range for np.random.seed.
    """
    encoded_word = word.encode(encoding)
    seed = sum(encoded_word) # this can lead to same numbers if the digits in the  serial number are the same
    max_seed_value = 2**32 - 1
    return seed


def set_timings(mins, hours, weekdays, runtimes):
    atomic_update_kv(os.path.join(CONTROL_ROOT, "minutes.txt"), "minutes", mins)
    atomic_update_kv(os.path.join(CONTROL_ROOT, "hours.txt"),   "hours",   hours)
    atomic_update_kv(os.path.join(CONTROL_ROOT, "weekdays.txt"),"weekdays",weekdays)
    atomic_update_kv(os.path.join(CONTROL_ROOT, "runtime.txt"), "runtime", runtimes)

def generate_unique_name(serial, lang):
    """
    Generates a unique name based on the Raspberry Pi's serial number.
    Args:
        serial: The Raspberry Pi's serial number as a string.

    Returns:
        A string containing a random word and a suffix based on the serial number.
    """
    # Use the serial number to create a unique seed for the random word generation.
    word_seed = word_to_seed(serial)
    np.random.seed(word_seed)
    # Create two word phrases
    if lang == 0:  # English
        extra = adjectives + colors + verbs
        random_extra = str(np.random.choice(extra, 1)[0]).lower()
        random_animal = str(np.random.choice(animals, 1)[0]).capitalize()
        finalCombo = random_extra + random_animal
    elif lang == 1:  # Spanish
        extra = adjectivos + colores + verbos + sustantivos
        random_extra = np.random.choice(extra, 1)[0]
        random_animal = np.random.choice(animales, 1)[0]
        finalCombo = (
            str(random_animal).lower() + str(random_extra).capitalize()
        )  # generally putting a noun before descriptor in spanish
    elif lang == 3:  # Spanglish
        extra = (
            adjectivos
            + colores
            + verbos
            + sustantivos
            + adjectives
            + verbs
            + adjectivos
            + colores
            + verbos
            + sustantivos
        )
        dosanimales = animals + animales
        random_extra = np.random.choice(extra, 1)[0]
        random_animal = np.random.choice(dosanimales, 1)[0]
        finalCombo = str(random_extra).lower() + str(random_animal).capitalize()
    return finalCombo


def find_file(path, filename, depth=1):
    """
    Recursively searches for a file within a directory and its subdirectories
    up to a specified depth.
    Args:
        path: The path to start searching from.
        filename: The name of the file to find.
        depth: The maximum depth of subdirectories to search (default 1).

    Returns:
        The full path to the file if found, otherwise None.
    """
    for root, dirs, files in os.walk(path):
        if (
            filename in files
            and len(root.split(os.sep)) - len(path.split(os.sep)) <= depth
        ):
            return os.path.join(root, filename)
        if depth > 1:
            # Prune directories beyond the specified depth
            dirs[:] = [
                d
                for d in dirs
                if len(os.path.join(root, d).split(os.sep)) - len(path.split(os.sep))
                <= depth
            ]
    return None

SETTING_DEFAULTS = {
    "runtime":      0,
    "photo_interval": 1, 
    "backup_interval": 5,
    "hardware": "auto",
    "utc_off":      0,
    "ssid":         None,
    "wifipass":     None,
    "manualTime":   "1986-04-06 11:11:11",
    "autoSystemTime": "true",
    "timezone":     "Africa/Timbuktu",
    "autoname":     "true",
    "name":         "ErrorName",
    "onlyflash":    0,
    "bat_voltage":  1.0,
    "bat_Wh":       10.0,
    "bat_80perVolts": 2.0,
    "bat_20perVolts": 1.0,
    "rtc_battery":  None,   # None = row missing: leave charging as config.txt set it
}


def _open_csv_robust(filename):
    """
    Opens a CSV file that may have been saved by Excel.
    Handles:
      - UTF-8 BOM  (utf-8-sig strips the BOM automatically)
      - Windows CRLF line endings  (universal newlines via newline='')
      - Latin-1 / cp1252 fallback  (Excel 'Save as CSV' on Windows)
    Returns an open file object; caller is responsible for closing it.
    """
    for encoding in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            f = open(filename, newline="", encoding=encoding)
            f.read(512)        # probe — will raise on bad encoding
            f.seek(0)
            return f, encoding
        except (UnicodeDecodeError, LookupError):
            try:
                f.close()
            except Exception:
                pass
    # Last-ditch: ignore undecodable bytes
    f = open(filename, newline="", encoding="utf-8", errors="replace")
    return f, "utf-8-replace"


def load_settings(filename):
    result = dict(SETTING_DEFAULTS)
    newwifidetected = False

    try:
        f, enc = _open_csv_robust(filename)
        print(f"[i] Reading settings with encoding: {enc}")
    except FileNotFoundError:
        print(f"Error: CSV file not found: {filename}")
        return None

    try:
        reader = csv.DictReader(f)

        # Excel sometimes renames / reformats the header row.
        # Normalise header names: strip whitespace + BOM residue, lowercase.
        if reader.fieldnames:
            reader.fieldnames = [
                h.strip().lstrip("\ufeff").upper()
                for h in reader.fieldnames
            ]

        # Require at minimum a SETTING and VALUE column
        if not reader.fieldnames or \
           "SETTING" not in reader.fieldnames or \
           "VALUE" not in reader.fieldnames:
            print(f"[!] CSV header malformed (got {reader.fieldnames}) -- returning None")
            return None

        for row in reader:
            # Skip completely blank rows (Excel often appends these)
            if not any(v and v.strip() for v in row.values()):
                continue

            raw_setting = row.get("SETTING", "") or ""
            raw_value   = row.get("VALUE",   "") or ""

            setting = raw_setting.strip().lstrip("\ufeff")
            value   = raw_value.strip()

            if not setting:
                continue   # empty setting name → skip silently

            try:
                if setting in ("day", "weekday", "hour", "minute",
                               "minutes_period", "second"):
                    result[setting] = value
                    print(setting + value)
                elif setting == "runtime":
                    result["runtime"] = int(value)
                elif setting in ("ssid", "wifissid"):
                    result["ssid"] = value
                    newwifidetected = True
                elif setting in ("wifipass", "wifipassword"):
                    result["wifipass"] = value
                    newwifidetected = True
                elif setting == "manualTime":
                    result["manualTime"] = value
                elif setting == "autoSystemTime":
                    result["autoSystemTime"] = value.strip().lower()
                elif setting == "timezone":
                    result["timezone"] = value
                elif setting == "autoname":
                    result["autoname"] = value.strip().lower()
                elif setting == "name":
                    result["name"] = value
                elif setting == "onlyflash":
                    result["onlyflash"] = int(value)
                elif setting == "bat_voltage":
                    result["bat_voltage"] = float(value)
                elif setting == "bat_Wh":
                    result["bat_Wh"] = float(value)
                elif setting == "bat_80perVolts":
                    result["bat_80perVolts"] = float(value)
                elif setting == "bat_20perVolts":
                    result["bat_20perVolts"] = float(value)
                elif setting == "photo_interval":
                    result["photo_interval"] = int(value)
                elif setting == "backup_interval":
                    # Consumed by Backup_Files.py directly from the CSV; parsed
                    # here only so it is recognised (no "Unknown setting" nag)
                    # and validated in the boot log. Valid 1-999, default 5.
                    bi = int(value)
                    if not 1 <= bi <= 999:
                        print(f"WARNING: backup_interval={bi} outside 1-999, Backup_Files will use 5")
                    result["backup_interval"] = bi
                elif setting == "rtc_battery":
                    # rechargeable | regular -- the RTC coin cell. Checked and
                    # applied by clock_battery.py at every boot.
                    result["rtc_battery"] = value
                elif setting == "hardware":
                    # auto | pro | diy -- read directly by mothbox_hw.py; parsed
                    # here only so it is recognised and shown in the boot log.
                    result["hardware"] = value.strip().lower()
                else:
                    print(f"Warning: Unknown setting: {setting}. Ignoring.")
            except (ValueError, TypeError):
                print(f"WARNING: Could not parse '{setting}' value '{value}', "
                      f"using default {result.get(setting, 'N/A')}")

    except Exception as e:
        print(f"[!] Unexpected error reading settings CSV: {e}")
        return None
    finally:
        f.close()

    result["newwifidetected"] = newwifidetected
    return result


def run_cmd(cmd):
    """Run a shell command safely"""
    subprocess.run(cmd, shell=True, check=False)


def atomic_write(path, content):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def atomic_update_kv(path, key, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    lines = []
    if os.path.exists(path):
        with open(path, "r") as f:
            for line in f:
                if "=" in line:
                    lines.append(line)

    found = False
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i] = f"{key}={value}\n"
            found = True

    if not found:
        lines.append(f"{key}={value}\n")

    atomic_write(path, "".join(lines))

def get_control_values(filename):
    """
    Safely reads key=value pairs from a control file.
    Returns {} if file does not exist or is unreadable.
    Ignores malformed lines.
    Never raises on read failure.
    """

    control_values = {}

    filename = str(_resolve_read(filename))
    if not os.path.exists(filename):
        return control_values

    try:
        with open(filename, "r") as file:
            for line in file:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue

                key, value = line.split("=", 1)
                control_values[key.strip()] = value.strip()

    except Exception as e:
        print(f"[!] Warning: Failed reading {filename}: {e}")

    return control_values






# ---------------------------------------------------------------------------
# End-of-session final backup
# ---------------------------------------------------------------------------
FINAL_BACKUP_TIMEOUT_S    = 15 * 60  # hard cap: a wedged USB drive must not keep the box awake all night
TAKEPHOTO_DRAIN_TIMEOUT_S = 120      # longest we wait for an in-flight capture to finish


def wait_for_takephoto_to_finish(timeout_s=TAKEPHOTO_DRAIN_TIMEOUT_S):
    """
    The session state stops cron from launching any NEW TakePhoto.py, but one
    may already be mid-capture when a window ends (HDR stacks take a while).
    Backing up at that moment would copy a truncated JPEG to the USB drive and
    then move the original out of photos/. Poll until it exits.

    Returns True if no TakePhoto.py is running, False if we gave up waiting.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        # pgrep exit status: 0 = at least one match, 1 = none. pgrep never
        # matches itself.
        rc = subprocess.run(
            ["pgrep", "-f", "Mothbox/TakePhoto.py"],
            capture_output=True,
        ).returncode
        if rc != 0:
            return True
        time.sleep(2)
    return False


def stop_capture_and_lights():
    """
    The first half of ending a window: let a photo in progress finish, then
    switch the attract lights off. Never raises.

    The session state has already stopped cron from starting any NEW
    TakePhoto.py, but one may be mid-capture (HDR stacks take a while), and
    backing up at that moment would copy a truncated JPEG.
    """
    # 1. Let an in-flight capture finish. Its flash/attract cycle needs the
    #    lights, so they stay on for (at most) TAKEPHOTO_DRAIN_TIMEOUT_S here.
    try:
        if wait_for_takephoto_to_finish():
            log_ok("No TakePhoto.py running -- photo capture has stopped.")
        else:
            log_warn(f"TakePhoto.py still running after {TAKEPHOTO_DRAIN_TIMEOUT_S}s "
                     f"-- proceeding anyway.")
    except Exception as e:
        log_warn(f"Could not check for a running TakePhoto.py ({e}) -- continuing.")

    # 2. Lights OFF, unconditionally. The attract lights are the main power
    #    drain, and a backup can take minutes. It retries once because a copy
    #    with the lights on is the one outcome we never want.
    lights_off = False
    for attempt in (1, 2):
        try:
            rc = subprocess.run(["python", "/home/pi/Desktop/Mothbox/Attract_Off.py"],
                                check=False, timeout=30).returncode
            if rc == 0:
                lights_off = True
                break
            log_warn(f"Attract_Off.py exited {rc} (attempt {attempt}/2).")
        except Exception as e:
            log_warn(f"Attract_Off.py failed (attempt {attempt}/2): {e}")
        time.sleep(1)
    if lights_off:
        log_ok("Attract lights off.")
    else:
        log_warn("Could not confirm attract lights are off -- carrying on; "
                 "power-off will cut them if the box is going down.")


def final_backup_before_shutdown():
    """
    End-of-session safety net so the box powers down fully backed up.

    Backup_Files.py runs from cron but throttles itself to once per
    backup_interval minutes (default 5), and cron's runs stop as soon as the
    session state leaves active/waiting -- so any photo taken after the last
    throttled backup used to sit in photos/ until the NEXT session.

    Ordering:
      1. wait for an in-flight TakePhoto.py to finish  (partial-JPEG hazard)
      2. attract lights off                            (don't burn 12V while copying)
      3. Backup_Files.py --final                       (bypasses state check + throttle)

    Bounded by FINAL_BACKUP_TIMEOUT_S and covered by the shutdown deadman. A
    partial copy on the USB drive is simply overwritten next time (copy2 with
    dirs_exist_ok), and the originals stay in photos/ because the move to
    photos_backedup/ only happens after a completed copy. Never raises.
    """
    log_section("SHUTDOWN -- Final Backup")
    stop_capture_and_lights()

    # 3. The backup itself.
    try:
        log_info("Running final backup (bypassing interval throttle)...")
        t0 = time.time()
        result = subprocess.run(
            ["python3", "/home/pi/Desktop/Mothbox/Backup_Files.py", "--final"],
            capture_output=True, text=True, timeout=FINAL_BACKUP_TIMEOUT_S,
        )
        out = result.stdout.strip()
        if out:
            print(out)
        elapsed = time.time() - t0
        if result.returncode == 0:
            log_ok(f"Final backup complete in {elapsed:.0f}s.")
        else:
            detail = result.stderr.strip() or "see output above"
            log_warn(f"Final backup exited {result.returncode} after {elapsed:.0f}s -- {detail}. "
                     f"Unbacked photos stay in photos/ for the next session.")
    except subprocess.TimeoutExpired:
        log_warn(f"Final backup exceeded {FINAL_BACKUP_TIMEOUT_S}s and was killed. "
                 f"Unbacked photos stay in photos/; shutting down to protect the battery.")
    except Exception as e:
        log_warn(f"Final backup step failed: {e} -- continuing shutdown.")


def start_background_backup():
    """
    End of a window the box stays up after (a short gap, or HI POWER): start
    the final backup and let it run while the box waits, dark, for the next
    window. Backup_Files.py itself makes sure only one backup runs at a time.
    Never raises.
    """
    try:
        with open("/home/pi/Desktop/Mothbox/logs/Backup_log.txt", "a") as log_file:
            subprocess.Popen(["python3", "/home/pi/Desktop/Mothbox/Backup_Files.py", "--final"],
                             stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)
        log_info("Final backup of the window started in the background.")
    except Exception as e:
        log_warn(f"Could not start the background backup ({e}) -- cron's backup will catch up.")


FALLBACK_WAKE_AFTER_DEADMAN_S = 120   # the fallback alarm fires this long after the deadman would


def next_wake_epoch():
    """The wake alarm for the next window -- or an hour from now if the schedule never leaves a gap."""
    nxt = schedule_core.next_window_start(sched, datetime.datetime.now())
    if nxt is None:
        return int(time.time()) + 3600
    return schedule_core.to_epoch(schedule_core.wake_time(nxt))


def write_session_info(phase, plan=None):
    """
    What the display's 'when' box shows (display_render.when_field):
      active  -> SESSION ENDS  (session_end; -1 = always on)
      waiting -> STARTS        (next_start)
      anything else            -> NEXT WAKE, from nextwake.txt
    """
    end = start = 0
    if plan is not None:
        if plan.state == schedule_core.ACTIVE:
            end = -1 if plan.session_end is None else schedule_core.to_epoch(plan.session_end)
        if plan.next_start is not None:
            start = schedule_core.to_epoch(plan.next_start)
    try:
        atomic_write(os.path.join(CONTROL_ROOT, "session.txt"),
                     f"phase={phase}\nsession_end={end}\nnext_start={start}\n")
    except Exception as e:
        log_warn(f"Could not write session.txt for the display: {e}")


def power_off(from_session=False, off_mode=False):
    """
    The one way the box turns itself off: STANDBY at boot, the end of a
    session, and OFF.

    Returns "powered_off" once `shutdown -h now` has been issued -- or
    "aborted" if, by the time everything else is done, the next window is so
    close that staying up is the right call (never in OFF mode: OFF is off).

    The wake alarm is set LAST, immediately before `shutdown -h now`. 6.0.x
    set it first and then ran a final backup that can take 15 minutes, so with
    a short gap the alarm could fire while the box was still on, get used up,
    and the box powered off with nothing left to wake it. The fallback alarm
    set at the start only matters if the deadman has to force the power-off:
    it fires just after the deadman would.
    """
    log_section("SHUTDOWN")
    session.write_state(session.SHUTTING_DOWN)

    fallback = max(next_wake_epoch(),
                   int(time.time()) + SHUTDOWN_DEADMAN_S + FALLBACK_WAKE_AFTER_DEADMAN_S)
    clear_wakeup_alarm()
    set_wakeup_alarm(fallback, record=False)
    deadman = arm_shutdown_deadman()

    # --- EEPROM safety guard: never power off with a staged flash pending ---
    # If eeprom_safe staged a bootloader update earlier this boot (or a prior
    # boot) and it hasn't been applied/verified yet, a halt here could
    # interrupt the SPI flash mid-write and corrupt the bootloader. Reboot
    # cleanly instead so recovery.bin can apply it on stable power.
    if should_reboot_before_poweroff():
        print("[!] EEPROM flash still pending -- rebooting to apply instead of powering off.")
        reboot_to_apply()
        return "powered_off"
    # --- End EEPROM guard ---

    # --- Guard: don't power off while the first-boot filesystem resize may
    # still be in flight. A fresh, shrunk image's root filesystem is much
    # smaller than the physical SD card until this completes; cutting power
    # mid-resize can corrupt the root filesystem.
    #
    # We update the ePaper display with a "please wait" message BEFORE
    # blocking -- otherwise the wait is silent on-screen. Since e-ink holds
    # its image with zero power, this message will sit on the display for the
    # whole wait. ---
    if filesystem_needs_expansion():
        log_warn("Filesystem not yet expanded -- updating ePaper display with a "
                 "'please wait' message before blocking on the resize.")
        GPIO.cleanup()
        run_helper("/home/pi/Desktop/Mothbox/UpdateDisplay.py", "UpdateDisplay error")
    wait_for_firstboot_settle(log=log_warn)
    # --- End first-boot guard ---

    if from_session:
        final_backup_before_shutdown()
    else:
        run_mode_script("/home/pi/Desktop/Mothbox/Attract_Off.py")   # nothing ran, nothing to back up

    if not off_mode:
        atomic_update_kv(os.path.join(CONTROL_ROOT, "mode.txt"), "mode", "STANDBY")
    write_session_info("standby")
    atomic_update_kv(os.path.join(CONTROL_ROOT, "nextwake.txt"), "nextwake", next_wake_epoch())

    GPIO.cleanup()
    log_info("Updating ePaper display before shutdown (if available)...")
    run_helper("/home/pi/Desktop/Mothbox/UpdateDisplay.py", "UpdateDisplay error")
    if from_session:
        run_script("/home/pi/Desktop/Mothbox/Diagnostics.py", "Shutdown_Check", show_output=True)

    # Decide again, with the time it is NOW: the backup alone can take minutes.
    # Same lookahead as the session loop, or a power-off that took under 3 s
    # would still see the closing seconds of its own window and stay up.
    if not off_mode:
        plan = schedule_core.plan_at(
            sched, datetime.datetime.now() + datetime.timedelta(seconds=session.LOOKAHEAD_S))
        if plan.state != schedule_core.SLEEP:
            log_ok("The next window is close now -- staying up instead of powering off.")
            disarm_shutdown_deadman(deadman)
            atomic_update_kv(os.path.join(CONTROL_ROOT, "mode.txt"), "mode", "ACTIVE")
            write_session_info("waiting" if plan.state == schedule_core.WAITING else "active", plan)
            clear_wakeup_alarm()
            set_wakeup_alarm(next_wake_epoch())
            return "aborted"

    # The wake alarm, LAST: nothing slow may run between this and the halt.
    wake = next_wake_epoch()
    clear_wakeup_alarm()
    set_wakeup_alarm(wake)
    log_ok(f"Wake alarm set for {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(wake))} "
           f"-- powering off now.")
    os.system("sudo shutdown -h now")
    return "powered_off"








def enable_shutdown():
    atomic_update_kv(
        os.path.join(CONTROL_ROOT, "shutdown_enabled.txt"),
        "shutdown_enabled",
        "true"
    )
'''
def enable_onlyflash():
    atomic_update_kv(
        os.path.join(CONTROL_ROOT, "onlyflash.txt"),
        "onlyflash",
        onlyflash
    )
'''

def stopcron():
    """Executes the '/home/pi/Desktop/Mothbox/StopCron.py' script."""
    print("stopping cron, you need to enable it yourself if needed, or reboot")
    subprocess.run(["python", "/home/pi/Desktop/Mothbox/StopCron.py"])


def is_wifi_already_known(ssid):
    """
    Returns True if NetworkManager already has a saved WiFi connection whose
    SSID matches the given string.

    Uses 'nmcli -t -f NAME,TYPE connection show' to list all saved profiles,
    then checks wifi-type connections by their SSID field specifically.
    This avoids a false-positive where a connection profile has a different
    name than the SSID (e.g. NM auto-names them differently).
    """
    try:
        # First pass: check connection names directly (fast, covers most cases)
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
            capture_output=True, text=True, check=True
        )
        wifi_profile_names = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(":")
            if len(parts) >= 2 and "wireless" in parts[1]:
                wifi_profile_names.append(parts[0])

        if ssid in wifi_profile_names:
            return True

        # Second pass: check actual SSID field of each wifi profile, because
        # NetworkManager profile names don't always match the SSID exactly
        for profile_name in wifi_profile_names:
            try:
                detail = subprocess.run(
                    ["nmcli", "-t", "-f", "802-11-wireless.ssid", "connection",
                     "show", profile_name],
                    capture_output=True, text=True, check=True
                )
                for line in detail.stdout.splitlines():
                    if ":" in line:
                        val = line.split(":", 1)[1].strip()
                        if val == ssid:
                            return True
            except subprocess.CalledProcessError:
                continue

        return False

    except subprocess.CalledProcessError as e:
        print(f"[WARN] Could not query NetworkManager connections: {e}")
        return False  # Assume unknown -- safer to try provisioning than to skip it


def provision_wifi(ssid, password):
    """
    Adds a new WiFi network to NetworkManager and attempts to connect.
    Skips silently if the SSID is already a saved connection.

    Returns True if the network was newly added and connection succeeded,
    False if it was skipped (already known) or if connection failed.
    """
    if not ssid or ssid in ("examplessid", "", None):
        print("No valid SSID provided -- skipping wifi provisioning.")
        return False

    if not password or password in ("examplepass", "", None):
        print(f"No valid password provided for '{ssid}' -- skipping wifi provisioning.")
        return False

    if is_wifi_already_known(ssid):
        print(f"WiFi '{ssid}' is already a saved connection -- skipping.")
        return False

    print(f"New WiFi SSID detected: '{ssid}' -- attempting to add and connect...")
    try:
        subprocess.run(
            ["nmcli", "dev", "wifi", "connect", ssid, "password", password],
            check=True
        )
        print(f"[OK] Successfully added and connected to WiFi network: '{ssid}'")
        return True
    except subprocess.CalledProcessError as error:
        print(f"[!] Failed to connect to WiFi network '{ssid}': {error}")
        return False


def handle_wifi_provisioning(ssid, password, mode):
    """
    Entry point for wifi provisioning logic.  Behaviour depends on mode:

    DEBUG / HI_POW / PARTY:
        Wifi is already up (configure_wifi_for_mode ran before this).
        Attempt provisioning immediately.  On success, clear the CSV values
        so the same credentials are not re-provisioned on every boot.

    ACTIVE / STANDBY / OFF:
        We do not want to spin up wifi just to provision -- it would delay
        the boot sequence.  Instead, write a "pending" flag to controls so
        the next DEBUG/HI_POW session picks it up automatically.
        (The user just needs to flip the Debug switch once to provision.)

    In all cases, if there is nothing new to provision the function returns
    immediately without side-effects.
    """
    PENDING_PATH = os.path.join(CONTROL_ROOT, "wifi_pending.txt")

    # Normalise: treat placeholder values the same as empty
    placeholder_ssids  = {"examplessid", "", None}
    placeholder_passes = {"examplepass", "", None}

    has_new_ssid = ssid not in placeholder_ssids
    has_new_pass = password not in placeholder_passes

    log_info(f"WiFi provisioning check -- SSID from CSV: {repr(ssid)}, new={has_new_ssid}, mode={mode}")

    if not has_new_ssid:
        # Nothing in the CSV -- but check if a previous boot left a pending job
        pending = get_control_values(PENDING_PATH)
        pending_ssid = pending.get("pending_ssid", "")
        pending_pass = pending.get("pending_pass", "")
        if not pending_ssid or pending_ssid in placeholder_ssids:
            log_info("No new WiFi credentials found -- skipping.")
            return  # Genuinely nothing to do
        print(f"Found pending wifi provisioning request for '{pending_ssid}' from a previous boot.")
        ssid     = pending_ssid
        password = pending_pass
        has_new_ssid = True
        has_new_pass = bool(pending_pass)

    wifi_modes = {"DEBUG", "HI_POW", "PARTY"}

    if mode in wifi_modes:
        # Wifi is up -- attempt provisioning now
        success = provision_wifi(ssid, password if has_new_pass else "")
        if success:
            # Clear the CSV credentials so they don't re-trigger on every boot.
            # We write the placeholder values back rather than blanking the rows,
            # so the CSV structure stays valid for the user to edit again later.
            print("Clearing provisioned credentials from CSV...")
            # Try both key names so it works regardless of which the user's CSV uses
            update_csv_setting(usersettingsFpath, "wifissid", "examplessid")
            update_csv_setting(usersettingsFpath, "ssid",     "examplessid")
            update_csv_setting(usersettingsFpath, "wifipass", "examplepass")
            update_csv_setting(usersettingsFpath, "wifipassword", "examplepass")
            # Also clear any pending flag
            if os.path.exists(PENDING_PATH):
                os.remove(PENDING_PATH)
        else:
            # Connection failed -- leave pending flag so user can retry
            print("Provisioning unsuccessful. Will retry next time wifi is available.")
            atomic_update_kv(PENDING_PATH, "pending_ssid", ssid)
            atomic_update_kv(PENDING_PATH, "pending_pass", password if has_new_pass else "")
    else:
        # Wifi is off in this mode -- park the request for a future DEBUG session
        if not is_wifi_already_known(ssid):
            print(
                f"Mode is {mode} -- wifi is off. Saving '{ssid}' as pending; "
                f"switch to DEBUG mode to provision it."
            )
            atomic_update_kv(PENDING_PATH, "pending_ssid", ssid)
            atomic_update_kv(PENDING_PATH, "pending_pass", password if has_new_pass else "")
        else:
            print(f"WiFi '{ssid}' is already known -- nothing to do.")


def modify_hours(data, offsett_value, key="hour"):
    """
    Modifies a list of hours stored in a dictionary value by subtracting a static number from each hour,
    but only if the key matches the provided key (default: "hour").
    Args:
        data: A dictionary containing a key with a value as a string representing hours separated by semicolons.
        offsett_value: The static value to subtract from each hour (integer).
        key: The specific key in the dictionary to modify (default: "hour").

    Returns:
        A modified dictionary with the updated list of hours (if the key exists).
    """
    # Check if the key exists in the dictionary and value type is string (containing hours)
    if key in data and isinstance(data[key], str):
        # Split the string into a list of hours (integers)
        hours = [int(hour) for hour in data[key].split(";")]

        # Subtract the static value from each hour
        modified_hours = [(hour - offsett_value) % 24 for hour in hours]

        # Ensure hours are between 0 and 24 (negative numbers become 24-hour format)
        modified_hours = [hour if hour >= 0 else hour + 24 for hour in modified_hours]

        # Update the dictionary value with the modified list
        data[key] = ";".join(str(hour) for hour in modified_hours)

    return data  # Return the modified dictionary (or original if no modification)


def clear_wakeup_alarm():
    """
    Clears the existing wakeup alarm for the Raspberry Pi using /sys/class/rtc/rtc0/wakealarm.
    """
    # Open the wakealarm file for writing with sudo
    with open("/sys/class/rtc/rtc0/wakealarm", "w") as f:
        f.write("0")  # Write 0 to clear the alarm


MIN_WAKE_LEAD_S = 90   # never arm an alarm that could pass while we are still shutting down

def set_wakeup_alarm(epoch_time, record=True):
    """
    Sets the wakeup alarm for the Raspberry Pi using /sys/class/rtc/rtc0/wakealarm.

    The kernel compares the alarm against the RTC's own clock and silently
    DISABLES an alarm that is already in the past (the write still succeeds),
    so this also: refuses to arm anything less than MIN_WAKE_LEAD_S away,
    warns if the RTC and system clocks disagree, and reads the alarm back to
    log what the RTC actually holds.

    Args:
        epoch_time: A unix timestamp representing the next wakeup time.
        record: False for power_off()'s fallback alarm, so nextwake.txt (and
                the display) keep showing the real next wake.
    """
    epoch_time = int(epoch_time)
    now_sys = int(time.time())
    if epoch_time - now_sys < MIN_WAKE_LEAD_S:
        print(f"[!] Wake alarm only {epoch_time - now_sys}s away -- pushing it to {MIN_WAKE_LEAD_S}s "
              f"so it cannot be swallowed by the shutdown itself.")
        epoch_time = now_sys + MIN_WAKE_LEAD_S

    # RTC vs system clock: the alarm is judged against the RTC, not `date`.
    try:
        with open("/sys/class/rtc/rtc0/since_epoch") as f:
            rtc_now = int(f.read().strip())
        skew = rtc_now - now_sys
        if abs(skew) > 30:
            print(f"[!] RTC is {skew:+d}s from the system clock -- syncing RTC with 'hwclock -w' before arming.")
            os.system("sudo hwclock -w")
    except Exception as e:
        print(f"[!] Could not read RTC time: {e}")

    # Open the wakealarm file for writing
    with open("/sys/class/rtc/rtc0/wakealarm", "w") as f:
        # Write the epoch time in seconds
        f.write(str(epoch_time))
    logging.info("Set the Wakeup Alarm" + str(epoch_time))

    # Read back what the RTC actually holds -- empty means it was rejected.
    try:
        with open("/sys/class/rtc/rtc0/wakealarm") as f:
            armed = f.read().strip()
        if armed == str(epoch_time):
            print(f"[OK] RTC confirms wake alarm armed for {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(epoch_time))} (epoch {epoch_time})")
        else:
            print(f"[!!] RTC did NOT accept the wake alarm (holds {armed!r}, wanted {epoch_time}). "
                  f"The box will not wake on its own.")
    except Exception as e:
        print(f"[!] Could not read back wake alarm: {e}")
    #Write to controls here!
    #set_nextWakeinControls("/boot/firmware/mothbox_custom/system/controls.txt",epoch_time)
    if record:
        atomic_update_kv(os.path.join(CONTROL_ROOT, "nextwake.txt"), "nextwake", epoch_time)


# Must exceed the WORST CASE of everything the shutdown path can legitimately
# wait for, or the safety net becomes the fault: drain 120s + final backup 900s
# + display 120s + diagnostics 180s + sleeps = 1325s (22.1 min). 1800s leaves
# ~8 min of headroom. Keep this above that sum if any of those caps change.
SHUTDOWN_DEADMAN_S = 1800   # 30 min

def arm_shutdown_deadman(seconds=SHUTDOWN_DEADMAN_S):
    """
    Last-resort guarantee that the box actually powers off.

    Everything in the shutdown path is now individually bounded, but "bounded"
    only helps if the Python process is still running. A box that stays
    powered keeps its lights on, flattens the battery, and then loses power
    mid-write -- which is how a FAT boot partition gets corrupted and a box
    stops booting entirely (Oct 2026, 3 of 6 deployed units).

    So before the long part of shutdown, hand a detached shell a stopwatch.
    It survives this process hanging, being killed, or crashing. If we power
    off normally it dies with the system and never fires.

    Returns the timer's Popen (or None): power_off() hands it to
    disarm_shutdown_deadman() if it decides to stay up after all, so a
    shutdown that is correctly cancelled can never be force-halted by it.
    """
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c",
             f"sleep {seconds}; "
             f"echo '[deadman] shutdown did not complete in {seconds}s -- forcing power off'; "
             f"sync; sudo shutdown -h now; "
             f"sleep 180; sync; sudo systemctl poweroff -ff"],
            start_new_session=True)
        log_info(f"Shutdown deadman armed ({seconds}s) -- the box will power off even if this script hangs.")
        return proc
    except Exception as e:
        log_warn(f"Could not arm the shutdown deadman: {e}")
        return None


def disarm_shutdown_deadman(proc):
    """Cancel a deadman from arm_shutdown_deadman() (it runs in its own process group)."""
    if proc is None:
        return
    try:
        os.killpg(proc.pid, 15)
        log_info("Shutdown deadman disarmed.")
    except Exception as e:
        log_warn(f"Could not disarm the shutdown deadman ({e}) -- it may still power the box off; "
                 f"the fallback wake alarm brings it back.")


def run_helper(script_path, label, timeout_s=120, args=()):
    """
    Run a helper script (UpdateDisplay, GPS, TimezoneUpdater) with a hard
    timeout, killing it if it overruns.

    Every one of these used to be `Popen(...).communicate()` with no timeout.
    UpdateDisplay in particular can block forever inside the e-paper driver's
    busy-pin wait, and it is called from the shutdown path -- so one wedged
    display stopped the box ever powering off. Nothing here is important
    enough to be worth that.
    """
    try:
        process = subprocess.Popen(["python", script_path, *args],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:
        log_warn(f"{label}: could not start {script_path}: {e}")
        return
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.communicate(timeout=10)
        except Exception:
            pass
        log_warn(f"{label}: {script_path} exceeded {timeout_s}s and was killed -- continuing.")
        return
    if stderr and stderr.strip():
        log_warn(f"{label} error: {stderr.decode(errors='replace').strip()}")
    else:
        print(stdout.decode(errors="replace"))


def run_script(script_path, *args, show_output=True, timeout_s=180):
    """
    Run a Python script and optionally display its output.
    Extra arguments (args) are passed to the script.
    Can run like these examples
    # No label (shared log)
    run_script("/home/pi/Desktop/Mothbox/Diagnostics.py", show_output=True)

    # With label (custom log)
    run_script("/home/pi/Desktop/Mothbox/Diagnostics.py", "Battery Test", show_output=True)

    # Or with multiple words
    run_script("/home/pi/Desktop/Mothbox/Diagnostics.py", "Morning", "Check", "Field", "Site", show_output=True)
    """
    try:
        # Build the command list safely
        cmd = ["python3", script_path] + list(args)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout_s,      # never unbounded: this runs during shutdown
        )

        if show_output:
            output = result.stdout.strip()
            if output:
                print(output)

    except subprocess.TimeoutExpired:
        print(f"[!] {script_path} exceeded {timeout_s}s and was killed -- continuing.")
    except subprocess.CalledProcessError as e:
        print(f"[!] Error running {script_path}: {e.stderr.strip() if e.stderr else 'Unknown error'}")


def run_mode_script(script_path):
    """
    Run one of the per-mode scripts (DebugMode.py, Party.py, Attract_Off.py).

    Two deliberate choices:
      * "python3 <script>", never bare "<script>". Running it bare needs the
        executable bit, and an rsync from a machine where the file is not
        marked executable silently strips it.
      * never raises. This is called from the LAUNCH -- Final Mode block, just
        before the hand-over to the session; an exception there used to stop
        the session from ever starting, and every cron-launched TakePhoto /
        backup then exited quietly, disabling the box with no obvious cause.
    """
    try:
        subprocess.run(["python3", script_path], check=False)
    except Exception as e:
        log_warn(f"Could not run {script_path}: {e}")







def update_csv_setting(filename, setting_name, new_value):
    rows = []
    fieldnames = []
    updated = False

    try:
        with open(filename, 'r', newline='') as csv_file:
            reader = csv.DictReader(csv_file)
            fieldnames = reader.fieldnames
            for row in reader:
                if row["SETTING"] == setting_name:
                    row["VALUE"] = str(new_value)
                    updated = True
                rows.append(row)
    except FileNotFoundError:
        print(f"Error: Could not find {filename} to update.")
        return

    if not updated:
        print(f"Warning: Setting '{setting_name}' not found in CSV.")
        return

    # Write to a temp file first, then atomically rename over the original
    tmp = filename + ".tmp"
    try:
        with open(tmp, 'w', newline='') as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            csv_file.flush()
            os.fsync(csv_file.fileno())
        os.replace(tmp, filename)
        print(f"Successfully updated {setting_name} to {new_value}.")
    except Exception as e:
        print(f"[!] Error writing updated CSV: {e}")
        # Clean up the temp file if something went wrong
        if os.path.exists(tmp):
            os.remove(tmp)

def read_control(key, default=None):
    # _resolve_read prefers the ext4 copy and falls back to the legacy
    # boot-partition one, so an upgrading box keeps its existing values and
    # the shipped read-only files are still found. See mothbox_paths.py.
    path = str(_resolve_read(os.path.join(CONTROL_ROOT, f"{key}.txt")))
    if not os.path.exists(path):
        return default
    vals = get_control_values(path)
    return vals.get(key, default)




def read_switch_schedule(switch_vals):
    """
    Reads the physical switches to determine the wake schedule.
    h00-h23 = hours 0-23
    d0-d6   = days of week (1-7 in cron format, 1=Monday)
    Runtime is always 59 mins, minute is always 0.
    """
    # Collect active hours
    # Note h00 = midnight (hour 0), h1-h23 = hours 1-23
    hour_map = {"h00": 0}
    for i in range(1, 24):
        hour_map[f"h{i}"] = i

    active_hours = []
    for switch_name, hour_val in hour_map.items():
        if int(switch_vals.get(switch_name, 0)) == 1:
            active_hours.append(hour_val)

    # Collect active days
    # d0=Sunday, d1=Monday ... d6=Saturday
    # Cron on this system uses 1=Monday...7=Sunday to match CSV weekday convention
    # Python weekday: Monday=0...Sunday=6
    # We'll store as cron-compatible: 1=Monday, 7=Sunday
    day_cron_map = {
        "d0": 7,  # Sunday -> 7
        "d1": 1,  # Monday -> 1
        "d2": 2,
        "d3": 3,
        "d4": 4,
        "d5": 5,
        "d6": 6,  # Saturday -> 6
    }

    active_days = []
    for switch_name, cron_day in day_cron_map.items():
        if int(switch_vals.get(switch_name, 0)) == 1:
            active_days.append(cron_day)

    # Fallback: if no hours or days selected, use safe defaults
    if not active_hours:
        print("Switch mode: no hours selected, defaulting to hour 20")
        active_hours = [19,21,23,2,4] # default schedule
    if not active_days:
        print("Switch mode: no days selected, defaulting to all days")
        active_days = [1, 2, 3, 4, 5, 6, 7]

    hour_str   = ",".join(str(h) for h in sorted(active_hours))
    weekday_str = ",".join(str(d) for d in sorted(active_days))

    print(f"Switch schedule -- hours: {hour_str}, days: {weekday_str}")

    return {
        "minute":  "0",
        "hour":    hour_str,
        "weekday": weekday_str,
        "runtime": schedule_core.DEFAULT_RUNTIME_MIN,
    }

#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~####
#                       Main Code
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~###

def lights_on():
    """Attract lights on now (cron's Attract_On.py would within a minute)."""
    try:
        if mothbox_hw is not None:
            mothbox_hw.attract_on()
        else:
            run_mode_script("/home/pi/Desktop/Mothbox/Attract_On.py")
    except Exception as e:
        log_warn(f"Could not switch the attract lights on ({e}) -- cron's Attract_On.py will retry.")


def refresh_display():
    """Redraw the e-paper mid-session (no logo, no wait for USB drives to mount)."""
    try:
        GPIO.cleanup()
    except Exception:
        pass
    run_helper("/home/pi/Desktop/Mothbox/UpdateDisplay.py", "Display error", args=["--refresh"])


def ntp_synced():
    """True once the system clock has been set from the network."""
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=10).stdout
        return out.strip() == "yes"
    except Exception:
        return False


def clear_clock_lost_if_synced():
    """CLOCK LOST stays on the display until the time is set again."""
    try:
        if clock_battery.was_clock_lost(CONTROL_ROOT) and ntp_synced():
            os.system("hwclock -w")                 # the RTC gets the good time too
            clock_battery.clear_clock_lost(CONTROL_ROOT, time.time())
            log_ok("The clock was set from the network -- CLOCK LOST cleared.")
            return True
    except Exception as e:
        log_warn(f"Could not check whether the clock has been set ({e})")
    return False


def check_health(active_since):
    """
    Every 10 minutes of a session: are photos actually arriving?

    Report-only by design. It kills a capture that has hung (TakePhoto's own
    watchdog gives up at 150 s, so one still running after 5 minutes is stuck
    in a C call) and puts an alert on the display -- it never reboots, so a
    wrong verdict can't start a reboot loop.
    """
    try:
        ps = subprocess.run(["ps", "-eo", "pid=,etimes=,args="],
                            capture_output=True, text=True, timeout=10).stdout
        stuck = session.stuck_photo_pids(ps)
        for pid in stuck:
            log_warn(f"TakePhoto.py (pid {pid}) has run for over {session.STUCK_PHOTO_S}s -- killing it.")
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        if stuck:
            run_mode_script("/home/pi/Desktop/Mothbox/Flash_Off.py")   # it may have died with the flash on
    except Exception as e:
        log_warn(f"Could not check for a stuck TakePhoto.py: {e}")

    try:
        with open(os.path.join(CONTROL_ROOT, "last_photo_time.txt")) as f:
            last_photo = float(f.read().strip())
    except (OSError, ValueError):
        last_photo = None
    stalled = session.photos_stalled(time.time(), active_since, photo_interval, last_photo)
    camera_seen = read_control("camera", "ok") != "missing"
    try:
        min_gb = int(read_control("safetygb", 9)) - 1          # TakePhoto stops below this
        storage_low = shutil.disk_usage("/home/pi/Desktop/Mothbox").free < min_gb * 1024 ** 3
    except Exception:
        storage_low = False
    alert = session.health_alert(stalled, camera_seen, storage_low)
    session.write_health(alert)
    if alert:
        log_warn(f"Health check: {alert}")
    elif stalled:
        log_warn("Health check: no recent photos -- the camera is already reported missing.")
    else:
        log_info("Health check: photos are arriving.")


def run_session(keep_awake):
    """
    Hand the box to the session loop (session.run) until it powers off: open
    and close the real windows, stay up through short gaps, power off for
    long ones. HI POWER (keep_awake) is the same loop, but it never powers off.
    """
    started = time.time()
    active_since = [started]

    def on_activate(plan):
        active_since[0] = time.time()
        session.write_health(None)
        write_session_info("active", plan)
        lights_on()
        if time.time() - started > 60:          # booted inside the window: boot just drew this
            refresh_display()

    def on_deactivate(plan):
        stop_capture_and_lights()
        start_background_backup()
        # The loop's plan, read with its lookahead: a fresh plan_at(now) here
        # would still see the last seconds of the window and show "STARTS ?".
        write_session_info("waiting", plan)
        refresh_display()

    def every_10_min(state):
        if state == session.ACTIVE:
            check_health(active_since[0])
        clear_clock_lost_if_synced()
        refresh_display()

    def on_switch_change():
        if mothbox_hw is not None:
            mothbox_hw.all_lights_off()
        log_info("HI POWER switched off -- rebooting so the normal startup takes over.")
        os.system("sudo reboot")

    def read_switches():
        return mothbox_hw.read_switches() if mothbox_hw is not None else {"Active": 1, "HI": 1}

    reason = session.run(
        plan_at=lambda t: schedule_core.plan_at(sched, t),
        now=datetime.datetime.now,
        set_state=session.write_state,
        on_activate=on_activate,
        on_deactivate=on_deactivate,
        power_off=lambda from_session: power_off(from_session=from_session),
        keep_awake=keep_awake,
        every_10_min=every_10_min,
        read_switches=read_switches if keep_awake else None,
        on_switch_change=on_switch_change,
        shutdown_enabled=lambda: read_control("shutdown_enabled", "true").lower() == "true",
        log=log_info,
    )
    log_info(f"Session loop finished: {reason}")

def log_section(title):
    """Prints a clearly delineated section header for the log."""
    width = 60
    print()
    print("=" * width)
    print(f"  [ {title} ]")
    print("=" * width)

def log_ok(msg):
    print(f"  \u2705 {msg}")

def log_warn(msg):
    print(f"  \u26a0\ufe0f  {msg}")

def log_info(msg):
    print(f"  \u2022  {msg}")


# Boot header -- printed before anything else so the log
# boundary is always visible even if startup crashes early
_boot_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
print()
print("\u2554" + "\u2550" * 58 + "\u2557")
print("\u2551" + "       MOTHBOX SCHEDULER  \u2014  STARTING             " + "\u2551")
print(f"\u2551  Boot time : {_boot_time:<44} \u2551")
print("\u255a" + "\u2550" * 58 + "\u255d")

log_section("STARTUP -- Hardware")

rpiModel = None
rpiModel = determinePiModel()

if rpiModel == 4:
    log_warn("Pi 4 is not fully supported -- it cannot wake itself back up without a PiJuice.")
    log_info("Use an older image if you need Pi 4 support.")

if rpiModel == 5:
    # --------------------------------------------------------------------
    # EEPROM provisioning -- delegated to eeprom_safe.py
    #
    # ensure_eeprom_configured() is gated by a persistent flag so it only
    # ever does real work once. If a change is needed it stages the update
    # and performs a CLEAN REBOOT to apply it (never a halt/wake-alarm
    # power-off), then verifies the live config on the next boot before
    # declaring success. This is what prevents the "reflash the bootloader"
    # corruption that happened when the old code staged a flash and then
    # powered off in the same boot.
    # --------------------------------------------------------------------
    eeprom_status = ensure_eeprom_configured()

    if eeprom_status == "rebooting":
        # A reboot has already been issued inside ensure_eeprom_configured().
        # Stop this boot's script here; the next boot will pick up from a
        # clean slate once the EEPROM flash has been applied.
        log_info("EEPROM update staged -- rebooting cleanly to apply. Exiting this boot.")
        sys.exit(0)
    elif eeprom_status == "ok":
        log_ok("EEPROM settings verified correct.")
    elif eeprom_status == "failed":
        log_warn("EEPROM flash did NOT take after repeated attempts -- error pattern "
                 "was blinked on the onboard LED. Continuing boot in a degraded state "
                 "(device may not fully power off on halt until this is resolved).")
    elif eeprom_status == "defer":
        log_info("First-boot filesystem resize not finished yet -- EEPROM check deferred "
                 "to a future boot.")

# Which Mothbox is this? Detected every boot (an SD card can move between
# boxes) and cached in controls/hardware.txt for every other script to read.
# See mothbox_hw.py for how: the Pro PCB's I2C switch expanders are the tell.
if mothbox_hw is not None:
    try:
        _hw = mothbox_hw.detect_hardware(write_cache=True, verbose=False)
        log_ok(f"Mothbox hardware: {_hw['hardware'].upper()}  (by {_hw['detected_by']}; "
               f"i2c: {[hex(a) for a in _hw['i2c_found']] or 'none'}; "
               f"voltage sensor: {_hw['voltage_sensor']}; light sensor: {_hw['light_sensor']})")
        if _hw["hardware"] == "diy":
            log_info("DIY build: relay-HAT lights, OFF/DEBUG jumpers only, schedule from the CSV.")
    except Exception as e:
        log_warn(f"Hardware detection failed ({e}); scripts will assume Mothbox Pro.")

# Figuring out the controls and settings
controlsFpath="/boot/firmware/mothbox_custom/system"
# Control files live on the JOURNALED ext4 root now -- see mothbox_paths.py.
# The FAT boot partition also holds config.txt and the kernel, so a power cut
# mid-write there can leave a box that will not boot at all (Oct 2026).
try:
    from mothbox_paths import STATE_ROOT as _STATE_ROOT, resolve_read as _resolve_read, ensure_state_dir as _ensure_state_dir
    CONTROL_ROOT = str(_ensure_state_dir())
except Exception as _e:
    print(f"[Scheduler] mothbox_paths unavailable ({_e}); using the legacy boot-partition controls")
    CONTROL_ROOT = os.path.join(controlsFpath, "controls")
    def _resolve_read(p):
        return p
os.makedirs(CONTROL_ROOT, exist_ok=True)

# Safe defaults -- will be properly set after switch reading
use_switch_schedule = False
switch_schedule = {}

usersettingsFpath="/boot/firmware/mothbox_custom/mothbox_settings.csv"


# --- First boot: the filesystem is still expanding to fill the SD card ---
# Handle this FIRST, before the camera check, the status blinks, diagnostics
# or a session can light anything up. The shutdown functions also guard
# against powering off mid-resize, but they run at the END of startup, so on a
# fresh card the box used to flash its lights (and, inside its schedule window,
# start a whole session) while it expanded, and the "please wait" screen only
# appeared if the resize happened to still be running by then.
# The session state is still 'booting' here, so cron's Attract_On / TakePhoto stay off.
try:
    _expanding = filesystem_needs_expansion()
except Exception as e:
    log_warn(f"Could not check first-boot filesystem expansion ({e}); assuming done.")
    _expanding = False
if _expanding:
    log_section("FIRST BOOT -- Filesystem Expanding")
    log_warn("Root filesystem not yet expanded to fill the SD card -- one long flash, "
             "'please wait' on the display, then wait for the resize to finish.")
    _active = 1
    if mothbox_hw is not None:
        try:
            mothbox_hw.all_lights_off()
            _active = int(mothbox_hw.read_switches().get("Active", 1))
        except Exception as e:
            log_warn(f"Could not read switches before the expansion signal ({e}); assuming ACTIVE.")
    # One long flash so someone watching knows the box is alive and busy --
    # distinct from the 2 quick blinks (OK) and 8 slow blinks (camera warning).
    # OFF means OFF: nothing may light up, so skip it when the Active switch is off.
    if _active:
        run_cmd("python /home/pi/Desktop/Mothbox/scripts/blink_standby.py 1 3")
    else:
        log_info("Active switch is off -- skipping the expansion flash (nothing may light up in OFF).")
    try:
        GPIO.cleanup()
    except Exception:
        pass
    # UpdateDisplay.py notices the expansion itself and draws the wait screen.
    run_helper("/home/pi/Desktop/Mothbox/UpdateDisplay.py", "UpdateDisplay error")
    # The first-boot script may reboot the Pi during this wait; that is fine,
    # the next boot lands back here until the expansion is done.
    wait_for_firstboot_settle(log=log_warn)
    log_ok("Filesystem expansion finished -- continuing a normal startup.")
# --- End first-boot expansion handling ---


log_section("STARTUP -- Configuration")

settings = load_settings(usersettingsFpath)
if settings is None:
    log_warn("CRITICAL: Could not load settings -- using built-in defaults.")
    settings = dict(SETTING_DEFAULTS)
    settings["newwifidetected"] = False

# Unpack explicitly -- no hidden globals
autoname        = settings["autoname"]
manName         = settings["name"]
manTimezone     = settings["timezone"]
autoTime        = settings["autoSystemTime"]
manTime         = settings["manualTime"]
bat80           = settings["bat_80perVolts"]
bat20           = settings["bat_20perVolts"]
bat_Wh          = settings["bat_Wh"]
bat_voltage     = settings["bat_voltage"]
onlyflash       = settings["onlyflash"]
newwifidetected = settings["newwifidetected"]
runtime         = settings["runtime"]
log_info(f"Loaded settings: {settings}")


# --- The RTC coin cell: it keeps the clock (and so every wake-up) alive
# whenever the main battery is unplugged. Measure it with charging off, then
# charge it only if the settings say it is rechargeable. Runs in every mode
# (it lights nothing) and BEFORE the time is set below: `hwclock -w` rewrites
# the RTC and would hide a clock that lost power.
log_section("STARTUP -- Clock battery")
try:
    _rtc_epoch = clock_battery.read_rtc_epoch()
    _clock_lost = clock_battery.clock_lost(_rtc_epoch) or clock_battery.was_clock_lost(CONTROL_ROOT)
    _cell = clock_battery.check(settings.get("rtc_battery"))
    _cv = _cell["cell_v"]
    log_info(f"Coin cell: setting={_cell['kind'] or 'not in settings (left as config.txt set it)'}  "
             f"cell={'unreadable' if _cv is None else f'{_cv:.3f} V'}  "
             f"charging={_cell['charging_uv']} uV")
    if clock_battery.clock_lost(_rtc_epoch):
        log_warn(f"The RTC lost power (it reads {_rtc_epoch}) -- the clock is wrong until the "
                 f"time is set again. The coin cell is dead, missing or disconnected.")
    elif _clock_lost:
        log_warn("The clock lost power on an earlier boot and hasn't been set since.")
    for _a in _cell["alerts"]:
        log_warn(f"Clock battery: {_a}")
    if not _cell["alerts"] and not _clock_lost:
        log_ok("Clock battery OK.")
    clock_battery.write_status(CONTROL_ROOT, _cell, _clock_lost, time.time())
    clock_battery.append_history(os.path.join(CONTROL_ROOT, clock_battery.HISTORY_FILE),
                                 time.time(), _cell, _clock_lost)
except Exception as e:
    log_warn(f"Clock battery check failed ({e}) -- charging left as config.txt set it.")


if manTimezone.startswith("right/"):
    log_warn("Timezone uses 'right/' prefix which includes leap seconds and will cause scheduling errors. Use a standard timezone name instead.")

# Change the timezone in controls
#set_timezone(controlsFpath, manTimezone)
atomic_update_kv(os.path.join(CONTROL_ROOT, "timezone.txt"), "timezone", manTimezone)

# Todo - make control values use backup control values in emergency they got corrupted
#thecontrol_values = get_control_values(controlsFpath)


# Check the timezone

log_info(f"Timezone set to: {manTimezone} -- running TimezoneUpdater...")
run_helper("/home/pi/Desktop/Mothbox/TimezoneUpdater.py", "TimezoneUpdater error")

# See if we should manually set the time
# Todo: fix the time setting algorithm
# Set the time manually!
if(autoTime=="false"):
    log_info("Time mode: MANUAL -- setting system clock from CSV value.")
    subprocess.run(["timedatectl", "set-ntp", "false"], check=True)
    subprocess.run([
    "python3",
    "/home/pi/Desktop/Mothbox/SetTimeandDate.py",
    manTime
    ], check=True)
    update_csv_setting(usersettingsFpath, "autoSystemTime", "True")
    if clock_battery.clear_clock_lost(CONTROL_ROOT, time.time()):
        log_ok("Clock set by hand -- CLOCK LOST cleared.")
else:
    log_info("Time mode: AUTO (NTP)")
    subprocess.run(["timedatectl", "set-ntp", "true"], check=True)
    os.system("sudo hwclock -w")
    clear_clock_lost_if_synced()

#Reset python's cached version of the time
time.tzset()

now = datetime.datetime.now()
formatted_time = now.strftime("%Y-%m-%d %H:%M:%S")  # Adjust the format as needed

log_ok(f"Confirmed time: {formatted_time}   (RPi model {rpiModel})")


log_section("STARTUP -- Identity")
log_info(f"Auto-name enabled: {autoname}")
# Add option for people to manually set a name, but default to autoname made by pi5 serial number 
if(autoname=="true"):
    filename = "/home/pi/Desktop/Mothbox/wordlist.csv"  # Replace with your actual filename
    data = read_csv_into_lists(filename)

    # Access data by category (column name)
    animals = data["Animal2"]
    adjectives = data["Adjectives"]
    colors = data["Colors"]
    verbs = data["Verbs"]
    animales = data["Animales"]
    # print(animales)
    adjectivos = data["Adjectivos"]
    # print(adjectivos)
    verbos = data["Verbos"]
    # print(verbos)
    colores = data["Colores"]
    # print(colores)
    sustantivos = data["Sustantivos"]
    # print(sustantivos)

    # SetRaspberrypiName
    serial_number = get_serial_number()
    # 0 is english 1 is spanish 2 is either spanish or enlgish 3 is spanglish
    unique_name = generate_unique_name(serial_number, 3)
    log_ok(f"Device name (auto): {unique_name}")

    # Change it in controls
    #set_computerName("/boot/firmware/mothbox_custom/system/controls.txt", unique_name)
    atomic_update_kv(os.path.join(CONTROL_ROOT, "name.txt"), "name", unique_name)
else:
  computerName=manName
  atomic_update_kv(os.path.join(CONTROL_ROOT, "name.txt"), "name", manName)  # <- add this
  log_ok(f"Device name (manual): {computerName}")
  
# ---- End figure out name -----




# -----Set MODE: CHECK THE PHYSICAL SWITCH on the GPIO PINS--------------------
# -----CHECK THE PHYSICAL SWITCH on the GPIO PINS--------------------

'''
There are several possible modes that a Mothbox can be in
Off: sw-Active=0 sw-Debug=0- Mothbox will turn off as soon as it can anytime it wakes (useful for charging)

Active: sw-Active=1 -  it is currently running a session. Automatic routines go. Wifi stops after 5 mins to save energy.
Standby: sw-Active=1 (but not time for session) - the mothbox pi is shut down, but during the next scheduled session it will become active

Debug: sw-Debug=1 + sw-Active=1 ------------------ When the mothbox has power, it will wake up and not shut down until manually turned off. Automatic Cron routines will not run. Lights are default off. Wifi stays on.
Party: sw-Debug=1 + sw-Active=1 + sw-C1=1 ----- subset of debug mode, but it runs a routine to just cycle all the lights
*TODO ActiveQRProgram:sw-Debug=1+ sw-Active=1+sw-U1=1   -  the schedule gets set by using the camera to read a QR code - will need to turn debug off after


*TODO HI Power: sw-ACtive=1 + sw-HI=1  - like ACTIVE but Assumption is connected not to battery, but unlimited power supply. Wifi stays on, attempts to upload photos to internet servers automatically.


'''
mode = "ACTIVE"

# Update hardware switch snapshot
run_script("/home/pi/Desktop/Mothbox/GetConfigSwitches.py", show_output=True)

# Read switches snapshot
switch_path = os.path.join(CONTROL_ROOT, "switches.txt")
switch_vals = get_control_values(switch_path)

sActive = int(switch_vals.get("Active", 1))
sDebug  = int(switch_vals.get("Debug", 0))
sC1     = int(switch_vals.get("C1", 0))
sU1     = int(switch_vals.get("U1", 0))
sHI     = int(switch_vals.get("HI", 0))



log_info(f"Switches -- Active:{sActive}  Debug:{sDebug}  C1:{sC1}  U1:{sU1}  HI:{sHI}")

# Initialize scheduling variables early so shutdown functions can safely reference them
# even if called before the scheduling block is reached
use_switch_schedule = (sU1 == 1)
switch_schedule = {}
source_type = "physical switches" if use_switch_schedule else "CSV settings"
log_info(f"Schedule source: {source_type}")



# ----------END SWITCH CHECK----------------


# ~~~~~~~~~~~~ Scheduling ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# schedule_core.py turns the settings into real windows of activity. Every
# decision from here on -- and the session loop at the end -- asks it.

log_section("CONFIGURATION -- Scheduling")

if use_switch_schedule:
    log_info("Schedule source: physical switches")
    switch_schedule = read_switch_schedule(switch_vals)
    raw_schedule = switch_schedule
else:
    log_info("Schedule source: CSV settings")
    raw_schedule = {k: settings.get(k) for k in ("weekday", "hour", "minute", "runtime")}

sched = schedule_core.parse(raw_schedule)
if sched.errors:
    # A typo here used to crash the Scheduler at boot, leaving the box awake,
    # dark and unable to shut down until the battery died. Now each bad
    # setting falls back to its default and the display says which to fix.
    log_warn(f"SCHEDULE ERROR in {', '.join(sched.errors)} ({raw_schedule}) -- using the default "
             f"for {'that setting' if len(sched.errors) == 1 else 'those settings'}.")
atomic_update_kv(os.path.join(CONTROL_ROOT, "schedule_errors.txt"), "errors", ",".join(sched.errors))
_in_use = schedule_core.as_settings(sched)
set_timings(_in_use["minute"], _in_use["hour"], _in_use["weekday"], _in_use["runtime"])
log_info(f"Schedule in use: {_in_use}")
log_info(f"Windows in the next 24 h: {schedule_core.describe(sched, datetime.datetime.now())}")

photo_interval = int(settings.get("photo_interval", 1))
atomic_update_kv(os.path.join(CONTROL_ROOT, "photo_interval.txt"), "photo_interval", photo_interval)
log_info(f"Photo interval: {photo_interval} min")
log_info(f"Backup interval: {settings.get('backup_interval', 5)} min (read live by Backup_Files.py)")
log_info(f"Hardware override in settings: {settings.get('hardware', 'auto')}")

if rpiModel == 4:
    log_warn("Pi 4 not supported -- device cannot wake itself.")

# A wake alarm for the next window, set now as a safety net: if this boot
# ends without a proper shutdown (power cut, crash), the box still wakes for
# it. power_off() replaces it with the exact one, last thing before halting.
_next_window = schedule_core.next_window_start(sched, datetime.datetime.now())
if _next_window is None:
    log_info("The schedule never leaves a gap (always on) -- no wake alarm needed.")
else:
    clear_wakeup_alarm()
    set_wakeup_alarm(next_wake_epoch())
    log_ok(f"Safety wake alarm set for the next window ({_next_window:%a %H:%M}).")

#-- End Scheduling complete, now set all the other settings




log_section("CONFIGURATION -- Mode")

if sActive == 0:
    mode = "OFF"
    log_info("Active switch is OFF -> mode: OFF")

if mode == "OFF":
    atomic_update_kv(os.path.join(CONTROL_ROOT, "mode.txt"), "mode", mode)
    power_off(off_mode=True)
    quit()

# If Active Switch is OFF, it should never make it past here


#### Check ACtive Modes
# Now check for subsets of Active Mode, like Party Mode or Debug
# TODO

if(sDebug==1):
    None
    mode="DEBUG"

if(sDebug==1 and sC1==1):
    None
    mode="PARTY"

#Temporarily disabling QR prog mode
# ~ if(sDebug==1 and sU1==1):
    # ~ None
    # ~ mode="QR_PROG"


if(sDebug==0 and sHI==1):
    None
    mode="HI_POW"

log_ok(f"Mode determined: {mode}")
# Write mode to controls.txt
#set_Mode(controlsFpath, mode)
atomic_update_kv(os.path.join(CONTROL_ROOT, "mode.txt"), "mode", mode)



# TODO - Implement these modes I haven't coded for yet
# for now, temp solution

if mode=="QR_PROG":
    mode="ACTIVE"
    atomic_update_kv(os.path.join(CONTROL_ROOT, "mode.txt"), "mode", mode)
    #set_Mode(controlsFpath, mode)
    log_info(f"Mode normalised to: {mode}")

# ----- Mode kills desktop mode for everything but DEBUG and PARTY   
configure_display_for_mode(mode)

configure_wifi_for_mode(mode)

log_section("CONFIGURATION -- WiFi Provisioning")
handle_wifi_provisioning(settings.get("ssid", None), settings.get("wifipass", None), mode)


####---- END MODE LOGIC----#



#------ Camera Check -----------
# Run for every mode except OFF (already quit() before reaching here).
# The result is stored in camera_ok so the standby blink block below can
# use it to choose between a normal 2-blink and a warning 8-blink.

log_section("CONFIGURATION -- Camera")
camera_ok = check_camera()
# Record it where UpdateDisplay.py can see it, so a disconnected camera shows
# as a banner on the e-paper instead of only as a blink pattern nobody is
# there to count. TakePhoto.py rewrites this too if the camera dies mid-session.
atomic_update_kv(os.path.join(CONTROL_ROOT, "camera.txt"), "camera", "ok" if camera_ok else "missing")
if not camera_ok:
    # Warn the human NOW, on every path, then carry on exactly as normal.
    # The check is advisory only: nothing downstream is gated on it, so if this
    # was a false positive the box still takes its photos. Better a needless
    # warning than a silent night of blank frames.
    log_warn("Camera not detected -- display banner set. "
             "Photos will still be attempted normally in case this is a false alarm.")
    # OFF means OFF: an OFF box must never light anything. It already exits
    # long before this point, but say so explicitly so that stays true if the
    # camera check is ever moved earlier in the boot sequence.
    if mode == "OFF":
        log_info("Mode is OFF -- skipping the 8-blink warning (nothing may light up in OFF).")
    else:
        log_warn("Blinking the 8x camera warning.")
        run_cmd("python /home/pi/Desktop/Mothbox/scripts/blink_standby.py 8 0.5")
#------ End Camera Check -------








log_section("LAUNCH -- Schedule Check")

boot_plan = schedule_core.plan_at(sched, datetime.datetime.now())

if mode == "ACTIVE" and boot_plan.state == schedule_core.SLEEP:
    log_info(f"Outside the schedule -- the next window opens {boot_plan.next_start:%a %H:%M}. "
             f"Entering STANDBY.")
    mode = "STANDBY"
    atomic_update_kv(os.path.join(CONTROL_ROOT, "mode.txt"), "mode", mode)
    # Blink FIRST. Someone is usually standing there waiting to know the
    # box is healthy, and the diagnostics below take several seconds they
    # would otherwise spend staring at a dark box. The log still gets
    # everything -- it just gets it after the human has their answer.
    # Only the "all good" blink here -- a missing camera already blinked its
    # 8x warning right after the check, on whichever path we took.
    if camera_ok:
        run_cmd("python /home/pi/Desktop/Mothbox/scripts/blink_standby.py 2 0.125")

    log_section("STANDBY -- Sensors & Diagnostics")
    run_script("/home/pi/Desktop/Mothbox/Diagnostics.py", "Standby_Check", show_output=True)
    if power_off() == "powered_off":
        quit()
    # Getting ready to sleep took long enough that the next window is close
    # now, so power_off() stayed up: carry on and run it.
    mode = "ACTIVE"
    boot_plan = schedule_core.plan_at(sched, datetime.datetime.now())

if mode in ("ACTIVE", "HI_POW"):
    if boot_plan.state == schedule_core.ACTIVE:
        _until = (f"{boot_plan.session_end:%a %H:%M}" if boot_plan.session_end
                  else "further notice (the schedule never leaves a gap)")
        log_ok(f"Inside a window -- lights and photos until {_until}.")
        write_session_info("active", boot_plan)
    else:
        # Switched on (or woken) just before a window -- or HI POWER between
        # windows: stay up, dark, and start exactly when the window opens.
        log_ok(f"The next window opens {boot_plan.next_start:%a %H:%M} -- waiting for it, dark"
               + (" (HI POWER never sleeps)." if mode == "HI_POW" else "."))
        write_session_info("waiting", boot_plan)
        if camera_ok:
            run_cmd("python /home/pi/Desktop/Mothbox/scripts/blink_standby.py 2 0.125")
else:
    write_session_info(mode.lower())         # DEBUG / PARTY: the display shows the next wake

log_section("CONFIGURATION -- Sensors & Diagnostics")
run_script("/home/pi/Desktop/Mothbox/Diagnostics.py", "Startup_Check", show_output=True)


log_section("LAUNCH -- GPS")
_gps_devs = gps_devices_attached()
if _gps_devs == 0:
    log_info("gpsd has no device attached -- skipping the GPS read (saves ~10s).")
elif _gps_devs < 0:
    log_info("gpsd not reachable -- skipping the GPS read.")
else:
    log_info(f"gpsd reports {_gps_devs} device(s) -- reading GPS, up to 10 seconds...")
    run_helper("/home/pi/Desktop/Mothbox/GPS.py", "GPS error")

# Toggle a mode where the flash lights are always on
#enable_onlyflash()



# ~~~~~~~ Display ~~~~~~~~~~~~~~~~~~~~

log_section("LAUNCH -- Display")
log_info("Updating ePaper display (if available)...")
GPIO.cleanup()
run_helper("/home/pi/Desktop/Mothbox/UpdateDisplay.py", "Display error")


log_section("LAUNCH -- Final Mode")

# NOTE: no "OFF" branch here. OFF is handled much earlier, in CONFIGURATION --
# Mode, which shuts down and quit()s before this point, so a branch here could
# never run.
if mode in ("DEBUG", "PARTY"):
    # Before DebugMode.py / Party.py: Party.py never returns, and running
    # TakePhoto.py or Attract_On.py by hand to test should work straight away.
    session.write_state(session.DEBUG)

if mode == "DEBUG":
    log_ok("Mode is DEBUG -- wifi on, cron off, staying alive.")
    # Define the path to your script (replace 'path/to/script' with the actual path)
    debug_script_path = "/home/pi/Desktop/Mothbox/DebugMode.py"
    run_mode_script(debug_script_path)
    # stopcron()
elif mode == "PARTY":
    log_ok("Mode is PARTY -- lights cycling, wifi on.")
    # Define the path to your script (replace 'path/to/script' with the actual path)
    debug_script_path = "/home/pi/Desktop/Mothbox/DebugMode.py"
    run_mode_script(debug_script_path)

    party_script_path = "/home/pi/Desktop/Mothbox/Party.py"
    run_mode_script(party_script_path)
    # stopcron()
elif mode == "ACTIVE":
    log_ok("Mode is ACTIVE -- session running.")
elif mode == "HI_POW":
    log_ok("Mode is HI POWER -- wifi on, never sleeps, lights and photos follow the schedule.")
else:
    log_warn(f"Unrecognised mode: {mode}")



###------ Hand over to the session ----------#
# From here on the session state decides what cron may do (see session.py).

log_section("LAUNCH -- Session")
if mode in ("DEBUG", "PARTY"):
    log_info("No session in DEBUG / PARTY -- cron is off and the box runs until switched off.")
else:
    enable_shutdown()
    if mode == "HI_POW":
        log_info("HI POWER: never powers off -- following the schedule and watching the HI switch.")
    run_session(keep_awake=(mode == "HI_POW"))
