#!/usr/bin/python3
"""read_power.py -- print battery voltage / current from whichever sensor this board has.

Entry point kept for cron / Scheduler.py / TakePhoto.py, which call this
script by path. All pin logic lives in mothbox_hw.py and dispatches on the
detected hardware (Mothbox Pro PCB or DIY relay build).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.dirname(_HERE)]
import mothbox_hw

# Printed in the format the EXIF diagnostics parser (mothbox_exif.py) reads:
#   Vin Voltage: 12.401 V, Current: 0.312 A
p = mothbox_hw.read_power()
if p["voltage_v"] < 0:
    print(f"Vin Voltage: unavailable ({p['sensor']})")
    sys.exit(1)
print(f"Vin Voltage: {p['voltage_v']:.3f} V, Current: {p['current_a']:.3f} A   [{p['sensor']}]")

