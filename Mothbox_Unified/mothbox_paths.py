#!/usr/bin/python3
"""
mothbox_paths.py -- where Mothbox files live, and why.

THE RULE: the FAT boot partition is for things HUMANS edit. Everything the
firmware generates lives on the journaled ext4 root.

Why it matters
--------------
/boot/firmware is FAT32 and has no journal. It also holds config.txt,
start*.elf, kernel*.img, cmdline.txt and overlays/ -- all read by the
bootloader BEFORE any Python runs. One allocation table covers our files and
those. A power cut while we rewrite a control file can orphan clusters
belonging to the kernel, and then the Pi will not boot at all: solid green
LED, no SD activity, unrecoverable in the field. No Python fallback can help,
because no Python ever runs.

That is not hypothetical. Oct 2026: 3 of 6 deployed boxes stopped waking and
would not boot. The firmware was writing ~725 fsync'd files per night to that
partition, ~590 of them from TakePhoto (last_photo_time.txt and camera.txt,
once per photo, every minute).

The split
---------
BOOT (FAT, user-facing, read-mostly by the firmware):
    mothbox_settings.csv, camera_settings.csv   <- the whole point of a
                                                   Windows/Mac-readable partition
    the HTML editors, timezone_list.txt, reference_boot_config.txt
    system/controls/{softwareversion,safetygb,onlyflash}.txt   <- shipped, never written
    system/controls/defaults/                                  <- shipped restore copies

STATE (ext4, journaled, written constantly, never read by a human):
    name, mode, switches, hours/minutes/weekdays/runtime, photo_interval,
    nextwake, hardware, camera, timezone, utc, lat/lon/gpstime,
    aflensposition/autogain/exposuretime/lastcalibration,
    last_photo_time, last_backup_time, shutdown_enabled, pending wifi

Upgrading in place
------------------
There is no migration step, deliberately. Writes always go to STATE_ROOT.
Reads prefer STATE_ROOT and fall back to the legacy boot copy, so a box that
upgrades keeps its existing name, schedule and calibration until the first
time each value is rewritten. The shipped files are never written, so they are
always read from the boot partition and a firmware update to them is picked up
immediately -- which a blanket migration would have broken.
"""
import os
from pathlib import Path

BOOT_CUSTOM  = Path("/boot/firmware/mothbox_custom")
LEGACY_ROOT  = BOOT_CUSTOM / "system" / "controls"      # FAT -- old home, still read
STATE_ROOT   = Path("/var/lib/mothbox/state")           # ext4 -- new home, all writes

SETTINGS_CSV = BOOT_CUSTOM / "mothbox_settings.csv"
CAMERA_CSV   = BOOT_CUSTOM / "camera_settings.csv"
DEFAULTS_DIR = LEGACY_ROOT / "defaults"


def ensure_state_dir():
    """
    Make the state directory, writable whether a script runs as root or as pi.
    Safe to call repeatedly; never raises.
    """
    try:
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        os.chmod(STATE_ROOT, 0o777)
    except Exception:
        pass
    return STATE_ROOT


def resolve_read(path):
    """
    Given a control-file path, return the one that actually has the data.

    Prefers the ext4 copy. Falls back to the legacy boot copy when the ext4
    one does not exist yet -- which covers both a box upgrading in place and
    the shipped files (softwareversion / safetygb / onlyflash) that are only
    ever read. Returns the original path if neither exists, so callers still
    get their "missing file" default.
    """
    try:
        p = Path(path)
        if p.exists():
            return p
        legacy = LEGACY_ROOT / p.name
        if legacy.exists():
            return legacy
        return p
    except Exception:
        return path


ensure_state_dir()
