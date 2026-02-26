#!/usr/bin/env python3

import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import os
import tempfile

CONTROL_ROOT = Path("/boot/firmware/mothbox_custom/system/controls")
ZONEINFO_DIR = Path("/usr/share/zoneinfo")


# ---------- Atomic File Write ----------

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


# ---------- Safe Read ----------

def get_control_values(path: Path):
    values = {}
    if not path.exists():
        return values

    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    except Exception as e:
        print(f"⚠️ Warning: Failed reading {path}: {e}")

    return values


# ---------- Timezone Logic ----------

def get_current_timezone():
    tz_file = Path("/etc/timezone")
    if tz_file.exists():
        return tz_file.read_text().strip()
    return "UTC"


def set_system_timezone(tz_name):
    zoneinfo_path = ZONEINFO_DIR / tz_name

    if not zoneinfo_path.exists():
        raise ValueError(f"Invalid timezone: {tz_name}")

    subprocess.run(
        ["sudo", "ln", "-sf", str(zoneinfo_path), "/etc/localtime"],
        check=True
    )

    Path("/etc/timezone").write_text(tz_name + "\n")


def get_utc_offset_hours(tz_name):
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    offset_seconds = now.utcoffset().total_seconds()
    return round(offset_seconds / 3600, 3)


# ---------- Main ----------

def main():
    tz_path = CONTROL_ROOT / "timezone.txt"
    utc_path = CONTROL_ROOT / "utc.txt"

    controls = get_control_values(tz_path)

    if "timezone" not in controls:
        print("TimezoneUpdater: No timezone field in timezone.txt")
        return

    desired_tz = controls["timezone"].strip()
    current_tz = get_current_timezone()

    if desired_tz != current_tz:
        print(f"TimezoneUpdater: Updating timezone {current_tz} → {desired_tz}")
        set_system_timezone(desired_tz)

    utc_offset = get_utc_offset_hours(desired_tz)

    atomic_update_kv(utc_path, "utc", utc_offset)

    print(f"TimezoneUpdater: Active TZ={desired_tz}, UTC offset={utc_offset}")


if __name__ == "__main__":
    main()
