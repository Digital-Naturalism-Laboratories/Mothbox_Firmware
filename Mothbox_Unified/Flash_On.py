#!/usr/bin/python3
"""Flash_On.py -- photo flash on.

Entry point kept for cron / Scheduler.py / TakePhoto.py, which call this
script by path. All pin logic lives in mothbox_hw.py and dispatches on the
detected hardware (Mothbox Pro PCB or DIY relay build).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.dirname(_HERE)]
import mothbox_hw

mothbox_hw.flash_on()

