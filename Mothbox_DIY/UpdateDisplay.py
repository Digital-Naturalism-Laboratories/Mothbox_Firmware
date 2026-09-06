#!/usr/bin/python
# -*- coding:utf-8 -*-

"""
This script works with the Waveshare epaper 2.13in display
it will collect information about the pi
refresh the display
and then power off the display
leaving a 0 power high contrast display to view in the field.

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
from PIL import Image,ImageDraw,ImageFont
import traceback

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
    
    
CONTROL_ROOT = Path("/boot/firmware/mothbox_custom/system/controls")


def read_control(path: Path, key: str, default=None):
    """
    Reads a single key=value control file.
    Safe against missing, empty, or corrupted files.
    """
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


def sort_hours_night_order(hours_str):
    """
    Reorders a schedule hours string so it reads in the order the hours
    happen over a night: noon (12) first, through midnight, ending at 11am.
    Keeps whatever separator the control file used, and returns the string
    unchanged if it can't be parsed as a list of hours.
    """
    if not hours_str:
        return hours_str

    sep = None
    for candidate in (";", ",", " "):
        if candidate in hours_str:
            sep = candidate
            break

    parts = [p.strip() for p in hours_str.split(sep)] if sep else [hours_str.strip()]

    try:
        hrs = [int(p) for p in parts if p != ""]
    except ValueError:
        return hours_str

    hrs.sort(key=lambda h: (h - 12) % 24)
    return (sep if sep else ";").join(str(h) for h in hrs)


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
external_info = ""
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

            free_ext = usage.free // (2**30)
            used_ext = total_ext - free_ext
            photos_folder = os.path.join(part.mountpoint, "photos_backup_" + computerName)
            photo_count = 0
            
            if os.path.isdir(photos_folder):
                photo_count = count_photos(photos_folder)
                
            external_info += f"USB: {used_ext} GB/{total_ext}GB used\n          {photo_count} photos\n" 

        except PermissionError:
            continue
            

#internal disk
total, used, free = shutil.disk_usage("/")
total_gb = total // (2**30)
free_gb = free // (2**30)
used_gb = total_gb-free_gb
photo_count_int = count_photos("/home/pi/Desktop/Mothbox/photos_backedup")+ count_photos("/home/pi/Desktop/Mothbox/photos")





# Wake Time

try:
    nexttime = int(read_control(CONTROL_ROOT / "nextwake.txt", "nextwake", "-1"))
except (ValueError, TypeError):
    print(" Could not parse nextwake, defaulting to -1")
    nexttime = -1

# Schedule Stuff
hours = read_control(CONTROL_ROOT / "hours.txt", "hours", "errorhours")
hours = sort_hours_night_order(hours)

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


# Determine which voltage reading method to use based on software version
if softwareversion.startswith("4"):
    print("detected device: Mothbox DIY")
    # Mothbox DIY (4.x) — INA260 sensor via I2C
    import board
    import adafruit_ina260
    try:
        i2c = board.I2C()
        ina260 = adafruit_ina260.INA260(i2c)
        voltage = ina260.voltage
        print("Current: %.2f mA  Voltage: %.2f V  Power: %.2f mW" % (
            ina260.current, ina260.voltage, ina260.power))
    except (OSError, ValueError) as e:
        print("INA260 sensor NOT CONNECTED:", e)
        voltage=-1

else:
    # Mothbox Pro (5.x) or unknown — read via PCB voltage script
    # Default to Pro behavior if version is unrecognized
    if not softwareversion.startswith("5"):
        print(f"Unrecognized software version '{softwareversion}', defaulting to Mothbox Pro voltage reading")
    else:
        print("detected device: Mothbox PRO")

    try:
        result3 = subprocess.run(
            ["python3", "/home/pi/Desktop/Mothbox/scripts/3v3SensorsOn.py"],
            capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as e:
        print("Error turning on sensors:", e)

    try:
        result = subprocess.run(
            ["python3", "/home/pi/Desktop/Mothbox/scripts/read_Vin.py"],
            capture_output=True, text=True, check=True
        )
        output = result.stdout.strip()
        match = re.search(r"Voltage:\s*([\d.]+)", output)
        if match:
            voltage = float(match.group(1))
        else:
            print("Could not parse voltage from output:", output)
            voltage=-1
    except subprocess.CalledProcessError as e:
        print("Error reading voltage:", e)
        voltage=-1

# Calculate battery percentage (same formula for both models)
# v20 -> 20%, v80 -> 80%, linearly extrapolated and clamped to 0-100%
if bat80 != bat20:
    percent = 20 + (voltage - bat20) * (80 - 20) / (bat80 - bat20)
    percent = max(0, min(percent, 100))
else:
    print("WARNING: bat80 and bat20 are equal, cannot calculate percentage")
    percent = -1  # will trigger the UNKNOWN display branch...
print(f"Voltage: {voltage:.2f}V  →  {percent:.0f}%")

try:
    logging.info("Mothbox Epaper Display")
    
    epd = epd2in13_V4.EPD()
    logging.info("init and Clear")
    epd.init()
    epd.Clear(0xFF)

    # Drawing on the image
    #fontHeaders = ImageFont.truetype('/home/pi/Desktop/Mothbox/graphics/fonts/scientifica/ttf/scientificaBold.ttf', 13)
    fontHeaders = ImageFont.truetype('/home/pi/Desktop/Mothbox/graphics/fonts/Atkinson_Next/AtkinsonHyperlegibleNext-Regular.otf', 13)
    #fontHeaders = ImageFont.truetype('/home/pi/Desktop/Mothbox/graphics/fonts/Atkinson/Atkinson-Hyperlegible-Regular-102.ttf', 12)
    fontHeadersSmall = ImageFont.truetype('/home/pi/Desktop/Mothbox/graphics/fonts/Atkinson_Next/AtkinsonHyperlegibleNext-Bold.otf', 9)

    
    font8 = ImageFont.truetype('/home/pi/Desktop/Mothbox/graphics/fonts/clear-sans/TTF/ClearSans-Medium.ttf', 8)

    font_bigs=ImageFont.truetype('/home/pi/Desktop/Mothbox/graphics/fonts/clear-sans/TTF/ClearSans-Bold.ttf',8)
    
    font_robotosemicon10=ImageFont.truetype('/home/pi/Desktop/Mothbox/graphics/fonts/scientifica/ttf/scientificaBold.ttf',13)
    font_scientifica22=ImageFont.truetype('/home/pi/Desktop/Mothbox/graphics/fonts/scientifica/ttf/scientificaBold.ttf',22)
    
    font_Atkinson19 = ImageFont.truetype('/home/pi/Desktop/Mothbox/graphics/fonts/Atkinson_Next/AtkinsonHyperlegibleNext-Bold.otf', 19)

    font_Mediumtext=ImageFont.truetype('/home/pi/Desktop/Mothbox/graphics/fonts/clear-sans/TTF/ClearSans-Medium.ttf',14)
    font_Mediumtext12=ImageFont.truetype('/home/pi/Desktop/Mothbox/graphics/fonts/clear-sans/TTF/ClearSans-Regular.ttf',12)

    font_roboto10=ImageFont.truetype('/home/pi/Desktop/Mothbox/graphics/fonts/scientifica/ttf/scientificaBold.ttf',12)

    logging.info("E-paper refresh")
    epd.init()
    
    #print(epd.width) #h 250px w 122
    # Setup for portrait mode
    #image = Image.new('1', (epd.width, epd.height), 255)  # Portrait: width=122, height=250
    
    #print(epd.width) #h 250px w 122
    # Setup for landscape mode
    image = Image.new('1', (epd.height, epd.width), 255)  # Portrait: width=122, height=250
    
    draw = ImageDraw.Draw(image)
    draw.fontmode="1"
    #Start Drawing stuff to the display
    
    colW = 128
    rowH=13
    #computerName="canineDorado" #example longest name
    # Name and State
    # Draw text elements (adjust coordinates to suit portrait layout)
    #draw.text((2,7), "NAME: ", font=font8, fill=0)
    draw.text((2, -2), "" + computerName, font=font_Atkinson19, fill=0)

    draw.text((colW,5), "state: ", font=fontHeaders, fill=0)
    draw.text((colW+4,-2), "     "+mode, font=font_Atkinson19, fill=0)

    #next wake
    draw.text((2, rowH+3), 'next wake:', font=fontHeaders, fill=0)
    draw.text((1,rowH+8),  time.strftime('%Y-%m-%d %H:%M', time.localtime(nexttime)), font=font_Mediumtext, fill=0)

    draw.line([(0,2*rowH+12),(epd.height,2*rowH+12)], fill = 0,width = 1)
    draw.line([(epd.height/2,.5*rowH+12),(epd.height/2,epd.width)], fill = 0,width = 1)
    

    #Schedule Stuff

    draw.text((2, 3*rowH), "last update: ", font=fontHeaders, fill=0)
    #draw.text((0, 4*rowH), time.strftime('%m-%d %H:%M:%S') + "UTC:"+str(UTCoff), font=fontHeaders, fill=0)
    draw.text((0, 4*rowH-5), time.strftime('%m-%d %H:%M') + " UTC:"+str(UTCoff), font=font_Mediumtext12, fill=0)

    draw.line([(0,4*rowH+12),(epd.height/2,4*rowH+12)], fill = 0,width = 1)

    draw.text((2, 5*rowH), 'RUN', font=fontHeadersSmall, fill=0)
    draw.text((2, 5.6*rowH), 'TIME ', font=fontHeadersSmall, fill=0)
    draw.text((2, 5.4*rowH), '                mins', font=fontHeadersSmall, fill=0)

    draw.text((28, 5.2*rowH), runtime, font=fontHeaders, fill=0)
    
    draw.text((78, 5.4*rowH), 'INT  '+photo_interval+" min", font=fontHeadersSmall, fill=0)
    #draw.text((30, 5.3*rowH), '                ' + photo_interval+ " mins", font=fontHeaders, fill=0)

    draw.text((2, 6.5*rowH), 'DAYS ' , font=fontHeadersSmall, fill=0)
    draw.text((2, 6.3*rowH), '         ' + weekdays, font=fontHeaders, fill=0)

    draw.text((2, 7.5*rowH), 'HOURS ', font=fontHeadersSmall, fill=0)
    draw.text((2, 7.3*rowH), '         '+hours, font=fontHeaders, fill=0)

    
    if(mins!="0"):
        draw.text((2, 8.5*rowH), 'MINUTES ', font=fontHeadersSmall, fill=0)
        draw.text((2, 8.3*rowH), '                ' + mins, font=fontHeaders, fill=0)



    #Battery Stuff
    draw.line([(0,.5*rowH+12),(epd.height,.5*rowH+12)], fill = 0,width = 1)

    draw.text((colW+2, 1.8*rowH), f"BATTERY", font=fontHeaders, fill=0)
    
    if(voltage==-1):
        draw.text((colW+2, 2*rowH), f"               UNKNOWN", font=fontHeaders, fill=0)
    else:
        draw.text((colW+6, 1.4*rowH), f"          {percent:.0f}%", font=font_Atkinson19 , fill=0)

    # DISK
    # Add disk space info
    draw.text((colW, 3*rowH+2), f'SD:{used_gb}GB/{total_gb}GB used\n          {photo_count_int} photos', font=fontHeaders, fill=0)

    # Starting Y position for external info (after previous lines)
    y_pos=5*rowH+3
    if external_info:
        for line in external_info.strip().split('\n'):
            draw.text((colW, y_pos), line, font=fontHeaders, fill=0)
            y_pos += 12  # line spacing
    else:
        draw.text((colW+2, y_pos), "No USB found", font=fontHeaders, fill=0)

    #GPS stuff
    draw.text((colW+2, 7.5*rowH), 'GPS: '+str(lat) +","+str(lon), font=fontHeaders, fill=0)
    #draw.text((+2, 9*rowH), '        '+str(lon), font=font_robotosemicon10, fill=0)
    
    draw.line([(epd.height/2,6.5*rowH+12),(epd.height,6.5*rowH+12)], fill = 0,width = 1)


    #Version Stuff
    draw.line([(epd.height/2,8.7*rowH),(epd.height,8.7*rowH)], fill = 0,width = 1)

    draw.text((colW, 8.7*rowH), 'M O T H B O X', font=font_bigs, fill=0)
    draw.text((colW+3, 8.7*rowH), '                          version:'+softwareversion, font=font_bigs, fill=0)


    #image = image.rotate(180) # rotate
    # Send to display
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
 
