#!/usr/bin/python3

import os
import time
import RPi.GPIO as GPIO

# ---------------- CONFIG ----------------

# GPIO pin numbers (BCM mode)
OFF_PIN   = 16   # Active OFF pin (grounded → OFF)
DEBUG_PIN = 12   # Debug pin (grounded → DEBUG)

SWITCHES_PATH = "/boot/firmware/mothbox_custom/system/controls/switches.txt"

# ---------------------------------------


def atomic_write(path, content):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_gpio_mode():
    """
    Reads GPIO pins and returns switch states.

    Logic:
      - If OFF_PIN grounded → Active = 0, Debug = 0
      - Else if DEBUG_PIN grounded → Active = 1, Debug = 1
      - Else → Active = 1, Debug = 0
    """

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(OFF_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(DEBUG_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    time.sleep(0.05)  # settle time

    off_grounded   = (GPIO.input(OFF_PIN) == GPIO.LOW)
    debug_grounded = (GPIO.input(DEBUG_PIN) == GPIO.LOW)

    GPIO.cleanup()

    if off_grounded:
        print("OFF pin grounded → MODE = OFF")
        return {
            "Active": 0,
            "Debug": 0
        }

    if debug_grounded:
        print("DEBUG pin grounded → MODE = DEBUG")
        return {
            "Active": 1,
            "Debug": 1
        }

    print("No pins grounded → MODE = ACTIVE")
    return {
        "Active": 1,
        "Debug": 0
    }


def write_switches(path, switches):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    lines = []
    for key in ["Active", "Debug"]:
        lines.append(f"{key}={switches.get(key, 0)}\n")

    atomic_write(path, "".join(lines))


def main():
    switches = read_gpio_mode()
    write_switches(SWITCHES_PATH, switches)

    print("Updated switches.txt:")
    for k, v in switches.items():
        print(f"  {k} = {v}")


if __name__ == "__main__":
    main()
