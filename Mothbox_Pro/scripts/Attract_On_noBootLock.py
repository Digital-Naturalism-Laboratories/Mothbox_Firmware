#!/usr/bin/python3


import os
import sys


#GPIO
import RPi.GPIO as GPIO
import time
import datetime
from datetime import datetime
import subprocess
import re

print("----------------- Attract On! (no Boot Lock Version)-------------------")
now = datetime.now()
formatted_time = now.strftime("%Y-%m-%d %H:%M:%S")  # Adjust the format as needed

print(f"Current time: {formatted_time}")

global onlyflash
onlyflash=False

GPIO_SW_Ch1 = 5
GPIO_SW_Ch2 = 6
GPIO_SW_Ch3 = 9
GPIO_SW_ChExt = 22 # Currently the PCBs have a bug where they are set to 7 but should change
GPIO_SW_Flash = 19  # flash MOSFET -- read-only here, shares the 12V rail
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

GPIO.setup(GPIO_SW_Ch1,GPIO.OUT)
GPIO.setup(GPIO_SW_Ch2,GPIO.OUT)
GPIO.setup(GPIO_SW_Ch3,GPIO.OUT)
GPIO.setup(GPIO_SW_ChExt,GPIO.OUT)
print("Setup The GPIO_SW Module is [success]")

# This is a weird hack right now where because Ext Att is connected to 7, but 7 is owned by SPI, we override it
def run_cmd(cmd):
    """Run a shell command safely"""
    subprocess.run(cmd, shell=True, check=False)



def AttractOn():
    run_cmd("python /home/pi/Desktop/Mothbox/scripts/12vOn.py")
    
    GPIO.output(GPIO_SW_Ch3,GPIO.HIGH)
    GPIO.output(GPIO_SW_Ch2,GPIO.HIGH)
    GPIO.output(GPIO_SW_Ch1,GPIO.HIGH)
    GPIO.output(GPIO_SW_ChExt,GPIO.HIGH)
    # Take GPIO7 from SPI and drive HIGH
    run_cmd("sudo pinctrl set 7 op dh")

    print("Attract Lights On\n")
    
def flash_is_on():
    """
    True if the flash MOSFET pin (GPIO 19) is currently driven HIGH.

    Read through pinctrl instead of RPi.GPIO so this script doesn't re-claim
    GPIO 19 as an output -- with the rpi-lgpio backend that can reset the pin.
    On any error assume the flash is OFF so the rail still gets powered down:
    that is the power-saving default and exactly the old behaviour.
    """
    try:
        out = subprocess.run(["sudo", "pinctrl", "get", str(GPIO_SW_Flash)],
                             capture_output=True, text=True, timeout=5).stdout
        # e.g. "19: op dh pn | hi // GPIO19 = output"
        return " op " in out and re.search(r"\|\s*hi\b", out) is not None
    except Exception:
        return False


def AttractOff():
    # The 12V rail (GPIO 23, scripts/12vOff.py) is shared by the attract
    # channels AND the flash. Only cut it if the flash isn't using it --
    # otherwise running Attract_Off while a photo is being taken kills the
    # flash as collateral damage.
    if flash_is_on():
        print("Flash is on -- leaving the shared 12V rail up for it")
    else:
        run_cmd("python /home/pi/Desktop/Mothbox/scripts/12vOff.py")

    GPIO.output(GPIO_SW_Ch3,GPIO.LOW)
    GPIO.output(GPIO_SW_Ch2,GPIO.LOW)
    GPIO.output(GPIO_SW_Ch1,GPIO.LOW)
    GPIO.output(GPIO_SW_ChExt,GPIO.LOW)
    # Take GPIO7 from SPI and drive LOW
    run_cmd("sudo pinctrl set 7 op dl")

    print("Attract Lights Off\n")


AttractOn()
#AttractOff()


