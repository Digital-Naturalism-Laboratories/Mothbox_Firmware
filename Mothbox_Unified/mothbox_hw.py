#!/usr/bin/python3
"""
mothbox_hw.py -- hardware abstraction layer for the unified Mothbox firmware.

One firmware image runs on two builds. Both are a Raspberry Pi 5 with the
Arducam 64MP OwlSight camera; they differ in everything wired to the GPIO:

  Mothbox PRO   custom PCB. Lights are MOSFET channels driven straight from
                GPIO (active HIGH) behind a switchable 12 V rail. Sensors
                (INA219 voltage, LTR-303 light, DS18B20 temperature) and the
                three PCA9555 I/O expanders that read the config DIP switches
                all sit behind a switchable 3.3 V sensor rail.

  Mothbox DIY   Pi 5 + 3-channel relay HAT (relay inputs are active LOW),
                an optional INA260 voltage sensor, an optional e-paper
                display, and two optional jumper pins for OFF / DEBUG mode.

How a board is identified
-------------------------
The PCA9555 expanders at I2C 0x20 / 0x21 / 0x22 exist only on the Pro PCB,
so finding any of them means "pro". They are powered from the Pro's switched
3.3 V sensor rail (GPIO 27, active LOW), so detection raises that rail first,
probes, then puts the rail back. On a DIY board GPIO 27 is unconnected and the
probe finds nothing -> "diy".

Scheduler.py runs detect_hardware() at every boot and caches the answer in
<controls>/hardware.txt; every other script just reads the cache, so there is
no I2C traffic per photo. A `hardware` row in mothbox_settings.csv
(auto | pro | diy) overrides detection for unusual builds. If detection cannot
run at all (no smbus2, no /dev/i2c-1) the firmware assumes PRO, the default
product.

Everything that touches a pin lives here, once, so the entry-point scripts
(Attract_On.py, Flash_Off.py, ...) are one-liners and cannot drift apart.
"""
import csv
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

CONTROL_ROOT  = Path("/boot/firmware/mothbox_custom/system/controls")
HARDWARE_FILE = CONTROL_ROOT / "hardware.txt"
SETTINGS_CSV  = Path("/boot/firmware/mothbox_custom/mothbox_settings.csv")

VALID_HARDWARE = ("pro", "diy")
DEFAULT_HARDWARE = "pro"          # what we assume when we cannot tell

# ---------------------------------------------------------------- pin maps --
PRO = dict(
    CH1=5, CH2=6, CH3=9, CHEXT=22,   # attract light channels, active HIGH
    CHEXT_PCB_BUG_PIN=7,             # early PCBs wired EXT to GPIO7 (SPI CE1) -- driven via pinctrl
    FLASH=19,                        # flash MOSFET, active HIGH
    RAIL_12V=23,                     # 12 V MOSFET Q8 enable, active HIGH (lights)
    RAIL_3V3=27,                     # 3.3 V MOSFET Q11 enable, active LOW (sensors + switch expanders)
)
DIY = dict(
    RELAY_ATTRACT_A=26,              # relay ch1, active LOW
    RELAY_FLASH=20,                  # relay ch2, active LOW
    RELAY_ATTRACT_B=21,              # relay ch3, active LOW
    JUMPER_OFF=16,                   # grounded -> OFF mode
    JUMPER_DEBUG=12,                 # grounded -> DEBUG mode
)

I2C_BUS        = 1
PCA9555_ADDRS  = (0x20, 0x21, 0x22)
LTR303_ADDR    = 0x29
INA_ADDR       = 0x40
INA260_MFR_ID  = 0x5449            # "TI", register 0xFE -- the INA219 has no such register

# PCA9555 registers
_REG_IN0, _REG_IN1, _REG_CFG0, _REG_CFG1 = 0x00, 0x01, 0x06, 0x07

