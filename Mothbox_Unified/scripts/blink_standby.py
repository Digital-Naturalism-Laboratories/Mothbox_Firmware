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
# Usage: blink_standby.py [count] [seconds_per_phase]
#
# The two patterns have to be unmistakable from across a field, so they differ
# in RHYTHM as well as count: "all good" is 2 quick blinks, the camera warning
# is 8 blinks at half speed. Counting flashes is hard; noticing that something
# is blinking slowly and insistently is not.
FAST = 0.25      # "everything is OK"
SLOW = 0.5       # warning -- twice as slow

blink_count = 2
if len(sys.argv) > 1:
    try:
        blink_count = int(sys.argv[1])
    except ValueError:
        print(f"Warning: invalid blink count '{sys.argv[1]}', using default of 2")

# Default the speed from the count so a caller that passes only a number still
# gets the slow warning rhythm for anything that is not the 2-blink OK.
phase = FAST if blink_count == 2 else SLOW
if len(sys.argv) > 2:
    try:
        phase = float(sys.argv[2])
    except ValueError:
        print(f"Warning: invalid blink speed '{sys.argv[2]}', using {phase}s")

print(f"Blinking {blink_count} time(s) at {phase}s per phase "
      f"({'WARNING - slow' if phase >= SLOW else 'OK - quick'})...")
for _ in range(blink_count):
    mothbox_hw.attract_on()
    mothbox_hw.flash_on()
    time.sleep(phase)
    mothbox_hw.flash_off()
    mothbox_hw.attract_off()
    time.sleep(phase)
    mothbox_hw.flash_off()
    mothbox_hw.attract_off()
    time.sleep(.25)

