#!/usr/bin/python3
"""Attract_On.py -- turn the attract (UV / white) lights on.

Entry point kept for cron / Scheduler.py / TakePhoto.py, which call this
script by path. All pin logic lives in mothbox_hw.py and dispatches on the
detected hardware (Mothbox Pro PCB or DIY relay build).
"""
import os
import sys

#######---- Check for Boot lock ------
BOOT_LOCK = "/run/boot_script_running"
if os.path.exists(BOOT_LOCK):
    sys.exit(0)
#-----------------------------##

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.dirname(_HERE)]
import mothbox_hw

print("----------------- Attract On! -------------------")
mothbox_hw.attract_on()