# Pro config-switch map: name -> (expander address, pin). Copied from the
# original GetConfigSwitches.py so the switches.txt keys stay identical.
SWITCH_MAP = {
    "Active": (0x20, "P1_0"),
    "Debug":  (0x22, "P0_0"),
    "C1":     (0x22, "P0_6"),   # Party mode
    "U1":     (0x22, "P0_7"),   # Switch-schedule mode
    "h1": (0x21, "P0_7"), "h2": (0x21, "P0_6"), "h3": (0x21, "P0_5"), "h4": (0x21, "P0_4"),
    "h5": (0x21, "P0_3"), "h6": (0x21, "P0_2"), "h7": (0x21, "P0_1"), "h8": (0x21, "P0_0"),
    "h9": (0x21, "P1_7"), "h10": (0x21, "P1_6"), "h11": (0x21, "P1_5"), "h12": (0x21, "P1_4"),
    "h13": (0x21, "P1_3"), "h14": (0x21, "P1_0"), "h15": (0x21, "P1_1"), "h16": (0x21, "P1_2"),
    "h17": (0x20, "P0_6"), "h18": (0x20, "P0_5"), "h19": (0x20, "P0_4"), "h20": (0x20, "P0_3"),
    "h21": (0x20, "P0_2"), "h22": (0x20, "P0_1"), "h23": (0x20, "P0_0"), "h00": (0x20, "P0_7"),
    "d1": (0x20, "P1_7"), "d2": (0x20, "P1_6"), "d3": (0x20, "P1_5"), "d4": (0x20, "P1_4"),
    "d5": (0x20, "P1_3"), "d6": (0x20, "P1_2"), "d0": (0x20, "P1_1"),
    "A1": (0x22, "P0_5"), "A2": (0x22, "P0_4"), "A3": (0x22, "P0_3"),
    "EXT": (0x22, "P0_2"), "HI": (0x22, "P0_1"),
}
SWITCH_NAMES = list(SWITCH_MAP)     # order used when writing switches.txt


