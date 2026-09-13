#!/usr/bin/python3
"""
i2c_survey.py -- show exactly what a Mothbox's I2C bus contains and when.

Run this on any board whose hardware detection looks wrong:
    python3 /home/pi/Desktop/Mothbox/scripts/i2c_survey.py

It scans the bus with the sensor rail as-found, raises the rail, then rescans
at 0.2 s / 1 s / 2.5 s so a slow rail is visible, identifies every chip it
sees, restores the rail, and finally runs the real detector. Paste the whole
output when reporting a misdetected board.
"""
import os, sys, time, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mothbox_hw as hw

def identify(bus, a):
    """Best-effort name for a chip at address a."""
    try:
        if a in hw.PCA9555_RANGE:
            bus.read_byte_data(a, 0x06); return "PCA9555 switch expander (Pro)"
        if a == hw.LTR303_ADDR and bus.read_byte_data(a, 0x86) == 0xA0:
            return "LTR-303 light sensor (old Pro PCB)"
        if a == hw.LTRF216A_ADDR and bus.read_byte_data(a, 0x06) == 0xB1:
            return "LTR-F216A light sensor (new Pro PCB)"
        if a == hw.INA_ADDR:
            return "INA260 (TI id) -- DIY voltage sensor" if hw._ina260_present(bus) else "INA219 (no TI id) -- Pro voltage sensor"
    except Exception as e:
        return f"answers, but id read failed ({e.__class__.__name__})"
    return "unknown device"

def scan(bus):
    return [a for a in range(0x03, 0x78) if hw._probe(bus, a)]

def show(label, bus):
    found = scan(bus)
    print(f"\n--- {label}: {len(found)} device(s) ---")
    for a in found:
        print(f"    {hex(a)}  {identify(bus, a)}")
    if not found:
        print("    (nothing)")
    return found

print("=== GPIO 27 (Pro 3.3 V sensor rail enable, active LOW) ===")
st = hw._pin_state(hw.PRO["RAIL_3V3"])
print(f"    pinctrl says: {st}   -> rail {'ON' if st and st[0]=='op' and st[1]=='lo' else 'OFF or not driven'}")
rc, out = hw._run(["i2cdetect", "-l"]); print("=== i2c buses ==="); print("    " + out.strip().replace("\n", "\n    ") if out else "    (i2cdetect not available)")

bus = hw._i2c_open()
try:
    show("bus 1 with the rail AS FOUND", bus)
    print("\n>>> raising the sensor rail (GPIO 27 -> LOW)")
    hw._out(hw.PRO["RAIL_3V3"], False)
    t0 = time.time()
    for wait in (0.2, 1.0, 2.5):
        time.sleep(max(0, wait - (time.time() - t0)))
        show(f"{wait:.1f}s after rail up", bus)
finally:
    bus.close()
    hw._out(hw.PRO["RAIL_3V3"], True)
    print("\n>>> rail restored (GPIO 27 -> HIGH)")

print("\n=== what the firmware's detector concludes ===")
info = hw.detect_hardware(write_cache=False, verbose=True)
if info["hardware"] == "pro":
    print(f"    -> PRO  ({info['detected_by']}; Pro chips answered {info['settle_s']}s after the rail came up)")
else:
    print(f"    -> DIY  (no Pro chip answered within {info['settle_s']}s of the rail coming up; "
          f"bus holds {[hex(a) for a in info['i2c_found']] or 'nothing'})")
    print("    If this board is actually a Pro, the scans above show which chips never answered.")
