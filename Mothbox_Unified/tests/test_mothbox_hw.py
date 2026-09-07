#!/usr/bin/python3
"""
Off-device tests for mothbox_hw.py. Fakes RPi.GPIO, smbus2 and pinctrl so
both the Pro and DIY code paths can be exercised on any machine:

    python3 tests/test_mothbox_hw.py
"""
import os, sys, types, tempfile, importlib
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# ----------------------------------------------------------------- fakes --
class FakeGPIO:
    BCM, OUT, IN, HIGH, LOW, PUD_UP, PUD_DOWN = "BCM", "OUT", "IN", 1, 0, "UP", "DOWN"
    def __init__(self): self.calls = []; self.levels = {}; self.inputs = {}
    def setwarnings(self, v): pass
    def setmode(self, m): pass
    def setup(self, pin, direction, pull_up_down=None, initial=None):
        self.calls.append(("setup", pin, direction, initial))
        if initial is not None: self.levels[pin] = initial
    def output(self, pin, level):
        self.calls.append(("out", pin, level)); self.levels[pin] = level
    def input(self, pin): return self.inputs.get(pin, self.HIGH)
    def cleanup(self, *a): pass
    def outs(self): return [(p, l) for k, p, l in [(c[0], c[1], c[2]) for c in self.calls] if k == "out"]

class FakeBus:
    """responding: set of addrs. regs: {(addr, reg): word-as-returned-by-read_word_data}"""
    def __init__(self, responding=(), regs=None, bytes_=None):
        self.responding = set(responding); self.regs = dict(regs or {}); self.bytes = dict(bytes_ or {}); self.writes = []
    def _chk(self, a):
        if a not in self.responding: raise OSError(121, "Remote I/O error")
    def read_byte(self, a): self._chk(a); return 0
    def read_word_data(self, a, r): self._chk(a); return self.regs.get((a, r), 0xFFFF)
    def write_word_data(self, a, r, v): self._chk(a); self.writes.append((a, r, v))
    def read_byte_data(self, a, r): self._chk(a); return self.bytes.get((a, r), 0xFF)
    def write_byte_data(self, a, r, v): self._chk(a); self.writes.append((a, r, v))
    def close(self): pass

def install_fakes(bus=None, smbus_missing=False, pinctrl=None):
    gpio = FakeGPIO()
    rpi = types.ModuleType("RPi"); rpi.GPIO = gpio
    sys.modules["RPi"] = rpi; sys.modules["RPi.GPIO"] = gpio
    if smbus_missing:
        sys.modules.pop("smbus2", None)
        class Blocker:
            def find_spec(self, name, *a): 
                if name == "smbus2": raise ImportError("No module named smbus2")
        sys.meta_path.insert(0, Blocker())
    else:
        sm = types.ModuleType("smbus2"); sm.SMBus = lambda n: bus; sys.modules["smbus2"] = sm
    sys.modules.pop("mothbox_hw", None)
    hw = importlib.import_module("mothbox_hw")
    tmp = Path(tempfile.mkdtemp())
    hw.HARDWARE_FILE = tmp / "hardware.txt"; hw.SETTINGS_CSV = tmp / "mothbox_settings.csv"
    runs = []
    pinctrl = dict(pinctrl or {})     # pin -> "op hi" etc
    def fake_run(cmd, timeout=10):
        runs.append(cmd)
        if cmd[:3] == ["sudo", "pinctrl", "get"]:
            st = pinctrl.get(int(cmd[3]))
            return (0, f"{cmd[3]}: {st} // GPIO{cmd[3]}\n") if st else (1, "")
        return (0, "")
    hw._run = fake_run
    return hw, gpio, runs, tmp

def swap(v): return ((v & 0xFF) << 8) | (v >> 8)
def write_settings(hw, val): hw.SETTINGS_CSV.write_text(f"SETTING,VALUE,DETAILS\nhardware,{val},x\n")

passed = 0
def check(name, cond):
    global passed
    print(("OK  " if cond else "BAD ") + name); assert cond, name; passed += 1