# ------------------------------------------------------------- utilities --
def _run(cmd, timeout=10):
    """Run a command, never raise. Returns (returncode, stdout)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return r.returncode, r.stdout or ""
    except Exception:
        return -1, ""


def _gpio():
    """RPi.GPIO (the rpi-lgpio shim on Pi 5), configured for BCM numbering."""
    import RPi.GPIO as GPIO
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    return GPIO


def _out(pin, high):
    """
    Drive a pin as an output at the given level. `initial` is passed to
    setup() so an active-low relay never glitches through the wrong state
    between setup() and output().
    """
    GPIO = _gpio()
    level = GPIO.HIGH if high else GPIO.LOW
    GPIO.setup(pin, GPIO.OUT, initial=level)
    GPIO.output(pin, level)


def _pin_state(pin):
    """
    ("op"|"ip"|other, "hi"|"lo") from pinctrl, without re-claiming the pin
    through RPi.GPIO (which can reset its level). None if pinctrl failed.
    e.g. "19: op dh pn | hi // GPIO19 = output"
    """
    rc, out = _run(["sudo", "pinctrl", "get", str(pin)])
    if rc != 0 or not out:
        return None
    m = re.search(r"^\s*\d+:\s+(\S+).*?\|\s*(hi|lo)\b", out, re.M)
    return (m.group(1), m.group(2)) if m else None


def _driven(pin, high):
    """True only if pinctrl reports the pin is an OUTPUT driven to that level."""
    st = _pin_state(pin)
    return bool(st and st[0] == "op" and st[1] == ("hi" if high else "lo"))


def _swap16(v):
    return ((v & 0xFF) << 8) | (v >> 8)


def _signed16(v):
    return v - 0x10000 if v > 0x7FFF else v


def _read_kv(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _write_kv(path, pairs):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        for k, v in pairs.items():
            f.write(f"{k}={v}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ------------------------------------------------------------- detection --
def settings_override():
    """'pro' / 'diy' if mothbox_settings.csv forces it, else None ('auto')."""
    try:
        with open(SETTINGS_CSV, newline="", encoding="utf-8-sig", errors="replace") as f:
            for row in csv.reader(f):
                if len(row) >= 2 and row[0].strip().lower() == "hardware":
                    v = row[1].strip().lower()
                    return v if v in VALID_HARDWARE else None
    except OSError:
        pass
    return None


def _i2c_open():
    import smbus2
    return smbus2.SMBus(I2C_BUS)


def _probe(bus, addr):
    try:
        bus.read_byte(addr)
        return True
    except Exception:
        return False


def _ina260_present(bus):
    try:
        return _swap16(bus.read_word_data(INA_ADDR, 0xFE)) == INA260_MFR_ID
    except Exception:
        return False


def detect_hardware(write_cache=True, verbose=True):
    """
    Work out which Mothbox this is. Returns a dict:
      hardware       'pro' | 'diy'
      detected_by    'settings' | 'i2c-expanders' | 'i2c-none' | 'fallback-...'
      i2c_found      list of addresses that answered
      voltage_sensor 'ina219' | 'ina260' | 'none'
      light_sensor   True/False (LTR-303 answered)
    """
    info = {"hardware": DEFAULT_HARDWARE, "detected_by": "fallback-default",
            "i2c_found": [], "voltage_sensor": "none", "light_sensor": False}

    forced = settings_override()

    # Probe I2C with the Pro sensor rail up. On a DIY board GPIO 27 is
    # unconnected, so driving it is harmless there.
    rail_was_on = None
    bus = None
    try:
        bus = _i2c_open()
    except Exception as e:
        info["detected_by"] = f"fallback-no-i2c ({e.__class__.__name__})"
    if bus is not None:
        try:
            try:
                st = _pin_state(PRO["RAIL_3V3"])
                rail_was_on = bool(st and st[0] == "op" and st[1] == "lo")
                if not rail_was_on:
                    _out(PRO["RAIL_3V3"], False)     # active LOW -> on
                    time.sleep(0.2)
            except Exception:
                pass
            found = [a for a in (*PCA9555_ADDRS, LTR303_ADDR, INA_ADDR) if _probe(bus, a)]
            info["i2c_found"] = found
            expanders = [a for a in found if a in PCA9555_ADDRS]
            if expanders:
                info["hardware"], info["detected_by"] = "pro", "i2c-expanders"
            else:
                info["hardware"], info["detected_by"] = "diy", "i2c-none"
            info["light_sensor"] = LTR303_ADDR in found
            if INA_ADDR in found:
                if _ina260_present(bus):
                    info["voltage_sensor"] = "ina260"
                else:
                    info["voltage_sensor"] = "ina219" if info["hardware"] == "pro" else "ina260"
        finally:
            try:
                bus.close()
            except Exception:
                pass
            if rail_was_on is False:
                try:
                    _out(PRO["RAIL_3V3"], True)      # back off
                except Exception:
                    pass

    if forced:
        info["hardware"], info["detected_by"] = forced, "settings"

    info["detected_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if verbose:
        print(f"[mothbox_hw] hardware={info['hardware']} ({info['detected_by']}) "
              f"i2c={[hex(a) for a in info['i2c_found']] or 'none'} "
              f"voltage_sensor={info['voltage_sensor']} light_sensor={info['light_sensor']}")
    if write_cache:
        try:
            _write_kv(HARDWARE_FILE, {
                "hardware": info["hardware"], "detected_by": info["detected_by"],
                "i2c_found": ",".join(hex(a) for a in info["i2c_found"]),
                "voltage_sensor": info["voltage_sensor"],
                "light_sensor": int(info["light_sensor"]),
                "detected_at": info["detected_at"],
            })
        except Exception as e:
            print(f"[mothbox_hw] could not write {HARDWARE_FILE}: {e}")
    return info


def hardware_info():
    """Cached detection result (dict of strings), detecting if there is no cache."""
    kv = _read_kv(HARDWARE_FILE)
    if kv.get("hardware") in VALID_HARDWARE:
        return kv
    info = detect_hardware(write_cache=True, verbose=False)
    return {k: str(v) for k, v in info.items()}


def get_hardware():
    """'pro' or 'diy'. Cache -> detect -> default. Never raises."""
    try:
        hw = hardware_info().get("hardware")
        return hw if hw in VALID_HARDWARE else DEFAULT_HARDWARE
    except Exception:
        return DEFAULT_HARDWARE


def is_pro():
    return get_hardware() == "pro"


def is_diy():
    return get_hardware() == "diy"


def body_name():
    return "Mothbox Pro" if is_pro() else "Mothbox DIY"


def vertical_flip_default():
    """Camera is mounted upside-down in the DIY enclosure."""
    return 0 if is_pro() else 1


# ------------------------------------------------------------------ rails --
def rail_12v(on):
    """Pro lights rail. No-op on DIY (relays switch the lights directly)."""
    if is_pro():
        _out(PRO["RAIL_12V"], bool(on))


def rail_3v3(on):
    """Pro sensor rail (active LOW). No-op on DIY."""
    if is_pro():
        _out(PRO["RAIL_3V3"], not on)


def rail_3v3_is_on():
    if not is_pro():
        return False
    return _driven(PRO["RAIL_3V3"], high=False)


def flash_is_on():
    """Is the flash currently being driven on? Read via pinctrl, never by re-claiming the pin."""
    if is_pro():
        return _driven(PRO["FLASH"], high=True)
    return _driven(DIY["RELAY_FLASH"], high=False)       # relay: LOW = on


# ----------------------------------------------------------------- lights --
def attract_on():
    if is_pro():
        rail_12v(True)
        for pin in (PRO["CH3"], PRO["CH2"], PRO["CH1"], PRO["CHEXT"]):
            _out(pin, True)
        _run(["sudo", "pinctrl", "set", str(PRO["CHEXT_PCB_BUG_PIN"]), "op", "dh"])
    else:
        _out(DIY["RELAY_ATTRACT_A"], False)
        _out(DIY["RELAY_ATTRACT_B"], False)
        # Relay inputs float LOW (= relay ON) before anything drives them, so
        # make the flash relay's OFF state explicit unless the flash is in use.
        if not flash_is_on():
            _out(DIY["RELAY_FLASH"], True)
    print("Attract Lights On")


def attract_off(respect_flash=True):
    """
    Attract lights off. The Pro's 12 V rail is shared with the flash, and on
    the DIY the relay HAT is shared too, so if the flash is on right now it is
    left alone (a photo may be in progress).
    """
    flash_busy = respect_flash and flash_is_on()
    if is_pro():
        for pin in (PRO["CH3"], PRO["CH2"], PRO["CH1"], PRO["CHEXT"]):
            _out(pin, False)
        _run(["sudo", "pinctrl", "set", str(PRO["CHEXT_PCB_BUG_PIN"]), "op", "dl"])
        if flash_busy:
            print("Flash is on -- leaving the shared 12V rail up for it")
        else:
            rail_12v(False)
    else:
        _out(DIY["RELAY_ATTRACT_A"], True)
        _out(DIY["RELAY_ATTRACT_B"], True)
        if flash_busy:
            print("Flash is on -- leaving its relay alone")
        else:
            _out(DIY["RELAY_FLASH"], True)
    print("Attract Lights Off")


def flash_on():
    if is_pro():
        _out(PRO["FLASH"], True)
        rail_12v(True)
    else:
        _out(DIY["RELAY_FLASH"], False)


def flash_off():
    if is_pro():
        _out(PRO["FLASH"], False)          # rail stays up for the attract lights
    else:
        _out(DIY["RELAY_FLASH"], True)


def all_lights_off():
    """Everything dark, rails down. Used by DEBUG mode and shutdown paths."""
    if is_pro():
        for pin in (PRO["CH3"], PRO["CH2"], PRO["CH1"], PRO["CHEXT"], PRO["FLASH"]):
            _out(pin, False)
        _run(["sudo", "pinctrl", "set", str(PRO["CHEXT_PCB_BUG_PIN"]), "op", "dl"])
        rail_12v(False)
    else:
        for pin in (DIY["RELAY_ATTRACT_A"], DIY["RELAY_ATTRACT_B"], DIY["RELAY_FLASH"]):
            _out(pin, True)
    print("All Lights Off")


def all_lights_on():
    """Everything lit. Used by PARTY mode."""
    if is_pro():
        rail_12v(True)
        for pin in (PRO["CH3"], PRO["CH2"], PRO["CH1"], PRO["CHEXT"], PRO["FLASH"]):
            _out(pin, True)
        _run(["sudo", "pinctrl", "set", str(PRO["CHEXT_PCB_BUG_PIN"]), "op", "dh"])
    else:
        for pin in (DIY["RELAY_ATTRACT_A"], DIY["RELAY_ATTRACT_B"], DIY["RELAY_FLASH"]):
            _out(pin, False)
    print("All Lights On")


# ------------------------------------------------------------------ power --
def _read_ina219(bus):
    """Port of scripts/read_Vin.py: calibration 4096, 33 mOhm shunt on the Pro PCB."""
    bus.write_word_data(INA_ADDR, 0x05, _swap16(4096))
    raw_v = _swap16(bus.read_word_data(INA_ADDR, 0x02)) >> 3
    raw_i = _signed16(_swap16(bus.read_word_data(INA_ADDR, 0x04)))
    return raw_v * 0.004, raw_i * 0.0003


def _read_ina260(bus):
    """INA260: bus voltage 1.25 mV/LSB (reg 0x02), current 1.25 mA/LSB signed (reg 0x01)."""
    raw_v = _swap16(bus.read_word_data(INA_ADDR, 0x02))
    raw_i = _signed16(_swap16(bus.read_word_data(INA_ADDR, 0x01)))
    return raw_v * 0.00125, raw_i * 0.00125


def read_power():
    """
    {'voltage_v': float, 'current_a': float, 'sensor': 'ina219'|'ina260'|'none'}
    voltage_v is -1 when nothing could be read. On the Pro this raises the
    sensor rail if needed and restores it afterwards.
    """
    result = {"voltage_v": -1.0, "current_a": 0.0, "sensor": "none"}
    sensor = hardware_info().get("voltage_sensor", "none")
    if sensor == "none":
        sensor = "ina219" if is_pro() else "ina260"   # cache from an older boot; try anyway
    rail_was_on = None
    try:
        if is_pro():
            rail_was_on = rail_3v3_is_on()
            if not rail_was_on:
                rail_3v3(True)
                time.sleep(0.2)
        bus = _i2c_open()
        try:
            v, i = (_read_ina219 if sensor == "ina219" else _read_ina260)(bus)
        finally:
            bus.close()
        result.update(voltage_v=round(v, 3), current_a=round(i, 3), sensor=sensor)
    except Exception as e:
        print(f"[mothbox_hw] voltage read failed ({sensor}): {e}")
    finally:
        if rail_was_on is False:
            try:
                rail_3v3(False)
            except Exception:
                pass
    return result


# --------------------------------------------------------------- switches --
def _read_pro_switches(bus):
    ports = {}
    for addr in PCA9555_ADDRS:
        bus.write_byte_data(addr, _REG_CFG0, 0xFF)   # all inputs
        bus.write_byte_data(addr, _REG_CFG1, 0xFF)
        time.sleep(0.02)
        p0 = (~bus.read_byte_data(addr, _REG_IN0)) & 0xFF   # switches pull LOW when on
        p1 = (~bus.read_byte_data(addr, _REG_IN1)) & 0xFF
        ports[addr] = (p0, p1)
    out = {}
    for name, (addr, pin) in SWITCH_MAP.items():
        port, bit = pin.split("_")
        out[name] = (ports[addr][0 if port == "P0" else 1] >> int(bit)) & 1
    return out


def _read_diy_jumpers():
    GPIO = _gpio()
    for pin in (DIY["JUMPER_OFF"], DIY["JUMPER_DEBUG"]):
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    time.sleep(0.05)
    off_grounded   = GPIO.input(DIY["JUMPER_OFF"]) == GPIO.LOW
    debug_grounded = GPIO.input(DIY["JUMPER_DEBUG"]) == GPIO.LOW
    out = {name: 0 for name in SWITCH_NAMES}
    if off_grounded:
        out.update(Active=0, Debug=0)
    elif debug_grounded:
        out.update(Active=1, Debug=1)
    else:
        out.update(Active=1, Debug=0)
    return out


def read_switches():
    """
    Every switch in SWITCH_NAMES -> 0/1. Pro: the DIP switches via the three
    PCA9555 expanders (sensor rail raised for the read). DIY: only Active and
    Debug exist, from the two jumper pins; everything else is 0, which makes
    the Scheduler fall back to the CSV schedule exactly as before.
    """
    if not is_pro():
        return _read_diy_jumpers()
    rail_was_on = rail_3v3_is_on()
    if not rail_was_on:
        rail_3v3(True)
        time.sleep(0.15)
    try:
        bus = _i2c_open()
        try:
            return _read_pro_switches(bus)
        finally:
            bus.close()
    finally:
        if not rail_was_on:
            rail_3v3(False)


if __name__ == "__main__":
    import json
    print(json.dumps(detect_hardware(write_cache=False), indent=1))
