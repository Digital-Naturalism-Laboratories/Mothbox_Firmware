#!/usr/bin/python3
"""GetConfigSwitches.py -- snapshot the physical switches into switches.txt.

Entry point kept for cron / Scheduler.py / TakePhoto.py, which call this
script by path. All pin logic lives in mothbox_hw.py and dispatches on the
detected hardware (Mothbox Pro PCB or DIY relay build).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.dirname(_HERE)]
import mothbox_hw

# Pro: the DIP switches via three PCA9555 expanders (sensor rail raised).
# DIY: only the OFF (GPIO16) and DEBUG (GPIO12) jumpers exist; all other
#      switches are written as 0, so the Scheduler keeps using the CSV
#      schedule exactly as the old DIY firmware did.
SWITCHES_PATH = str(mothbox_hw.CONTROL_ROOT / "switches.txt")

def atomic_write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def main():
    switches = mothbox_hw.read_switches()
    atomic_write(SWITCHES_PATH, "".join(f"{name}={switches.get(name, 0)}\n" for name in mothbox_hw.SWITCH_NAMES))
    on = [n for n, v in switches.items() if v]
    print(f"[{mothbox_hw.get_hardware()}] switches on: {on if on else 'none'} -> {SWITCHES_PATH}")

if __name__ == "__main__":
    main()