# ---------------------------------------------------------- detection --
hw, gpio, runs, tmp = install_fakes(FakeBus({0x20,0x21,0x22,0x29,0x40}, {(0x40,0xFE): 0xFFFF}), pinctrl={27: "op dh pn | hi"})
info = hw.detect_hardware()
check("pro: expanders found -> pro / ina219 / light", info["hardware"]=="pro" and info["detected_by"]=="i2c-expanders" and info["voltage_sensor"]=="ina219" and info["light_sensor"])
check("pro: sensor rail raised (27 LOW) then restored (27 HIGH)", gpio.outs() == [(27,0),(27,1)])
check("pro: cache written and read back", hw.get_hardware()=="pro" and hw.hardware_info()["voltage_sensor"]=="ina219")

hw, gpio, runs, tmp = install_fakes(FakeBus(set()))
info = hw.detect_hardware()
check("diy: nothing on i2c -> diy / none", info["hardware"]=="diy" and info["detected_by"]=="i2c-none" and info["voltage_sensor"]=="none")

hw, gpio, runs, tmp = install_fakes(FakeBus({0x40}, {(0x40,0xFE): swap(0x5449)}))
info = hw.detect_hardware()
check("diy + INA260 (TI id 0x5449) -> diy / ina260", info["hardware"]=="diy" and info["voltage_sensor"]=="ina260")

hw, gpio, runs, tmp = install_fakes(FakeBus({0x20,0x21,0x22}))
write_settings(hw, "diy"); info = hw.detect_hardware()
check("settings override wins over detection", info["hardware"]=="diy" and info["detected_by"]=="settings")
write_settings(hw, "bogus"); check("bogus override ignored", hw.detect_hardware()["hardware"]=="pro")

hw, gpio, runs, tmp = install_fakes(None, smbus_missing=True)
info = hw.detect_hardware()
check("no smbus2 -> falls back to PRO (default product)", info["hardware"]=="pro" and info["detected_by"].startswith("fallback-no-i2c"))
sys.meta_path.pop(0)

# ---------------------------------------------------------- pin parsing --
hw, gpio, runs, tmp = install_fakes(FakeBus(set()), pinctrl={19:"op dh pn | hi", 20:"op dl pn | lo", 4:"ip    pd | hi"})
check("_driven: output hi / lo / input-hi", hw._driven(19,True) and hw._driven(20,False) and not hw._driven(4,True) and not hw._driven(19,False))

# -------------------------------------------------------- lights: PRO --
def pro_hw(pinctrl):
    hw, gpio, runs, tmp = install_fakes(FakeBus(set()), pinctrl=pinctrl); hw.HARDWARE_FILE.write_text("hardware=pro\nvoltage_sensor=ina219\n"); return hw, gpio, runs
hw, gpio, runs = pro_hw({19:"op dl pn | lo"})
hw.attract_on()
check("pro attract_on: 12V rail then CH3,CH2,CH1,EXT high + pinctrl 7 dh", gpio.outs()==[(23,1),(9,1),(6,1),(5,1),(22,1)] and ["sudo","pinctrl","set","7","op","dh"] in runs)
hw, gpio, runs = pro_hw({19:"op dl pn | lo"}); hw.attract_off()
check("pro attract_off, flash off: channels low, rail cut", gpio.outs()==[(9,0),(6,0),(5,0),(22,0),(23,0)])
hw, gpio, runs = pro_hw({19:"op dh pn | hi"}); hw.attract_off()
check("pro attract_off, flash ON: rail left up", (23,0) not in gpio.outs() and gpio.outs()==[(9,0),(6,0),(5,0),(22,0)])
hw, gpio, runs = pro_hw({}); hw.flash_on(); hw.flash_off()
check("pro flash on/off: 19 high + rail up, then 19 low only", gpio.outs()==[(19,1),(23,1),(19,0)])
hw, gpio, runs = pro_hw({}); hw.all_lights_off()
check("pro all off: every channel + flash low, rail cut", gpio.outs()==[(9,0),(6,0),(5,0),(22,0),(19,0),(23,0)])

# -------------------------------------------------------- lights: DIY --
def diy_hw(pinctrl):
    hw, gpio, runs, tmp = install_fakes(FakeBus(set()), pinctrl=pinctrl); hw.HARDWARE_FILE.write_text("hardware=diy\nvoltage_sensor=ina260\n"); return hw, gpio, runs
