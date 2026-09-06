"""Ambient light reading with auto-detection:
  1) Try LTR-F216A (new boards, addr 0x53)
  2) Fall back to LTR-303 (old boards, addr 0x29)
  3) No sensor -> warn and exit cleanly (no crash)

read_lux() always returns (lux, ch0, ch1) so existing code keeps working.
On the F216A, ch0 = raw ALS counts and ch1 = None (single-channel sensor,
IR rejection is done on-chip).
"""

import sys
import time

try:
    from smbus2 import SMBus
except ImportError:
    from smbus import SMBus  # fall back to python-smbus


# ---------------------------------------------------------------- LTR-F216A --
class LTRF216A:
    ADDR = 0x53

    # Registers
    MAIN_CTRL     = 0x00
    ALS_MEAS_RATE = 0x04
    ALS_GAIN      = 0x05
    PART_ID       = 0x06
    MAIN_STATUS   = 0x07
    ALS_DATA_0    = 0x0D  # LSB; 0x0E mid; 0x0F holds bits 19:16

    PART_ID_VALUE = 0xB1  # verified on Mothbox new-revision PCB

    def __init__(self, bus):
        self.bus = bus
        self.gain = 18       # 1, 3, 6, 9, 18
        self.int_100ms = 4   # integration time in units of 100 ms

    def probe(self):
        """Return True if a genuine LTR-F216A answers at 0x53."""
        try:
            part_id = self.bus.read_byte_data(self.ADDR, self.PART_ID)
        except OSError:
            return False
        return part_id == self.PART_ID_VALUE

    def begin(self):
        # Max sensitivity to mirror the old gain=96 / 400 ms LTR-303 setup:
        # gain 18, 20-bit conversion (400 ms), 500 ms measurement rate.
        self.bus.write_byte_data(self.ADDR, self.MAIN_CTRL, 0x02)      # ALS enable
        self.bus.write_byte_data(self.ADDR, self.ALS_MEAS_RATE, 0x04)  # 20-bit / 500 ms
        self.bus.write_byte_data(self.ADDR, self.ALS_GAIN, 0x04)       # gain = 18

    def read_lux(self):
        # Burst-read the 3 data bytes so the chip keeps them coherent.
        b = self.bus.read_i2c_block_data(self.ADDR, self.ALS_DATA_0, 3)
        raw = ((b[2] & 0x0F) << 16) | (b[1] << 8) | b[0]
        # Datasheet: lux = 0.45 * ALS_DATA / (gain * integration_time[100ms])
        lux = 0.45 * raw / (self.gain * self.int_100ms)
        return lux, raw, None


# --------------------------------------------------------------- LTR-303 IDs --
LTR303_ADDR           = 0x29
LTR303_PART_ID_REG    = 0x86
LTR303_PART_ID_VALUE  = 0xA0
LTR303_MANUF_ID_REG   = 0x87
LTR303_MANUF_ID_VALUE = 0x05


# ------------------------------------------------------------- autodetection --
def find_sensor(bus_num=1):
    """Return (name, sensor) where sensor.read_lux() -> (lux, ch0, ch1),
    or (None, None) if nothing usable is on the bus."""
    try:
        bus = SMBus(bus_num)
    except (OSError, FileNotFoundError) as e:
        print(f"WARNING: could not open I2C bus {bus_num}: {e}", file=sys.stderr)
        return None, None

    # 1) New sensor first
    f216a = LTRF216A(bus)
    try:
        if f216a.probe():
            f216a.begin()
            return "LTR-F216A", f216a
    except OSError as e:
        print(f"WARNING: LTR-F216A detected but init failed: {e}", file=sys.stderr)

    # 2) Legacy LTR-303 -- confirm the IDs on the already-open bus before
    #    handing off to the library, so another device at 0x29 can't fool us.
    try:
        part  = bus.read_byte_data(LTR303_ADDR, LTR303_PART_ID_REG)
        manuf = bus.read_byte_data(LTR303_ADDR, LTR303_MANUF_ID_REG)
        if part == LTR303_PART_ID_VALUE and manuf == LTR303_MANUF_ID_VALUE:
            from ltr303 import LTR303
            bus.close()  # hand the bus over to the library
            sensor = LTR303(bus_num=bus_num)
            sensor.begin(gain=96, integration_time=400)
            return "LTR-303", sensor
        print(f"WARNING: device at 0x29 is not an LTR-303 "
              f"(part=0x{part:02X}, manuf=0x{manuf:02X})", file=sys.stderr)
    except OSError:
        pass  # nothing at 0x29 -- normal on new hardware
    except ImportError as e:
        print(f"WARNING: LTR-303 found but library missing: {e}", file=sys.stderr)

    bus.close()
    return None, None


# --------------------------------------------------------------------- main --
if __name__ == "__main__":
    name, sensor = find_sensor(bus_num=1)

    if sensor is None:
        print("No ambient light sensor found - continuing without light data.")
        sys.exit(0)  # graceful: exit code 0, or carry on with a default value

    print(f"Using sensor: {name}")
    time.sleep(0.5)  # give it time to finish the first conversion

    lux, ch0, ch1 = sensor.read_lux()

    if ch1 is None:
        # F216A: single channel, IR rejection done on-chip
        print(f"Lux: {lux:.5f} , Raw ALS counts={ch0}")
    else:
        # LTR-303: CH0 = visible + IR, CH1 = IR only
        print(f"Lux: {lux:.5f} , Channels: CH0(V+IR)={ch0}, CH1(IR)={ch1}")
