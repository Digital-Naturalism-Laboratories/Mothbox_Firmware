#!/usr/bin/python3
"""blink_standby.py -- blink the lights N times (default 2) to signal STANDBY; 8 = camera warning.

Entry point kept for cron / Scheduler.py / TakePhoto.py, which call this
script by path. All pin logic lives in mothbox_hw.py and dispatches on the
detected hardware (Mothbox Pro PCB or DIY relay build).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.dirname(_HERE)]
import mothbox_hw

import time
blink_count = 2
if len(sys.argv) > 1:
    try:
        blink_count = int(sys.argv[1])
    except ValueError:
        print(f"Warning: invalid blink count '{sys.argv[1]}', using default of 2")
print(f"Blinking {blink_count} time(s)...")
for _ in range(blink_count):
    mothbox_hw.attract_on()
    mothbox_hw.flash_on()
    time.sleep(.25)
    mothbox_hw.flash_off()
    mothbox_hw.attract_off()
    time.sleep(.25)