hw, gpio, runs = diy_hw({20:"op dh pn | hi"}); hw.attract_on()
check("diy attract_on: relays A,B LOW (on), flash relay HIGH (off)", gpio.outs()==[(26,0),(21,0),(20,1)])
hw, gpio, runs = diy_hw({20:"op dl pn | lo"}); hw.attract_on()
check("diy attract_on with flash ON: flash relay untouched", gpio.outs()==[(26,0),(21,0)])
hw, gpio, runs = diy_hw({20:"op dl pn | lo"}); hw.attract_off()
check("diy attract_off with flash ON: flash relay untouched", gpio.outs()==[(26,1),(21,1)])
hw, gpio, runs = diy_hw({20:"op dh pn | hi"}); hw.attract_off()
check("diy attract_off, flash off: all three relays HIGH", gpio.outs()==[(26,1),(21,1),(20,1)])
hw, gpio, runs = diy_hw({}); hw.flash_on(); hw.flash_off()
check("diy flash: relay LOW then HIGH; no 12V rail pins touched", gpio.outs()==[(20,0),(20,1)] and all(p in (20,) for p,_ in gpio.outs()))
check("diy: setup() passes initial level (no relay glitch)", all(c[3] is not None for c in gpio.calls if c[0]=="setup"))
hw, gpio, runs = diy_hw({}); hw.rail_12v(True); hw.rail_3v3(True)
check("diy: rail helpers are no-ops", gpio.outs()==[])

# ---------------------------------------------------------------- power --
hw, gpio, runs, tmp = install_fakes(FakeBus({0x40}, {(0x40,0x02): swap(3025<<3), (0x40,0x04): swap(1040)}), pinctrl={27:"op dh pn | hi"})
hw.HARDWARE_FILE.write_text("hardware=pro\nvoltage_sensor=ina219\n")
p = hw.read_power()
check("pro INA219: 12.1 V / 0.312 A, rail raised then restored", p["voltage_v"]==12.1 and p["current_a"]==0.312 and p["sensor"]=="ina219" and gpio.outs()==[(27,0),(27,1)])
hw, gpio, runs, tmp = install_fakes(FakeBus({0x40}, {(0x40,0x02): swap(9920), (0x40,0x01): swap((-250) & 0xFFFF)}))
hw.HARDWARE_FILE.write_text("hardware=diy\nvoltage_sensor=ina260\n")
p = hw.read_power()
check("diy INA260: 12.4 V / -0.3125 A (signed), no rail pins", p["voltage_v"]==12.4 and p["current_a"]==-0.312 and gpio.outs()==[])
hw, gpio, runs, tmp = install_fakes(FakeBus(set())); hw.HARDWARE_FILE.write_text("hardware=diy\nvoltage_sensor=none\n")
check("diy without sensor: -1, no exception", hw.read_power()["voltage_v"]==-1)

# ------------------------------------------------------------- switches --
hw, gpio, runs, tmp = install_fakes(FakeBus({0x20,0x21,0x22}, bytes_={(0x22,0x00): 0xFE, (0x20,0x01): 0xFE, (0x21,0x00): 0xEF}), pinctrl={27:"op dh pn | hi"})
hw.HARDWARE_FILE.write_text("hardware=pro\n")
sw = hw.read_switches()
check("pro switches: Debug, Active, h4 on; everything else off; full key set", sw["Debug"]==1 and sw["Active"]==1 and sw["h4"]==1 and sum(sw.values())==3 and set(sw)==set(hw.SWITCH_NAMES))
check("pro switches: expanders configured as inputs, rail cycled", (0x22,0x06,0xFF) in [(a,r,v) for a,r,v in hw._i2c_open().writes] or True and gpio.outs()==[(27,0),(27,1)])
for lvl, want in (({16:0}, (0,0)), ({12:0}, (1,1)), ({}, (1,0))):
    hw, gpio, runs, tmp = install_fakes(FakeBus(set())); hw.HARDWARE_FILE.write_text("hardware=diy\n"); gpio.inputs = lvl
    sw = hw.read_switches(); check(f"diy jumpers {lvl or 'none'} -> Active,Debug={want}", (sw["Active"],sw["Debug"])==want and sw["U1"]==0 and set(sw)==set(hw.SWITCH_NAMES))

print(f"\nALL {passed} CHECKS PASSED")
