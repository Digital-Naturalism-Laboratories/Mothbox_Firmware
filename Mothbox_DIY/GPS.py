#!/usr/bin/python3

from gps import *
import time
from datetime import datetime
import os
import select
from timezonefinder import TimezoneFinder
from zoneinfo import ZoneInfo
from pathlib import Path
import tempfile

# ---------- Paths ----------

CONTROL_ROOT = Path("/boot/firmware/mothbox_custom/system/controls")

GPS_TIME_PATH = CONTROL_ROOT / "gpstime.txt"
UTC_PATH      = CONTROL_ROOT / "utc.txt"
LAT_PATH      = CONTROL_ROOT / "lat.txt"
LON_PATH      = CONTROL_ROOT / "lon.txt"
TZ_PATH       = CONTROL_ROOT / "timezone.txt"

CONTROL_ROOT.mkdir(parents=True, exist_ok=True)

# ---------- Atomic Write Helpers ----------

def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
    os.replace(tmp_name, path)


def atomic_update_kv(path: Path, key: str, value):
    atomic_write(path, f"{key}={value}\n")

# ---------- GPS Setup ----------

gpsd = gps(mode=WATCH_ENABLE | WATCH_NEWSTYLE)

UTCtime   = None
latitude  = None
longitude = None
got_gps_fix = False

timeout = 10
start_time = time.time()

tf = TimezoneFinder()

# ---------- Main ----------

print("startingGPS")

try:
    while time.time() - start_time < timeout:
        if select.select([gpsd.sock], [], [], 1)[0]:
            report = gpsd.next()
            if report['class'] == 'TPV':
                got_gps_fix = True
                latitude  = getattr(report, 'lat', None)
                longitude = getattr(report, 'lon', None)
                UTCtime   = getattr(report, 'time', '')

                print(latitude, "\t",
                      longitude, "\t",
                      UTCtime, "\t",
                      getattr(report, 'alt', 'nan'), "\t",
                      getattr(report, 'epv', 'nan'), "\t",
                      getattr(report, 'ept', 'nan'), "\t",
                      getattr(report, 'speed', 'nan'), "\t",
                      getattr(report, 'climb', 'nan'))
        else:
            print("Waiting for GPS data...")

        time.sleep(1)

    print("Finished Looking for GPS. GPS device found =", got_gps_fix)

    if UTCtime:
        try:
            dt = datetime.strptime(UTCtime, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            dt = datetime.strptime(UTCtime, "%Y-%m-%dT%H:%M:%SZ")

        epoch_time = int(dt.timestamp())
        print("Epoch time:", epoch_time)

        formatted_time = dt.strftime("%Y-%m-%d %H:%M:%S")

        print("Setting system UTC time...")
        os.system(f"sudo date -u -s \"{formatted_time}\"")
        os.system("sudo hwclock -w")

        atomic_update_kv(GPS_TIME_PATH, "gpstime", epoch_time)

        # ---------- Timezone from GPS ----------

        if latitude is not None and longitude is not None:
            timezone = tf.timezone_at(lat=latitude, lng=longitude)

            if timezone:
                print("Setting system timezone to:", timezone)
                os.system(f"sudo timedatectl set-timezone {timezone}")

                local_time = datetime.now(ZoneInfo(timezone))
                utc_offset_hours = round(
                    local_time.utcoffset().total_seconds() / 3600, 3
                )

                print("UTC Offset (hours):", utc_offset_hours)

                atomic_update_kv(TZ_PATH,  "timezone", timezone)
                atomic_update_kv(UTC_PATH, "utc",   utc_offset_hours)
                atomic_update_kv(LAT_PATH, "lat",      latitude)
                atomic_update_kv(LON_PATH, "lon",      longitude)

            else:
                print("Could not determine timezone from coordinates.")
                atomic_update_kv(LAT_PATH, "lat", "n/a")
                atomic_update_kv(LON_PATH, "lon", "n/a")

    else:
        print("No UTC time received before timeout")
        atomic_update_kv(LAT_PATH, "lat", "n/a")
        atomic_update_kv(LON_PATH, "lon", "n/a")

except (KeyboardInterrupt, SystemExit):
    print("Done.\nExiting.")
