#!/usr/bin/python
# -*- coding:utf-8 -*-

"""
This script works with the Waveshare epaper 2.13in display
it will collect information about the pi
refresh the display
and then power off the display
leaving a 0 power high contrast display to view in the field.

--------------------------------------------------------------------------
FIRST-BOOT FILESYSTEM EXPANSION NOTICE
--------------------------------------------------------------------------
On a freshly-flashed, shrunk image, the root filesystem is expanded to fill
the SD card on first boot. That can take a little while, and if this script
runs during that window the disk-usage numbers it would normally show are
meaningless (they reflect the shrunk size, not the real card size). Rather
than show wrong numbers -- or worse, do a bunch of slow/irrelevant work
(USB mount wait, voltage sensor reads, photo counting) while the resize is
still happening -- this script checks filesystem_needs_expansion() from
firstboot_guard.py (installed alongside the scheduler) FIRST. If expansion
looks incomplete, it shows a simple "please wait" screen and exits early.
--------------------------------------------------------------------------
"""
import sys
import os
import csv
from pathlib import Path
import subprocess
import re


picdir = "/home/pi/Desktop/Mothbox/scripts/RaspberryPi_JetsonNano_Epaper/pic"
#picdir = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'pic')
libdir = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'lib')
sys.path.append("/home/pi/Desktop/Mothbox/scripts/RaspberryPi_JetsonNano_Epaper/lib")

import shutil


import psutil

import logging

# Pin gpiozero's backend BEFORE importing waveshare_epd (which imports gpiozero
# and instantiates the pins at import time).
#
# A 2025 Raspberry Pi OS kernel update renumbered the gpiochip devices -- the
# header GPIO now shows up as /dev/gpiochip11..15 instead of 0..4. gpiozero
# 2.0.1's LGPIOFactory only probes the low numbers, so it fails with
# "can not open gpiochip" and falls back down its factory chain. The rpigpio
# factory (the rpi-lgpio shim) handles the new numbering fine and is what the
# rest of the Mothbox scripts already use via `import RPi.GPIO`.
#
# Naming it explicitly does two things: silences the PinFactoryFallback warning,
# and stops gpiozero from ever sliding further down the chain to the `native`
# factory, which pokes /dev/mem directly and is not reliable on a Pi 5.
# setdefault() so it can still be overridden from the environment when debugging.
os.environ.setdefault("GPIOZERO_PIN_FACTORY", "rpigpio")

from waveshare_epd import epd2in13_V4
import time
import traceback

# Every screen is drawn by display_render.py (pure PIL, no hardware), so the
# layout can be iterated on a computer with tools/display_mock.py.
import display_render


# --- First-boot filesystem-expansion check (see firstboot_guard.py) ---
# Lives alongside scheduler.py at /home/pi/Desktop/Mothbox/. Import failures
# are handled gracefully -- this display script should never crash just
# because the guard module is missing or older.
try:
    from firstboot_guard import filesystem_needs_expansion
except Exception as _e:
    print(f"[UpdateDisplay] Could not import firstboot_guard ({_e}) -- "
          f"skipping first-boot expansion check.")
    filesystem_needs_expansion = None
# --- End first-boot import ---

