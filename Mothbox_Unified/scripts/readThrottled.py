#!/usr/bin/python3
"""
readThrottled.py -- report the Pi's own under-voltage and throttling flags.

Diagnostics samples the battery and the 5 V rail every couple of minutes, which
cannot see a dip lasting a few milliseconds (the instant the flash fires, for
example). The Pi latches those events itself: `vcgencmd get_throttled` keeps
"since boot" bits that stay set until the next reboot, so a single reading at
the end of a session says whether anything happened at any point during it.

Prints one line for Diagnostics.py (and mothbox_exif.py) to pick up:
    throttled: 0x0  (nothing since boot)
    throttled: 0x50005  (now: under-voltage, throttled | since boot: under-voltage, throttled)
"""
import subprocess

# Bit meanings from the Raspberry Pi documentation for get_throttled. Bits 0-3
# describe right now; the same flags shifted up by 16 mean "has happened since
# boot".
NOW_BITS = {
    0: "under-voltage",
    1: "freq capped",
    2: "throttled",
    3: "soft temp limit",
}
SINCE_BOOT_BITS = {bit + 16: name for bit, name in NOW_BITS.items()}


def read_throttled():
    """Raw value from `vcgencmd get_throttled`, or None if it can't be read."""
    try:
        result = subprocess.run(["vcgencmd", "get_throttled"],
                                capture_output=True, text=True, timeout=10)
        # Example: "throttled=0x50005"
        return int(result.stdout.strip().split("=")[1], 16)
    except Exception:
        return None


def describe(value):
    """The one-line report for a raw get_throttled value."""
    if value is None:
        return "throttled: unavailable"
    now = [name for bit, name in NOW_BITS.items() if value & (1 << bit)]
    since = [name for bit, name in SINCE_BOOT_BITS.items() if value & (1 << bit)]
    if not now and not since:
        return f"throttled: {value:#x}  (nothing since boot)"
    parts = []
    if now:
        parts.append("now: " + ", ".join(now))
    if since:
        parts.append("since boot: " + ", ".join(since))
    return f"throttled: {value:#x}  ({' | '.join(parts)})"


if __name__ == "__main__":
    print(describe(read_throttled()))
