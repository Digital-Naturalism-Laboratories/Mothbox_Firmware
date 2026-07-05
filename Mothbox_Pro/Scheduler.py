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
boot (via run_shutdown_pi5 / run_shutdown_pi5_FAST + RTC wake-alarm), which
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
  3. Guards both shutdown paths so the device will reboot-to-apply instead of
     powering off whenever an EEPROM flash is still staged and pending.
--------------------------------------------------------------------------
"""

###------Boot Lock-------------###
#create boot lock. This stops other scripts that might get called by cron from running

BOOT_LOCK = "/run/boot_script_running"

# create lock
with open(BOOT_LOCK, "w") as f:
    f.write("booting\n")

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
import numpy as np
import sys
import schedule

import crontab
from crontab import CronTab
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


def check_camera():
    """
    Checks whether a camera is connected and responding using rpicam-hello
    (the current tool on Raspberry Pi OS Bookworm and later).
    Falls back to the older libcamera-hello if rpicam-hello is not found,
    for compatibility with older images.
    Returns True if a camera is detected, False otherwise.
    Uses a short timeout so it never stalls the boot sequence.
    """
    # Try rpicam-hello first (current), fall back to libcamera-hello (legacy)
    for tool in ("rpicam-hello", "libcamera-hello"):
        try:
            result = subprocess.run(
                [tool, "--list-cameras"],
                capture_output=True,
                text=True,
                timeout=10
            )
            output = result.stdout + result.stderr
            print(f"[-]    Camera check using {tool}:")
            print(f"       {output.strip()[:200]}")
            if result.returncode == 0 and "No cameras available" not in output:
                print(f"[OK]   Camera detected via {tool}.")
                return True
            else:
                print(f"[WARN] {tool}: no camera found.")
                print(f"       Output: {output.strip()[:200]}")
                return False
        except FileNotFoundError:
            print(f"[-]    {tool} not found, trying fallback...")
            continue
        except subprocess.TimeoutExpired:
            print(f"[WARN] Camera check timed out after 10 seconds ({tool}) -- assuming no camera.")
            return False
        except Exception as e:
            print(f"[WARN] Camera check failed unexpectedly ({tool}): {e}")
            return False

    print("[WARN] Neither rpicam-hello nor libcamera-hello found -- cannot verify camera.")
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
}

def load_settings_for_wakeup():
    """
    Safely loads settings needed to calculate the next wakeup alarm.
    Falls back to SETTING_DEFAULTS if the CSV is missing or corrupt.
    Never returns None.
    """
    settings = load_settings(usersettingsFpath)

    if settings is None:
        print("[!] WARNING: Could not load settings for wakeup -- using defaults")
        settings = dict(SETTING_DEFAULTS)

    settings.pop("runtime", None)  # runtime not needed for wakeup scheduling
    settings.pop("utc_off", None)  # comes from controls, not CSV

    # Normalize semicolons to commas
    for key, value in settings.items():
        if isinstance(value, str) and ";" in value:
            settings[key] = value.replace(";", ",")

    return settings

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


def schedule_shutdown(minutes):
    """Schedules the execution of shutdown after the specified delay in minutes."""
    if rpiModel == 4:
        "pi4 no longer suppored"
        schedule.every(minutes).minutes.do(run_shutdown_pi5)
    if rpiModel == 5:
        schedule.every(minutes).minutes.do(run_shutdown_pi5)

    try:
        while True:
            #control_values = get_control_values("/boot/firmware/mothbox_custom/system/controls.txt")
            shutdown_enabled = read_control("shutdown_enabled", "true").lower() == "true"
            if not shutdown_enabled:
                print("Shutdown scheduling stopped.")
                break

            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutdown scheduling stopped.")


def should_abort_shutdown(cron_source, runtime_minutes, grace_minutes=1):
    """
    Checks whether the device is still inside a valid scheduled session window.

    This guards against the edge case where a slow SD card or a runtime that
    exceeds the gap between scheduled wakeups causes shutdown to fire slightly
    after the next slot's start time.  Without this check, calculate_next_event
    would skip that slot and the device would wake an hour (or more) late.

    Args:
        cron_source:     dict with 'minute', 'hour', 'weekday' keys (same format
                         as build_cron_expression expects).
        runtime_minutes: configured session length in minutes.
        grace_minutes:   minimum window past a slot's start time that is still
                         considered "in session" (default: 1 minute).  Catches
                         slow-SD-card delays even when runtime < slot gap.

    Returns:
        Remaining minutes (float) if shutdown should be postponed -- the caller
        should reschedule shutdown for that many minutes from now.
        None if it is safe to proceed with shutdown immediately.
    """
    now = datetime.datetime.now()

    minutes      = parse_int_list(cron_source.get("minute",  "0"))
    hours        = parse_int_list(cron_source.get("hour",    "20"))
    weekdays_raw = parse_int_list(cron_source.get("weekday", "1,2,3,4,5,6,7"))

    # Convert CSV weekday (1-7, 1=Monday) -> Python weekday (0-6, 0=Monday)
    weekdays = [(d - 1) % 7 for d in weekdays_raw]

    now_weekday = now.weekday()

    # Check today and yesterday to catch sessions that started before midnight
    for day_offset in (0, -1):
        day     = now.date() + datetime.timedelta(days=day_offset)
        weekday = (now_weekday + day_offset) % 7

        if weekday not in weekdays:
            continue

        for h in hours:
            for m in minutes:
                start = datetime.datetime.combine(day, datetime.time(hour=h, minute=m))
                # Use whichever is larger: the configured runtime, or the grace
                # floor.  This means a 1-minute slip on a 59-minute session is
                # caught, AND a runtime longer than the slot gap is also caught.
                effective_end = start + datetime.timedelta(
                    minutes=max(runtime_minutes, grace_minutes)
                )

                if start <= now < effective_end:
                    remaining = (effective_end - now).total_seconds() / 60.0
                    print(
                        f"[!]  Shutdown requested at {now.strftime('%H:%M:%S')} but "
                        f"session window {start.strftime('%H:%M')}-"
                        f"{effective_end.strftime('%H:%M')} is still active. "
                        f"Rescheduling shutdown in {remaining:.1f} min."
                    )
                    return remaining

    return None


def run_shutdown_pi5():
    """
    Shut down the raspberry pi
    """
    # --- EEPROM safety guard: never power off with a staged flash pending ---
    # If eeprom_safe staged a bootloader update earlier this boot (or a prior
    # boot) and it hasn't been applied/verified yet, a halt here could
    # interrupt the SPI flash mid-write and corrupt the bootloader. Reboot
    # cleanly instead so recovery.bin can apply it on stable power.
    if should_reboot_before_poweroff():
        print("[!] EEPROM flash still pending -- rebooting to apply instead of powering off.")
        reboot_to_apply()
        return
    # --- End EEPROM guard ---

    # Re-lock the other scripts (don't want it to start taking a photo before shutting down)
    ###------Boot Lock-------------###
    #create boot lock. This stops other scripts that might get called by cron from running

    BOOT_LOCK = "/run/boot_script_running"

    # create lock
    with open(BOOT_LOCK, "w") as f:
        f.write("booting\n")

    #-------------------#

    # --- Guard: don't shut down if we're still inside a scheduled session window ---
    cron_source = switch_schedule if use_switch_schedule else load_settings_for_wakeup()
    current_runtime = int(read_control("runtime", runtime))
    remaining_minutes = should_abort_shutdown(cron_source, current_runtime)
    if remaining_minutes is not None:
        print(f"Shutdown aborted -- rescheduling in {remaining_minutes:.1f} min.")
        # Clear the old fired job and schedule a new one for the remaining window.
        # This ensures the device shuts down at the true end of the session rather
        # than running forever.
        schedule.clear()
        schedule.every(remaining_minutes).minutes.do(run_shutdown_pi5)
        return
    # --- End guard ---

    log_section("SHUTDOWN -- Graceful")

    # SCHEDULE WAKEUP AGAIN FOR SECURITY
    settings = load_settings_for_wakeup()
    cron_source = switch_schedule if use_switch_schedule else settings
    cron_expression = build_cron_expression(cron_source)
    log_info(f"Cron expression: {cron_expression}")

    next_epoch_time = calculate_next_event(cron_expression, utc_off)
    clear_wakeup_alarm()
    set_wakeup_alarm(next_epoch_time)
    log_ok(f"Next wakeup scheduled for: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_epoch_time))}")

    ''' # Cutting out GPS check at shutdown, feels not really needed
    # GPS check / 10 second delay
    print("Checking GPS (if available) for 10 seconds")
    process = subprocess.Popen(['python', '/home/pi/Desktop/Mothbox/GPS.py'],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()
    if stderr:
      print(f"Error running script: {stderr.decode()}")
    else:
      print(stdout.decode())
    '''
    # Change the mode to "STANDBY" (if we got to this point, the board must have been "ACTIVE" and so now we are switching to "STANDBY". This is because OFF mode will just use the fast shutdown script, and DEBUG modes will never shut down

    # Write mode to controls.txt
    #set_Mode("/boot/firmware/mothbox_custom/system/controls.txt", "STANDBY")
    
    mode="STANDBY"
    atomic_update_kv(os.path.join(CONTROL_ROOT, "mode.txt"), "mode", mode)
    
    #Epaper
    #Update the Epaper screen if it is available 
    GPIO.cleanup()

    log_info("Updating ePaper display before shutdown (if available)...")
    process = subprocess.Popen(['python', '/home/pi/Desktop/Mothbox/UpdateDisplay.py'],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()
    if stderr:
      log_warn(f"UpdateDisplay error: {stderr.decode().strip()}")
    else:
      print(stdout.decode())

    log_info("Shutting down in ~4 seconds...")
    time.sleep(1)
    run_script("/home/pi/Desktop/Mothbox/Diagnostics.py", "Shutdown_Check", show_output=True)

    time.sleep(3)

    os.system("sudo shutdown -h now")



def run_shutdown_pi5_FAST():
    """
    Shut down the raspberry pi
    """
    log_section("SHUTDOWN -- Fast")

    # --- EEPROM safety guard: never power off with a staged flash pending ---
    if should_reboot_before_poweroff():
        print("[!] EEPROM flash still pending -- rebooting to apply instead of powering off.")
        reboot_to_apply()
        return
    # --- End EEPROM guard ---

    # Re-lock the other scripts (don't want it to start taking a photo before shutting down)
    ###------Boot Lock-------------###
    #create boot lock. This stops other scripts that might get called by cron from running

    BOOT_LOCK = "/run/boot_script_running"

    # create lock
    with open(BOOT_LOCK, "w") as f:
        f.write("booting\n")

    #-------------------#

    # --- Guard: don't shut down if we're still inside a scheduled session window ---
    # Note: OFF mode bypasses this check intentionally -- if the user flipped the
    # Active switch off, they want the device to shut down regardless of schedule.
    current_mode = read_control("mode", "ACTIVE")
    if current_mode != "OFF":
        cron_source = switch_schedule if use_switch_schedule else load_settings_for_wakeup()
        current_runtime = int(read_control("runtime", runtime))
        remaining_minutes = should_abort_shutdown(cron_source, current_runtime)
        if remaining_minutes is not None:
            print(f"Shutdown aborted -- rescheduling in {remaining_minutes:.1f} min.")
            schedule.clear()
            schedule.every(remaining_minutes).minutes.do(run_shutdown_pi5_FAST)
            return
    # --- End guard ---
    
    #Stop big lights from turning on!
    offlight_script_path = "/home/pi/Desktop/Mothbox/Attract_Off.py"
    # Call the script using subprocess.run
    subprocess.run([offlight_script_path])
    
    # SCHEDULE WAKEUP AGAIN FOR SECURITY
    settings = load_settings_for_wakeup()
    cron_source = switch_schedule if use_switch_schedule else settings
    cron_expression = build_cron_expression(cron_source)
    log_info(f"Cron expression: {cron_expression}")

    next_epoch_time = calculate_next_event(cron_expression, utc_off)
    clear_wakeup_alarm()
    set_wakeup_alarm(next_epoch_time)
    log_ok(f"Next wakeup scheduled for: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_epoch_time))}")

    GPIO.cleanup()
    log_info("Updating ePaper display before shutdown (if available)...")
    process = subprocess.Popen(['python', '/home/pi/Desktop/Mothbox/UpdateDisplay.py'],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()
    if stderr:
      log_warn(f"UpdateDisplay error: {stderr.decode().strip()}")
    else:
      print(stdout.decode())

    # subprocess.run(["python", "/home/pi/Desktop/Mothbox/TurnEverythingOff.py"])
    os.system("sudo shutdown -h now")



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

def calculate_next_event(cron_expression, utcOff):
    cron = CronTab(user="root")
    job = cron.new(command="echo hello_world")
    job.setall(cron_expression)

    # Work entirely in UTC
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    # Convert UTC -> LOCAL using known offset
    now_local = now_utc + datetime.timedelta(hours=float(utcOff))

    schedule = job.schedule(date_from=now_local)
    next_local = schedule.get_next()

    # Convert back LOCAL -> UTC
    next_utc = next_local - datetime.timedelta(hours=float(utcOff))

    return int(next_utc.timestamp())

def clear_wakeup_alarm():
    """
    Clears the existing wakeup alarm for the Raspberry Pi using /sys/class/rtc/rtc0/wakealarm.
    """
    # Open the wakealarm file for writing with sudo
    with open("/sys/class/rtc/rtc0/wakealarm", "w") as f:
        f.write("0")  # Write 0 to clear the alarm


def set_wakeup_alarm(epoch_time):
    """
    Sets the wakeup alarm for the Raspberry Pi using /sys/class/rtc/rtc0/wakealarm.

    Args:
        epoch_time: A unix timestamp representing the next wakeup time.
    """
    # Open the wakealarm file for writing
    with open("/sys/class/rtc/rtc0/wakealarm", "w") as f:
        # Write the epoch time in seconds
        f.write(str(epoch_time))
    logging.info("Set the Wakeup Alarm" + str(epoch_time))
    #Write to controls here!
    #set_nextWakeinControls("/boot/firmware/mothbox_custom/system/controls.txt",epoch_time)
    atomic_update_kv(os.path.join(CONTROL_ROOT, "nextwake.txt"), "nextwake", epoch_time)


def run_script(script_path, *args, show_output=True):
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
            check=True
        )

        if show_output:
            output = result.stdout.strip()
            if output:
                print(output)

    except subprocess.CalledProcessError as e:
        print(f"[!] Error running {script_path}: {e.stderr.strip() if e.stderr else 'Unknown error'}")


# Check if now is in schedule 

def parse_int_list(value):
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        return [int(v.strip()) for v in value.split(",") if v.strip()]
    return []

def is_now_in_schedule(settings, runtime_minutes):
    now = datetime.datetime.now()

    minutes = parse_int_list(settings.get("minute", ""))
    hours = parse_int_list(settings.get("hour", ""))
    weekdays_raw = parse_int_list(settings.get("weekday", ""))

    # Convert CSV weekday (1-7) -> Python weekday (0-6)
    weekdays = [(d - 1) % 7 for d in weekdays_raw]

    now_weekday = now.weekday()

    # Try all scheduled start times for today *and* yesterday
    # (needed for cross-midnight runtimes)
    for day_offset in (0, -1):
        day = now.date() + datetime.timedelta(days=day_offset)
        weekday = (now_weekday + day_offset) % 7

        if weekday not in weekdays:
            continue

        for h in hours:
            for m in minutes:
                start = datetime.datetime.combine(
                    day,
                    datetime.time(hour=h, minute=m)
                )
                end = start + datetime.timedelta(minutes=runtime_minutes)

                #print(start)
                #print(now)
                #print(end)
                if start <= now < end:
                    return True

    return False


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
    path = os.path.join(CONTROL_ROOT, f"{key}.txt")
    if not os.path.exists(path):
        return default
    vals = get_control_values(path)
    return vals.get(key, default)


def build_cron_expression(settings):
    minute  = settings.get("minute",  "0")
    hour    = settings.get("hour",    "20")
    weekday = settings.get("weekday", "1,2,3,4,5,6,7")
    return f"{minute} {hour} * * {weekday}"


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
        "runtime": 59,
    }

#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~####
#                       Main Code
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~###

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

# Figuring out the controls and settings
controlsFpath="/boot/firmware/mothbox_custom/system"
CONTROL_ROOT = os.path.join(controlsFpath, "controls")
os.makedirs(CONTROL_ROOT, exist_ok=True)

# Safe defaults -- will be properly set after switch reading
use_switch_schedule = False
switch_schedule = {}

usersettingsFpath="/boot/firmware/mothbox_custom/mothbox_settings.csv"
default_settingspath = "/boot/firmware/mothbox_custom/system/default_settings.txt"
default_backup_controlspaths="/boot/firmware/mothbox_custom/system/default_backup_controls.txt"



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


if manTimezone.startswith("right/"):
    log_warn("Timezone uses 'right/' prefix which includes leap seconds and will cause scheduling errors. Use a standard timezone name instead.")

# Change the timezone in controls
#set_timezone(controlsFpath, manTimezone)
atomic_update_kv(os.path.join(CONTROL_ROOT, "timezone.txt"), "timezone", manTimezone)

# Todo - make control values use backup control values in emergency they got corrupted
#thecontrol_values = get_control_values(controlsFpath)


# Check the timezone

log_info(f"Timezone set to: {manTimezone} -- running TimezoneUpdater...")
process = subprocess.Popen(['python', '/home/pi/Desktop/Mothbox/TimezoneUpdater.py'],
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
stdout, stderr = process.communicate()
if stderr:
  log_warn(f"TimezoneUpdater error: {stderr.decode().strip()}")
else:
  print(stdout.decode())

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
else:
    log_info("Time mode: AUTO (NTP)")
    subprocess.run(["timedatectl", "set-ntp", "true"], check=True)
    os.system("sudo hwclock -w")

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


# Read UTC offset from new control layout
utc_off = float(read_control("utc", 0))



# ~~~~~~~~~~~~ Figuring out Scheduling Details ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# ~~~~ Pi 5 specific things to change cron-like commands to the next UTC target


log_section("CONFIGURATION -- Scheduling")

switch_schedule = {}
if use_switch_schedule:
    log_info("Schedule source: physical switches")
    switch_schedule = read_switch_schedule(switch_vals)
    minute  = switch_schedule["minute"]
    hour    = switch_schedule["hour"]
    weekday = switch_schedule["weekday"]
    runtime = switch_schedule["runtime"]
else:
    log_info("Schedule source: CSV settings")
    minute  = settings.get("minute",  "0")
    hour    = settings.get("hour",    "20")
    weekday = settings.get("weekday", "1,2,3,4,5,6,7")
    runtime = int(settings.get("runtime", 58))

photo_interval = int(settings.get("photo_interval", 1))
atomic_update_kv(os.path.join(CONTROL_ROOT, "photo_interval.txt"), "photo_interval", photo_interval)
settings.pop("photo_interval", None)  # don't let it pollute cron builder

set_timings(minute, hour, weekday, runtime)
settings.pop("runtime", None)  # safe delete, no KeyError


if rpiModel == 4:
    log_warn("Pi 4 not supported -- device cannot wake itself.")

if rpiModel == 5:
    # don't need to modify the hours to UTC like we do for pijuice
    # Build Cron expression
    # The cron expression is made of five fields. Each field can have the following values.
    # minute (0-59) |	hour (0 - 23)	|day of the month (1 - 31)	| month (1 - 12)	| day of the week (0 - 6)

    # Loop through each key-value pair in the dictionary
    for key, value in settings.items():
        # Check if the value is a string and contains semicolons
        if isinstance(value, str) and ";" in value:
            # Replace semicolons with commas
            settings[key] = value.replace(";", ",")
    cron_source = switch_schedule if use_switch_schedule else settings
    cron_expression = build_cron_expression(cron_source)
    
    log_info(f"Cron expression : {cron_expression}")
    log_info(f"UTC offset      : {utc_off}")

    next_epoch_time = calculate_next_event(cron_expression, utc_off)

    # Clear existing wakeup alarm (assuming sudo access)
    clear_wakeup_alarm()

set_wakeup_alarm(next_epoch_time)
log_ok(f"Next wakeup alarm set for: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_epoch_time))}")

#-- End Scheduling complete, now set all the other settings




log_section("CONFIGURATION -- Mode")

if sActive == 0:
    mode = "OFF"
    log_info("Active switch is OFF -> mode: OFF")

if mode == "OFF":
    atomic_update_kv(os.path.join(CONTROL_ROOT, "mode.txt"), "mode", mode)
    run_shutdown_pi5_FAST()
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

if mode=="HI_POW" or mode=="QR_PROG":
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
if not camera_ok:
    log_warn("Camera not found -- standby blink will use warning pattern (8 blinks).")
#------ End Camera Check -------



log_section("CONFIGURATION -- Sensors & Diagnostics")

run_script("/home/pi/Desktop/Mothbox/Diagnostics.py", "Startup_Check", show_output=True)





log_section("LAUNCH -- Schedule Check")

if mode == "ACTIVE":
    schedule_to_check = switch_schedule if use_switch_schedule else settings
    if is_now_in_schedule(schedule_to_check, runtime):
        now_is_in_schedule = 1
        log_ok("Within schedule window -- staying awake.")
    else:
        now_is_in_schedule = 0
        log_info("Outside schedule window -> entering STANDBY.")
        mode = "STANDBY"
        atomic_update_kv(os.path.join(CONTROL_ROOT, "mode.txt"), "mode", mode)
        blink_count = 2 if camera_ok else 8
        run_cmd(f"python /home/pi/Desktop/Mothbox/scripts/blink_standby.py {blink_count}")
        run_shutdown_pi5_FAST()
        quit()


log_section("LAUNCH -- GPS")
log_info("Checking GPS (if available) -- up to 10 seconds...")
process = subprocess.Popen(['python', '/home/pi/Desktop/Mothbox/GPS.py'],
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
stdout, stderr = process.communicate()
if stderr:
  log_warn(f"GPS error: {stderr.decode().strip()}")
else:
  print(stdout.decode())

# Toggle a mode where the flash lights are always on
#enable_onlyflash()



# ~~~~~~~ Display ~~~~~~~~~~~~~~~~~~~~

log_section("LAUNCH -- Display")
log_info("Updating ePaper display (if available)...")
GPIO.cleanup()
process = subprocess.Popen(['python', '/home/pi/Desktop/Mothbox/UpdateDisplay.py'],
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
stdout, stderr = process.communicate()
if stderr:
  log_warn(f"Display error: {stderr.decode().strip()}")
else:
  print(stdout.decode())


log_section("LAUNCH -- Final Mode")

if mode == "OFF":
    log_info("Mode is OFF -- shutting down.")
    if rpiModel == 4:
        log_warn("Pi 4 not supported.")
        run_shutdown_pi5_FAST()
        quit()
    if rpiModel == 5:
        run_shutdown_pi5()
        quit()
elif mode == "DEBUG":
    log_ok("Mode is DEBUG -- wifi on, cron off, staying alive.")
    # Define the path to your script (replace 'path/to/script' with the actual path)
    debug_script_path = "/home/pi/Desktop/Mothbox/DebugMode.py"
    # Call the script using subprocess.run
    subprocess.run([debug_script_path])
    # stopcron()
elif mode == "PARTY":
    log_ok("Mode is PARTY -- lights cycling, wifi on.")
    # Define the path to your script (replace 'path/to/script' with the actual path)
    debug_script_path = "/home/pi/Desktop/Mothbox/DebugMode.py"
    # Call the script using subprocess.run
    subprocess.run([debug_script_path])
    
    party_script_path = "/home/pi/Desktop/Mothbox/Party.py"
    # Call the script using subprocess.run
    subprocess.run([party_script_path])
    # stopcron()
elif mode == "ACTIVE":
    log_ok("Mode is ACTIVE -- session running.")
else:
    log_warn(f"Unrecognised mode: {mode}")



###------ Remove the Boot lock ----------#
# Allow other scripts to be run by cron can be enabled. Run any time-sensitive sensor scripts before this (e.g. measure light)

if os.path.exists(BOOT_LOCK):
    os.remove(BOOT_LOCK)

###--------------------------------------###


log_section("LAUNCH -- Session Timer")
if runtime > 0 and mode not in ("DEBUG", "PARTY"):
    enable_shutdown()
    time.sleep(0.05)
    log_info(f"Session will run for {runtime} minute(s) then shut down.")
    schedule_shutdown(runtime)
else:
    log_info("No shutdown scheduled -- running indefinitely.")