SETTING_DEFAULTS = {
    "bat_80perVolts": 12.0,
    "bat_20perVolts": 11.0,
    "bat_Wh":         1.0,
    "bat_voltage":    1.0,
}
# load in the schedule CSV
def load_settings(filename):
    result = dict(SETTING_DEFAULTS)
    try:
        with open(filename, newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            for row in reader:
                setting = row["SETTING"]
                value   = row["VALUE"]
                try:
                    if setting == "bat_voltage":
                        result["bat_voltage"] = float(value)
                    elif setting == "bat_Wh":
                        result["bat_Wh"] = float(value)
                    elif setting == "bat_80perVolts":
                        result["bat_80perVolts"] = float(value)
                    elif setting == "bat_20perVolts":
                        result["bat_20perVolts"] = float(value)
                except ValueError:
                    print(f"WARNING: Could not parse '{setting}' value '{value}', using default {result[setting]}")
        return result
    except FileNotFoundError:
        print(f"Error: CSV file not found: {filename}")
        return None
    
    
# Control files live on the JOURNALED ext4 root now, not the FAT boot
# partition -- see mothbox_paths.py for why (Oct 2026: FAT corruption bricked
# deployed boxes). Writes go to STATE_ROOT; reads fall back to the old boot
# location so a box upgrading in place keeps its existing values.
try:
    from mothbox_paths import STATE_ROOT as _STATE_ROOT, resolve_read as _resolve_read, ensure_state_dir as _ensure_state_dir
    _ensure_state_dir()
    CONTROL_ROOT = _STATE_ROOT
except Exception:                       # module missing -> old behaviour, still runs
    CONTROL_ROOT = Path("/boot/firmware/mothbox_custom/system/controls")
    def _resolve_read(p):
        return p


def read_control(path: Path, key: str, default=None):
    """
    Reads a single key=value control file.
    Safe against missing, empty, or corrupted files.
    """
    # Prefer the ext4 copy; fall back to the legacy boot-partition one so a
    # box upgrading in place keeps its existing value, and so the shipped
    # read-only files (softwareversion / safetygb) are found.
    path = _resolve_read(path)
    if not path.exists():
        return default

    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip()
    except Exception as e:
        print(f"⚠️ Warning: Failed reading {path}: {e}")

    return default


# ---- Load Controls ----


usersettingsFpath="/boot/firmware/mothbox_custom/mothbox_settings.csv"




settings = load_settings(usersettingsFpath)
if settings is None:
    print("Could not load settings, using battery defaults")
    settings = dict(SETTING_DEFAULTS)

bat80       = settings["bat_80perVolts"]
bat20       = settings["bat_20perVolts"]
bat_Wh      = settings["bat_Wh"]
bat_voltage = settings["bat_voltage"]

LastCalibration= float(read_control(CONTROL_ROOT / "lastcalibration.txt", "lastcalibration", 0))

computerName = read_control(CONTROL_ROOT / "name.txt", "name", "errorname")
print(f"Mothbox Name: {computerName}")


# We will receive the mode from the control values

mode = read_control(CONTROL_ROOT / "mode.txt", "mode", "ERRORMODE")

print("Current Mothbox MODE: ", mode)


# --------------------------------------------------------------------
# First-boot filesystem expansion check -- show "please wait" and bail
# out BEFORE any of the slower/less-meaningful info gathering below
# (USB mount wait, voltage sensor reads, disk usage, photo counting).
# --------------------------------------------------------------------
if filesystem_needs_expansion is not None:
    try:
        still_expanding = filesystem_needs_expansion()
    except Exception as e:
        print(f"[UpdateDisplay] Error checking filesystem expansion state: {e}")
        still_expanding = False

    if still_expanding:
        print("[UpdateDisplay] Root filesystem not yet expanded -- showing wait screen.")
        try:
            image = display_render.render_wait()     # logo + "unpacking" notice
            epd = epd2in13_V4.EPD()
            logging.info("init (first-boot wait screen)")
            epd.init()
            epd.display(epd.getbuffer(image))
            logging.info("Display Go to Sleep... (first-boot wait screen)")
            epd.sleep()

        except IOError as e:
            logging.info(e)
        except Exception as e:
            print(f"[UpdateDisplay] Error rendering first-boot wait screen: {e}")

        # Skip the rest of the normal display update this run -- the
        # scheduler / shutdown guard will call this again on a later
        # attempt once the filesystem has finished expanding.
        sys.exit(0)
# --- End first-boot expansion check ---


# ------------- Gathering Information to Display --------------------#



### Disk Usage

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".heic"}
def count_photos(folder):
    count = 0
    for root, dirs, files in os.walk(folder):
        for name in files:
            if os.path.splitext(name)[1].lower() in PHOTO_EXTENSIONS:
                count += 1
    return count

time.sleep(6) # need to wait for USB drives to mount


# Check for external drives
usb_drives = []
for part in psutil.disk_partitions():
    # 1. Only look at /media or /mnt
    # 2. Safety: Ignore anything that is an internal SD card (mmcblk)
    if (part.mountpoint.startswith('/media') or part.mountpoint.startswith('/mnt')) and "mmcblk" not in part.device:
        try:
            usage = shutil.disk_usage(part.mountpoint)
            total_ext = usage.total // (2**30)
            
            # Ignore ghost drives (0GB)
            if total_ext == 0:
                continue 

            photos_folder = os.path.join(part.mountpoint, "photos_backup_" + computerName)
            photo_count = 0
            
            if os.path.isdir(photos_folder):
                photo_count = count_photos(photos_folder)
                
            usb_drives.append(dict(free_gb=usage.free / 2**30, total_gb=usage.total / 2**30,
                                   photos=photo_count))

        except PermissionError:
            continue
            

#internal disk
total, used, free = shutil.disk_usage("/")
free_gb = free / 2**30
photo_count_int = count_photos("/home/pi/Desktop/Mothbox/photos_backedup")+ count_photos("/home/pi/Desktop/Mothbox/photos")





# Wake Time

try:
    nexttime = int(read_control(CONTROL_ROOT / "nextwake.txt", "nextwake", "-1"))
