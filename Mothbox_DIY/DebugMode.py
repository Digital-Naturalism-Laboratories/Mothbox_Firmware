#!/usr/bin/python3

'''
This is a special script to debug mothboxes with which will
-Stop cron
-Stop the internet from going off
-Turning off the bright UV 
-stop the mothbox from shutting down
'''


import subprocess


#GPIO
import RPi.GPIO as GPIO
import time
import datetime
from datetime import datetime
from pathlib import Path
import os
print("----------------- Activate DEBUG for DIY-------------------")

now = datetime.now()
formatted_time = now.strftime("%Y-%m-%d %H:%M:%S")  # Adjust the format as needed

print(f"Current time: {formatted_time}")
print("----------------- STOP CRON-------------------")


def stop_cron():
    """Runs the command 'service cron stop' to stop the cron service."""
    try:
        subprocess.run(["sudo", "service", "cron", "stop"], check=True)
        print("Cron service stopped successfully.")
    except subprocess.CalledProcessError as error:
        print("Error stopping cron service:", error)

stop_cron()



print("----------------- ATTRACT OFF-------------------")


Relay_Ch1 = 26
Relay_Ch2 = 20
Relay_Ch3 = 21

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

GPIO.setup(Relay_Ch1,GPIO.OUT)
GPIO.setup(Relay_Ch2,GPIO.OUT)

GPIO.setup(Relay_Ch3,GPIO.OUT)

print("Setup The Relay Module is [success]")



def AttractOn():
    GPIO.output(Relay_Ch3,GPIO.LOW)
    if(onlyflash):
        GPIO.output(Relay_Ch2,GPIO.LOW)
        print("Always Flash mode is on")
    else:
        GPIO.output(Relay_Ch2,GPIO.HIGH)

    GPIO.output(Relay_Ch1,GPIO.LOW)
    print("Attract Lights On\n")
    
def AttractOff():
    GPIO.output(Relay_Ch1,GPIO.HIGH)

    GPIO.output(Relay_Ch2,GPIO.HIGH)
    GPIO.output(Relay_Ch3,GPIO.HIGH)

    print("Attract Lights Off\n")



#AttractOn()
AttractOff()


## STOP THE INTERNET FROM STOPPING
print("----------------- KEEP INTERNET ON-------------------")

# Define the path to your script (replace 'path/to/script' with the actual path)
script_path = "/home/pi/Desktop/Mothbox/scripts/MothPower/stop_lowpower.sh"

# Call the script using subprocess.run
subprocess.run([script_path])

print("WIFI Script execution completed!")


# STOP SCHEDULED SHUTDOWN
## STOP THE PI FROM STOPPING
print("----------------- KEEP PI ON INDEFINITLEY-------------------")


def atomic_write(path, content):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def atomic_update_kv(path, key, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    lines = []
    if os.path.exists(path):
        with open(path, "r") as f:
            for line in f:
                if "=" in line:
                    lines.append(line)

    found = False
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i] = f"{key}={value}\n"
            found = True

    if not found:
        lines.append(f"{key}={value}\n")

    atomic_write(path, "".join(lines))
       
CONTROL_ROOT = Path("/boot/firmware/mothbox_custom/system/controls")


def read_control(path: Path, key: str, default=None):
    """
    Reads a single key=value control file.
    Safe against missing, empty, or corrupted files.
    """
    if not path.exists():
        return default

    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip()
    except Exception as e:
        print(f"⚠️ Warning: Failed reading {path}: {e}")

    return default


# ---- Load Controls ----
def unenable_shutdown():
    atomic_update_kv(
        os.path.join(CONTROL_ROOT, "shutdown_enabled.txt"),
        "shutdown_enabled",
        "false"
    )

unenable_shutdown()
