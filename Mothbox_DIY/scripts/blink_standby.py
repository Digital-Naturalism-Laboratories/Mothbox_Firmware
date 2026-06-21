#!/usr/bin/python3


import os
import sys


#GPIO
import RPi.GPIO as GPIO
import time
import datetime
from datetime import datetime
import subprocess

print("----------------- Blink Standby DIY! (no Boot Lock Version)-------------------")
now = datetime.now()
formatted_time = now.strftime("%Y-%m-%d %H:%M:%S")  # Adjust the format as needed

print(f"Current time: {formatted_time}")

global onlyflash
onlyflash=False

GPIO_SW_Ch1 = 26
GPIO_SW_Ch2 = 20
GPIO_SW_Ch3 = 21
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

GPIO.setup(GPIO_SW_Ch1,GPIO.OUT)
GPIO.setup(GPIO_SW_Ch2,GPIO.OUT)
GPIO.setup(GPIO_SW_Ch3,GPIO.OUT)
print("Setup The GPIO_SW Module is [success]")

# This is a weird hack right now where because Ext Att is connected to 7, but 7 is owned by SPI, we override it
def run_cmd(cmd):
    """Run a shell command safely"""
    subprocess.run(cmd, shell=True, check=False)



def AttractOff():
    
    GPIO.output(GPIO_SW_Ch3,GPIO.HIGH)
    GPIO.output(GPIO_SW_Ch2,GPIO.HIGH)
    GPIO.output(GPIO_SW_Ch1,GPIO.HIGH)

    print("All Lights Off\n")
    
def AttractOn():

    GPIO.output(GPIO_SW_Ch3,GPIO.LOW)
    GPIO.output(GPIO_SW_Ch2,GPIO.LOW)
    GPIO.output(GPIO_SW_Ch1,GPIO.LOW)
    
    print("AllLights On\n")



# Read blink count from command-line argument; default to 2 (normal standby)
blink_count = 2
if len(sys.argv) > 1:
    try:
        blink_count = int(sys.argv[1])
    except ValueError:
        print(f"Warning: invalid blink count '{sys.argv[1]}', using default of 2")

print(f"Blinking {blink_count} time(s)...")

for _ in range(blink_count):
    AttractOn()
    time.sleep(.25)
    AttractOff()
    time.sleep(.25)

quit()