except (ValueError, TypeError):
    print(" Could not parse nextwake, defaulting to -1")
    nexttime = -1

# Schedule Stuff
hours = read_control(CONTROL_ROOT / "hours.txt", "hours", "errorhours")

#weekdays=control_values.get("weekdays", "error")
weekdays = read_control(CONTROL_ROOT / "weekdays.txt", "weekdays", "errorweekdays")


#mins=control_values.get("minutes", "error")
mins = read_control(CONTROL_ROOT / "minutes.txt", "minutes", "errorminutes")

#runtime=control_values.get("runtime", "error")
runtime = read_control(CONTROL_ROOT / "runtime.txt", "runtime", "erroruntime")

photo_interval =read_control(CONTROL_ROOT / "photo_interval.txt", "photo_interval", "errInt")


# UTCoffset
#UTCoff=control_values.get("UTCoff", "error")
UTCoff= read_control(CONTROL_ROOT / "utc.txt", "utc", "erroUTC")


#GPS stuff
#lat=control_values.get("lat", "error")
lat= read_control(CONTROL_ROOT / "lat.txt", "lat", "errolat")

#lon=control_values.get("lon", "error")
lon= read_control(CONTROL_ROOT / "lon.txt", "lon", "errolon")

#gpstime=control_values.get("gpstime", "error")
gpstime= read_control(CONTROL_ROOT / "gpstime.txt", "gpstime", "errgpstime")



#Software Version
softwareversion = read_control(CONTROL_ROOT / "softwareversion.txt", "softwareversion", "5")


# Which board is this, and what does its voltage sensor say? (mothbox_hw.py:
# Pro = INA219 behind the sensor rail, DIY = optional INA260, else UNKNOWN)
try:
    import mothbox_hw
    hw_name = mothbox_hw.get_hardware()
    _power = mothbox_hw.read_power()
    voltage = float(_power.get("voltage_v", -1))
    print(f"detected device: Mothbox {hw_name.upper()}   voltage sensor: {_power.get('sensor')}   "
          f"Vin: {voltage:.2f} V   I: {_power.get('current_a', 0):.3f} A")
except Exception as e:
    print("Could not read hardware / voltage:", e)
    hw_name = "pro"
    voltage = -1

# Calculate battery percentage (same formula for both models)
# v20 -> 20%, v80 -> 80%, linearly extrapolated and clamped to 0-100%
if bat80 != bat20:
    percent = 20 + (voltage - bat20) * (80 - 20) / (bat80 - bat20)
    percent = max(0, min(percent, 100))
else:
    print("WARNING: bat80 and bat20 are equal, cannot calculate percentage")
    percent = -1  # will trigger the UNKNOWN display branch...
print(f"Voltage: {voltage:.2f}V  →  {percent:.0f}%")

# ------------- Drawing --------------------#
# Render BEFORE touching the panel: if drawing fails, the panel is never
# cleared and the previous (stale but useful) screen stays up.
alerts = []
camera_state = read_control(CONTROL_ROOT / "camera.txt", "camera", "ok")
if str(camera_state).strip().lower() == "missing":
    alerts.append("CAMERA DISCONNECTED")
    print("[UpdateDisplay] camera.txt says missing -- drawing the alert bar")

info = dict(
    name=computerName, mode=mode, hw=hw_name, version=softwareversion,
    battery_pct=None if voltage == -1 or percent < 0 else percent,
    next_wake=nexttime, updated=time.time(), utc_offset=UTCoff,
    sd_free_gb=free_gb, sd_photos=photo_count_int, usb=usb_drives,
    weekdays=weekdays, hours=hours, minutes=mins, runtime=runtime, interval=photo_interval,
    lat=lat, lon=lon, alerts=alerts,
)

# The Mothbox logo is shown on the first update after each boot. It takes the
# place of the clear-to-white refresh this script always did before drawing,
# so it costs no extra refresh -- only LOGO_HOLD_S of looking at it. /run is
# a tmpfs, so the flag is gone again after the next reboot.
LOGO_FLAG = "/run/mothbox_display_logo_shown"
LOGO_HOLD_S = 2

try:
    image = display_render.render_status(info)
    logo = None
    if not os.path.exists(LOGO_FLAG):
        logo = display_render.render_logo(hw_name, softwareversion)

    logging.info("Mothbox Epaper Display")
    epd = epd2in13_V4.EPD()
    logging.info("init and Clear")
    epd.init()
    if logo is not None:
        epd.display(epd.getbuffer(logo))
        try:
            with open(LOGO_FLAG, "w") as f:
                f.write("shown\n")
        except OSError as e:
            print(f"[UpdateDisplay] could not write {LOGO_FLAG} ({e}) -- logo will show again next time")
        time.sleep(LOGO_HOLD_S)
    else:
        epd.Clear(0xFF)

    logging.info("E-paper refresh")
    epd.init()
    epd.display(epd.getbuffer(image))

    logging.info("Display Go to Sleep...")
    epd.sleep()

