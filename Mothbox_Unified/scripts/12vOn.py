#!/usr/bin/python3
"""12vOn.py -- Pro: 12 V lights rail on. DIY: no-op.

Entry point kept for cron / Scheduler.py / TakePhoto.py, which call this
script by path. All pin logic lives in mothbox_hw.py and dispatches on the
detected hardware (Mothbox Pro PCB or DIY relay build).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.dirname(_HERE)]
import mothbox_hw

mothbox_hw.rail_12v(True)
print("12V rail on (no-op on DIY)")

