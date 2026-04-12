#!/usr/bin/env python3
"""
check_camera.py - Minimal camera detection for Raspberry Pi 5 cam0 port
"""

import subprocess
import sys


def check_camera():
    try:
        result = subprocess.run(
            ["rpicam-hello", "--list-cameras"],
            capture_output=True,
            text=True,
            timeout=5
        )
        output = result.stdout + result.stderr

        if result.returncode == 0 and "Available cameras" in output:
            print("OK: Camera detected on cam0")
            print(f"  {output.strip()}")
            return True
        else:
            print("FAIL: No camera detected on cam0")
            print(f"  Output: {output.strip()}")
            return False

    except FileNotFoundError:
        print("FAIL: rpicam-hello not found — is rpicam-apps installed?")
        print("  Try: sudo apt install rpicam-apps")
        return False
    except subprocess.TimeoutExpired:
        print("FAIL: Camera check timed out (cable may be loose or missing)")
        return False
    except Exception as e:
        print(f"FAIL: Unexpected error — {e}")
        return False


if __name__ == "__main__":
    ok = check_camera()
    sys.exit(0 if ok else 1)