except IOError as e:
    # SPI/GPIO failures land here (spidev.open raises OSError/IOError when
    # /dev/spidev0.0 is missing, e.g. SPI turned off in config.txt after an
    # OS update). logging.info() is invisible at the default WARNING level,
    # so print the real error and traceback instead of failing silently.
    print(f"[UpdateDisplay] E-paper I/O error: {e}")
    traceback.print_exc()

except Exception as e:
    print(f"[UpdateDisplay] Unexpected error while updating display: {e}")
    traceback.print_exc()
    
except KeyboardInterrupt:    
    logging.info("ctrl + c:")
    epd2in13_V4.epdconfig.module_exit(cleanup=True)
    exit()


'''
    logging.info("1.Drawing on the image...")
    image = Image.new('1', (epd.height, epd.width), 255)  # 255: clear the frame    
    draw = ImageDraw.Draw(image)
    draw.rectangle([(0,0),(50,50)],outline = 0)
    draw.rectangle([(55,0),(100,50)],fill = 0)
    draw.line([(0,0),(50,50)], fill = 0,width = 1)
    draw.line([(0,50),(50,0)], fill = 0,width = 1)
    draw.chord((10, 60, 50, 100), 0, 360, fill = 0)
    draw.ellipse((55, 60, 95, 100), outline = 0)
    draw.pieslice((55, 60, 95, 100), 90, 180, outline = 0)
    draw.pieslice((55, 60, 95, 100), 270, 360, fill = 0)
    draw.polygon([(110,0),(110,50),(150,25)],outline = 0)
    draw.polygon([(190,0),(190,50),(150,25)],fill = 0)
    draw.text((120, 60), 'e-Paper demo', font = font15, fill = 0)
    draw.text((110, 90), u'微雪电子', font = font24, fill = 0)
    # image = image.rotate(180) # rotate
    epd.display(epd.getbuffer(image))
    time.sleep(2)
    
    # read bmp file 
    logging.info("2.read bmp file...")
    image = Image.open(os.path.join(picdir, 'MBlogoBWnoversion.bmp'))
    draw = ImageDraw.Draw(image)
    draw.text((120, 100), 'version 5.0.0', font = font15, fill = 255)

    epd.display(epd.getbuffer(image))
    time.sleep(1)
    
    
        time.sleep(15)

    
    
    # read bmp file 
    logging.info("More info .read bmp file...")
    #image = Image.open(os.path.join(picdir, 'MBlogoBWsmall.bmp'))
    image = Image.open(os.path.join(picdir, 'MBlogoBWsmall.bmp')).rotate(90, expand=True)
    draw = ImageDraw.Draw(image)
    draw.text((120, 20), 'version 5.0.0', font = font15, fill = 255)
    draw.text((60, 0), "Name: "+mbname, font = fontHeaders, fill = 255)

    draw.text((30, 60), "mothbox is ACTIVE", font = font15, fill = 255)

    
    draw.text((30, 80), "current time: "+time.strftime('%H:%M:%S') +" UTC:-5", font = font15, fill = 255)
    draw.text((30, 100), 'next op:'+nexttime, font = font15, fill = 255)
    #image=image.rotate(90)

    epd.display(epd.getbuffer(image))
    #time.sleep(15)
    
    
    
    
    # read bmp file on window
    logging.info("3.read bmp file on window...")
    # epd.Clear(0xFF)
    image1 = Image.new('1', (epd.height, epd.width), 255)  # 255: clear the frame
    bmp = Image.open(os.path.join(picdir, 'MBlogoBW.bmp'))
    image1.paste(bmp, (2,2))
    image1.rotate(90)
    
    epd.display(epd.getbuffer(image1))
    time.sleep(2)
    
    
    # # partial update
    logging.info("4.show time...")
    time_image = Image.new('1', (epd.height, epd.width), 255)
    time_draw = ImageDraw.Draw(time_image)
    epd.displayPartBaseImage(epd.getbuffer(time_image))
    num = 0
    while (True):
        time_draw.rectangle((120, 80, 220, 105), fill = 255)
        time_draw.text((120, 80), time.strftime('%H:%M:%S'), font = font24, fill = 0)
        epd.displayPartial(epd.getbuffer(time_image))
        num = num + 1
        if(num == 10):
            break
    
    #logging.info("Clear...")
    #epd.init()
    #epd.Clear(0xFF)
'''