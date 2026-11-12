#!/usr/bin/python3
"""Attract_On.py -- turn the attract (UV / white) lights on.

Entry point kept for cron / Scheduler.py / TakePhoto.py, which call this
script by path. All pin logic lives in mothbox_hw.py and dispatches on the
detected hardware (Mothbox Pro PCB or DIY relay build).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.dirname(_HERE)]

#######---- Only inside a session window ------
# The Scheduler says what the box is doing in /run/mothbox_session (see
# session.py): lights only while it is "active" (or "debug", so this still
# works when run by hand).
import session
if not session.allows(session.LIGHTS_AND_PHOTOS):
    sys.exit(0)
#-----------------------------##

import mothbox_hw

print("----------------- Attract On! -------------------")
mothbox_hw.attract_on()

