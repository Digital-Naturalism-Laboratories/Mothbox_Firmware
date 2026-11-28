#!/usr/bin/python3
"""
Pseudo-global shutter (pseudo_global.py): the flash must land while EVERY row of the
rolling shutter is exposing, whichever way the camera's SensorTimestamp turns out to
be defined; it must notice when it misses, fall back on bright ambient light, and keep
the photo's brightness where calibration put it.

A fake rolling-shutter camera stands in for the OwlSight: frames on a simulated clock,
each row exposing for E from its own start (row r starts R * r/rows after row 0).

    python3 tests/test_pseudo_global.py
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import pseudo_global as pg

failures = 0
def check(label, got, want):
    global failures
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"     got:  {got!r}\n     want: {want!r}")
        failures += 1


class FakeCamera:
    """Rolling shutter on a simulated clock. SensorTimestamp = start of row 0's exposure
    ('soe') or of its readout ('sof'), as the real camera may do either."""

    def __init__(self, true_sync, readout_us, exposure_us, frame_us, ambient=0.01, settle_frames=0):
        self.true_sync, self.R, self.E, self.F = true_sync, readout_us * 1000, exposure_us * 1000, frame_us * 1000
        self.now = 1_000_000_000_000
        self.t0 = self.now + 50_000_000
        self.next_frame = 0
        self.pulses = []
        self.ambient = ambient
        self.settle_frames = settle_frames

    def clock(self):
        return self.now

    def wait(self, target, clock):
        self.now = max(self.now, target)

    def fire(self, micros):
        self.pulses.append((self.now, self.now + int(micros * 1000)))
        self.now += int(micros * 1000)
        return float(micros)

    def soe(self, n):
        return self.t0 + n * self.F

    def delivered(self, n):
        return self.soe(n) + self.E + self.R           # readout of the last row done

    def capture_request(self, wait=None):
        n = self.next_frame
        while self.delivered(n) < self.now:
            n += 1                                      # frames finished while nobody asked are gone
        self.next_frame = n + 1
        self.now = self.delivered(n)
        return FakeRequest(self, n)

    def row_lit(self, n, frac):
        start = self.soe(n) + frac * self.R
        end = start + self.E
        return any(p0 >= start and p1 <= end for p0, p1 in self.pulses)


class FakeRequest:
    def __init__(self, cam, n):
        self.cam, self.n, self.released = cam, n, False

    def get_metadata(self):
        c = self.cam
        ts = c.soe(self.n) if c.true_sync == "soe" else c.soe(self.n) + c.E
        exposure = c.E // 1000 if self.n >= c.settle_frames else 1119
        return {"SensorTimestamp": ts, "ExposureTime": exposure, "FrameDuration": c.F // 1000}

    def release(self):
        self.released = True


def thirds_of(req):
    c = req.cam
    return [0.45 if c.row_lit(req.n, f) else c.ambient for f in (0.0, 0.5, 1.0)]


READOUT = 385_000            # ~ OwlSight full resolution
timing = pg.plan_timing(READOUT, 800, 60)
check("exposure is longer than the readout by the window", timing["exposure_us"] - READOUT >= 60_000, True)
check("frame duration covers the exposure", timing["frame_us"] > timing["exposure_us"], True)


def run(true_sync, sync="sof", ambient=0.01, settle=0, target=0.4):
    cam = FakeCamera(true_sync, READOUT, timing["exposure_us"], timing["frame_us"], ambient=ambient, settle_frames=settle)
    req, info = pg.capture_one(cam, cam.fire, 800, timing, thirds_of, sync=sync, clock=cam.clock, wait=cam.wait,
                               target=target, log=lambda *_: None)
    return cam, req, info


for true in ("sof", "soe"):
    cam, req, info = run(true, sync=true)
    check(f"[{true}] one flash, whole frame lit", (req is not None, len(cam.pulses), info.get("sync")), (True, 1, true))
    check(f"[{true}] every row was open during the flash", all(cam.row_lit(req.n, f / 10) for f in range(11)), True)

cam, req, info = run("soe", sync="sof")
check("assumed the wrong timing: reported as missed, not saved dark", (req, info.get("reason"), len(cam.pulses)), (None, "missed", 1))

cam, req, info = run("sof", ambient=0.3, target=0.4)
check("too much ambient light: no flash, normal photo instead", (req, info.get("reason"), len(cam.pulses)), (None, "ambient", 0))

cam, req, info = run("sof", settle=3)
check("waits for the long exposure to take effect", (req is not None, req.n >= 3), (True, True))

# --- brightness ---
check("same light: 1119 us x gain 2 -> 800 us pulse at gain 2.8", pg.pulse_and_gain(1119, 2.0, 800), (800, 2.797))
p, g = pg.pulse_and_gain(1119, 15.0, 800, max_gain=16.0)
check("gain capped: the pulse grows instead", (g, p > 800, abs(p * g - 1119 * 15) < 5), (16.0, True, True))
p, g = pg.pulse_and_gain(1119, 1.0, 800, correction=0.3)
check("very bright: shorter pulse rather than gain under 1", (g >= 1.0, p < 800), (True, True))
check("HDR brackets the pulse", pg.hdr_pulses(800, 3), [800, 1600, 400])
check("correction moves toward the target", pg.update_correction(1.0, 0.4, 0.2, 0.01) > 1.0, True)
check("correction ignores a frame the flash barely lit", pg.update_correction(1.3, 0.4, 0.012, 0.01), 1.3)

# --- settings ---
s = {"PseudoGlobalShutter": "0", "PGS_FlashUs": "50", "ExposureTime": 499}
cfg = pg.read_config(s)
check("settings read and removed from the camera controls", (cfg["enabled"], cfg["flash_us"], "PGS_FlashUs" in s, "ExposureTime" in s),
      (False, pg.MIN_FLASH_US, False, True))
check("defaults: on, 800 us, sof, light sensor off", (lambda c: (c["enabled"], c["flash_us"], c["sync"], c["use_light_sensor"], c["max_lux"]))(pg.read_config({})),
      (True, 800, "sof", False, 5.0))
check("an unknown sync falls back to sof", pg.read_config({"PGS_Sync": "auto"})["sync"], "sof")
check("light sensor on with a limit", (lambda c: (c["use_light_sensor"], c["max_lux"]))(pg.read_config({"PGS_UseLightSensor": "1", "PGS_MaxLux": "2.5"})), (True, 2.5))

# --- the light reading kept from calibration ---
check("a recent stored reading is used", pg.stored_lux({"lux": 0.3, "lux_time": 1000.0}, 1000.0 + 1800), 0.3)
check("an hour-old reading is taken again", pg.stored_lux({"lux": 0.3, "lux_time": 1000.0}, 1000.0 + 3601), None)
check("no reading yet", pg.stored_lux({"lux": None, "lux_time": None}, 5000.0), None)
import tempfile
_tmp = os.path.join(tempfile.mkdtemp(), "pgs_state.txt")
pg.save_state(_tmp, {"correction": 1.2, "target_luma": 0.4, "needs_reference": "0", "lux": 0.25, "lux_time": 1234.5})
_st = pg.load_state(_tmp)
check("state file keeps the reading and its time", (_st["lux"], _st["lux_time"], _st["correction"], _st["needs_reference"]), (0.25, 1234.5, 1.2, "0"))

# --- image measurement (needs numpy; the Pi always has it) ---
try:
    import numpy as np
    w, h = 64 * 8, 64 * 6
    yuv = np.zeros((h * 3 // 2, w), dtype=np.uint8)
    yuv[: h // 3] = 255                                  # top third of the Y plane bright
    t = pg.luma_thirds(yuv, width=w, height=h)
    check("YUV420: thirds read from the Y plane", (round(t[0], 2), round(t[2], 2)), (1.0, 0.0))
    rgb = np.full((h, w, 3), 128, dtype=np.uint8)
    check("RGB: mean of the channels", round(pg.luma_thirds(rgb)[1], 2), 0.5)
    check("a band of light is not a lit frame", pg.is_lit(t, 0.0, 0.4), False)
except ImportError:
    print("SKIP numpy not installed here (image measurement)")

if failures:
    print(f"\n{failures} check(s) FAILED")
    sys.exit(1)
print("\nALL CHECKS PASSED")
