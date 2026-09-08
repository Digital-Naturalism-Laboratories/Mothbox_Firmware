#!/usr/bin/python3
"""
firstboot_guard.py -- Don't let the Mothbox scheduler power the Pi off while
the OS is still in the middle of its one-time first-boot filesystem expansion.

Background
----------
Raspberry Pi OS images are typically distributed shrunk to a small size (e.g.
via PiShrink) and grow the root filesystem to fill the SD card on first boot.
Depending on the OS version and how the image was built, this can happen via:
  - the classic init_resize.sh mechanism (temporarily replaces /sbin/init via
    cmdline.txt, resizes, then reboots itself once done), or
  - a newer initramfs-tools local-premount hook (Bookworm), or
  - a plain systemd oneshot service running alongside normal boot.

We deliberately do NOT try to identify which exact mechanism is in play --
that's fragile across OS versions and shrink tools. Instead we use two
mechanism-agnostic signals:

  1. Is the root filesystem still much smaller than the physical disk?
     (the "6GB used / 6GB total on a 128GB card" symptom)
  2. Does it look like a resize is actively running right now?
     (matches common process/service names, best-effort)

If the filesystem still looks unexpanded, we hold off on powering down for a
bounded time so a fast, spurious first-boot shutdown can't cut power mid-write
and corrupt the root filesystem. We do NOT touch the wake-alarm logic -- the
scheduler already sets that before it powers off, and that only depends on
system clock/timezone/RTC, not on resize status, so scheduled wakeups still
happen on time regardless.
"""

import os
import re
import subprocess
import time


def _root_device():
    """Return the block device backing '/', e.g. /dev/mmcblk0p2, or None."""
    try:
        out = subprocess.check_output(
            ["findmnt", "-no", "SOURCE", "/"], text=True
        ).strip()
        return out or None
    except Exception:
        return None


def _parent_disk_size_bytes(root_dev):
    """
    Return the size in bytes of the whole disk backing root_dev
    (e.g. /dev/mmcblk0 for /dev/mmcblk0p2), or None if it can't be determined.
    """
    if not root_dev:
        return None
    try:
        parent = subprocess.check_output(
            ["lsblk", "-no", "PKNAME", root_dev], text=True
        ).strip()
        if not parent:
            return None
        size_out = subprocess.check_output(
            ["lsblk", "-b", "-dno", "SIZE", f"/dev/{parent}"], text=True
        ).strip()
        return int(size_out) if size_out else None
    except Exception:
        return None


def _root_fs_size_bytes():
    """Return the total size of the mounted root filesystem in bytes."""
    st = os.statvfs("/")
    return st.f_frsize * st.f_blocks


def filesystem_needs_expansion(threshold=0.90):
    """
    Best-effort check: True if the root filesystem looks like it hasn't been
    grown to fill the disk yet (i.e. still close to the shrunk image size).

    threshold: if the filesystem is smaller than this fraction of the whole
    disk, we consider it "not yet expanded". Comparing filesystem size
    directly to disk size (rather than trying to track partition-vs-fs
    resize sub-steps separately) is deliberately simple and robust across
    resize mechanisms.

    Returns False (i.e. "assume it's fine") if we can't determine sizes --
    we never want an inability to measure to block shutdown forever.
    """
    root_dev = _root_device()
    disk_bytes = _parent_disk_size_bytes(root_dev)
    if not disk_bytes:
        return False

    fs_bytes = _root_fs_size_bytes()
    if fs_bytes <= 0:
        return False

    return (fs_bytes / disk_bytes) < threshold


_RESIZE_MARKERS = (
    "resize2fs",
    "init_resize",
    "parted",
    "growpart",
    "resizefs",
    "rpi-resizefs",
)


def resize_process_active():
    """
    Best-effort check for a currently-running resize process, matching common
    process name substrings used by various Raspberry Pi OS resize mechanisms.
    Returns False (not "definitely no") on any error -- this is a bonus signal,
    not the primary one.
    """
    try:
        out = subprocess.check_output(["ps", "-eo", "args"], text=True)
    except Exception:
        return False
    lower = out.lower()
    return any(marker in lower for marker in _RESIZE_MARKERS)


def firstboot_settling():
    """
    True if there's reason to believe first-boot filesystem housekeeping is
    still incomplete or in-flight and we should hold off on a hard power-off.
    """
    return filesystem_needs_expansion()


def wait_for_firstboot_settle(timeout_s=600, poll_s=10, log=print):
    """
    Blocks (checking periodically) until the root filesystem looks fully
    expanded, or until timeout_s elapses.

    Returns True  -- filesystem looks settled, safe to proceed with power-off.
    Returns False -- timed out while still looking unexpanded; caller should
                     log a warning but may still choose to proceed rather than
                     block forever (e.g. if the disk really is just huge, or
                     if the resize mechanism failed and will never "complete").

    This is intentionally a bounded wait, not an indefinite one: first-boot
    setup is normally done on a bench/at home, so a few extra minutes awake is
    harmless, but a unit should never be able to get stuck awake forever due
    to a stalled resize once it's out in the field on battery.
    """
    deadline = time.time() + timeout_s
    first_check = True
    while True:
        if not filesystem_needs_expansion():
            if not first_check:
                log("[firstboot] Filesystem expansion detected complete -- proceeding.")
            return True

        if time.time() >= deadline:
            log(f"[firstboot] WARNING: root filesystem still looks unexpanded after "
                f"{timeout_s}s -- proceeding anyway rather than blocking forever. "
                f"This may indicate a stalled or failed first-boot resize.")
            return False

        if first_check:
            log("[firstboot] Root filesystem not yet expanded to fill the disk -- "
                "holding off on power-off so a resize in progress isn't interrupted.")
            first_check = False

        time.sleep(poll_s)


if __name__ == "__main__":
    print("needs_expansion:", filesystem_needs_expansion())
    print("resize_active:  ", resize_process_active())
