#!/usr/bin/python3
"""3v3SensorsOn.py -- Pro: 3.3 V sensor rail on. DIY: no-op.

Entry point kept for cron / Scheduler.py / TakePhoto.py, which call this
script by path. All pin logic lives in mothbox_hw.py and dispatches on the
detected hardware (Mothbox Pro PCB or DIY relay build).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.dirname(_HERE)]
import mothbox_hw

mothbox_hw.rail_3v3(True)
print("3V3 sensor rail on (no-op on DIY)")

