#!/usr/bin/python3
"""
pseudo_global.py -- pseudo-global shutter photos (firmware 6.1.4).

The OwlSight is a ROLLING-shutter sensor: each row starts and ends its exposure a
little later than the row above, so an insect that moves while the frame is read
out (~0.4 s at full resolution) is smeared or skewed. A pseudo-global shutter
freezes it anyway:

  1. the attract lights (Mothbeams) go OFF, so the scene is dark;
  2. the exposure is made LONGER than the readout, so for a short while every
     row is exposing at the same time;
  3. the flash fires for ~0.8 ms inside that while.

Every row then sees the same 0.8 ms of light -- the photo is effectively a
0.8 ms global-shutter exposure -- and the dark around it adds (almost) nothing.

Timing, per frame (times relative to the first row starting its exposure):

    row 0      |=========== exposure E ===========|
    last row                      |=========== exposure E ===========|
               0                  R               E                 E+R
                                  [ all rows open ]
                                  ^ flash goes here, in the middle

R is the readout time (top row to bottom row), E the exposure; we need E > R.
The flash is timed from each frame's SensorTimestamp, read as the start of the
first row's READOUT ("sof", the Pi's frame-start time) unless PGS_Sync says it is
the start of its exposure ("soe"). Which one is used is logged with every photo,
and a photo the flash missed (top or bottom not lit) is logged and retaken the
normal way rather than saved dark.

Brightness, in two calibration passes:
  pass 1  calibration as before, lights on continuously (focus needs light);
          auto-exposure finds the gain for a ~1.1 ms continuous exposure.
  pass 2  before the next pseudo-global photo, one normal frame is captured at
          those settings and measured -- NOT saved -- and its brightness becomes
          the target.
A flash pulse of P us gives about the light of a P us continuous exposure, so the
gain is scaled by calib_exposure / P; each pseudo-global photo then measures itself
against the target and nudges a stored correction, which also takes care of LED
drivers that are not quite instant.

Everything that needs a real camera, clock or GPIO is passed in, so the logic
can be tested off the Pi (tests/test_pseudo_global.py).
"""
import os
import time

# ------------------------------------------------------------ settings --
# camera_settings.csv rows (removed from the settings before they reach picamera2)
SETTING_DEFAULTS = {
    "PseudoGlobalShutter": "1",   # 1 on (default), 0 off
    "PGS_FlashUs": "800",         # flash pulse, microseconds
    "PGS_WindowMs": "60",         # extra exposure beyond the readout = time all rows are open
    "PGS_AmbientLimit": "0.25",   # ambient brighter than this share of a flash photo -> normal photo
    "PGS_Sync": "sof",            # sof | soe: how SensorTimestamp is read (logged)
    "PGS_UseLightSensor": "0",    # 1: the Pro's light sensor decides (off by default)
    "PGS_MaxLux": "5",            # with the light sensor on: above this, a normal photo
}
MIN_FLASH_US = 100
MAX_FLASH_US = 5000
SYNCS = ("sof", "soe")


def _float(value, default):
    try:
        v = float(str(value).strip())
        return v if v == v else default          # NaN -> default
    except (TypeError, ValueError):
        return default


def read_config(settings):
    """Pseudo-global settings from a camera_settings dict. Pops them (picamera2 would
    reject unknown controls) and returns a validated dict."""
    raw = {k: settings.pop(k, d) for k, d in SETTING_DEFAULTS.items()}
    sync = str(raw["PGS_Sync"]).strip().lower()
    return {
        "use_light_sensor": str(raw["PGS_UseLightSensor"]).strip().lower() in ("1", "1.0", "true", "on", "yes"),
        "max_lux": max(0.0, _float(raw["PGS_MaxLux"], 5.0)),
        "enabled": str(raw["PseudoGlobalShutter"]).strip().lower() in ("1", "1.0", "true", "on", "yes"),
        "flash_us": int(min(MAX_FLASH_US, max(MIN_FLASH_US, _float(raw["PGS_FlashUs"], 800)))),
        "window_ms": max(10.0, _float(raw["PGS_WindowMs"], 60)),
        "ambient_limit": min(1.0, max(0.0, _float(raw["PGS_AmbientLimit"], 0.25))),
        "sync": sync if sync in SYNCS else "sof",
    }


# ------------------------------------------------------------- timing --
def plan_timing(readout_us, flash_us, window_ms):
    """Exposure and frame duration (us) so every row is open for a window around the flash."""
    window_us = max(window_ms * 1000.0, 4.0 * flash_us)
    exposure_us = int(readout_us + window_us)
    frame_us = exposure_us + 5000        # a little vertical blanking on top
    return {"readout_us": int(readout_us), "window_us": int(window_us),
            "exposure_us": exposure_us, "frame_us": frame_us}


def window_center_ns(ts_ns, exposure_us, readout_us, sync):
    """When to fire (middle of the flash), for a frame whose SensorTimestamp is ts_ns."""
    e, r = exposure_us * 1000, readout_us * 1000
    if sync == "soe":                    # ts = row 0 starts exposing; all open in [ts+R, ts+E]
        return int(ts_ns + (r + e) / 2)
    return int(ts_ns - (e - r) / 2)      # sof: ts = row 0 is read out; all open in [ts-E+R, ts]


# --------------------------------------------------------- brightness --
def pulse_and_gain(calib_exposure_us, calib_gain, flash_us, max_gain=16.0, correction=1.0):
    """
    (pulse_us, gain) giving the light calibration found right, for a pulse of about
    flash_us. A shorter pulse freezes motion better, so the pulse only grows when the
    gain would exceed the sensor's maximum, and shrinks (down to MIN_FLASH_US) rather
    than going below gain 1.
    """
    need = max(1.0, float(calib_exposure_us) * float(calib_gain) * float(correction))   # us x gain
    pulse = float(flash_us)
    gain = need / pulse
    if gain > max_gain:
        pulse = min(MAX_FLASH_US, need / max_gain)
        gain = min(max_gain, need / pulse)
    elif gain < 1.0:
        pulse = max(MIN_FLASH_US, need)
        gain = max(1.0, need / pulse)
    return int(round(pulse)), round(gain, 3)


def hdr_pulses(pulse_us, count):
    """Bracketed flash pulses for HDR: the middle one, then longer and shorter."""
    factors = [1, 2, 0.5, 4, 0.25, 8, 0.125]
    out = []
    for f in factors[:max(1, count)]:
        out.append(int(min(MAX_FLASH_US, max(MIN_FLASH_US, pulse_us * f))))
    return out


def luma_thirds(array, width=None, height=None, step=64):
    """
    Mean brightness (0..1) of the top, middle and bottom thirds of an image, from a
    sparse grid of pixels (cheap even at 64 MP). Takes a YUV420 buffer (2-D: the Y
    plane is the first `height` rows, up to `width` columns) or an H x W x C array.
    """
    import numpy as np
    a = np.asarray(array)
    if a.ndim == 2:                       # YUV420: Y plane first
        h = height or (a.shape[0] * 2 // 3)
        w = width or a.shape[1]
        y = a[:h, :w]
    else:
        y = a[..., :3].mean(axis=2) if a.shape[2] >= 3 else a[..., 0]
    h = y.shape[0]
    thirds = []
    for i in range(3):
        part = y[i * h // 3:(i + 1) * h // 3:step, ::step]
        thirds.append(float(part.mean()) / 255.0 if part.size else 0.0)
    return thirds


def is_lit(thirds, ambient, target=None):
    """Did the flash light the WHOLE frame? Top and bottom must both rise above the dark
    frame -- a flash that missed the all-rows window lights only a band."""
    target_rise = max(0.0, (target or 0.0) - ambient)
    need = max(0.015, 0.25 * target_rise)
    return min(thirds[0], thirds[2]) - ambient >= need


def ambient_too_bright(ambient, target, limit):
    """Would ambient light (through the whole long exposure) swamp the flash?"""
    if target and target > 0:
        return ambient > limit * target
    return ambient > 0.08                 # no target yet: a clearly lit dark frame


def update_correction(correction, target, measured, ambient):
    """Nudge the stored brightness correction so the next photo lands nearer the target."""
    flash_part = measured - ambient
    if not target or flash_part <= 0.005:
        return correction
    ratio = min(2.0, max(0.5, (target - ambient) / flash_part))
    return round(min(4.0, max(0.25, correction * ratio ** 0.7)), 4)


# --------------------------------------------------------------- state --
def load_state(path):
    """
    pgs_state.txt: correction (brightness factor learned from photos), target_luma
    (the brightness of a normal frame after the last calibration) and needs_reference
    (1 = measure a new target before the next pseudo-global photo; set by calibration).
    """
    state = {"correction": 1.0, "target_luma": None, "needs_reference": "1"}
    try:
        with open(path) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    state[k.strip()] = v.strip()
    except OSError:
        pass
    state["correction"] = _float(state.get("correction"), 1.0)
    t = _float(state.get("target_luma"), -1)
    state["target_luma"] = t if 0 < t <= 1 else None
    state.pop("sync", None)               # 6.1.4 test builds stored a detected sync; no longer used
    return state


def save_state(path, state):
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            for k, v in state.items():
                if v is not None:
                    f.write(f"{k}={v}\n")
        os.replace(tmp, path)
    except OSError as e:
        print(f"[pseudo_global] could not save {path}: {e}")


# --------------------------------------------------------- clocks/wait --
def pick_clock(ts_ns):
    """The clock SensorTimestamp is on (CLOCK_MONOTONIC or CLOCK_BOOTTIME), or None."""
    candidates = [time.monotonic_ns]
    if hasattr(time, "CLOCK_BOOTTIME"):
        candidates.append(lambda: time.clock_gettime_ns(time.CLOCK_BOOTTIME))
    best, best_gap = None, None
    for clock in candidates:
        gap = abs(clock() - ts_ns)
        if best_gap is None or gap < best_gap:
            best, best_gap = clock, gap
    return best if best_gap is not None and best_gap < 5e9 else None


def wait_until(target_ns, clock):
    """Sleep most of the way, then spin for the last few ms (sleep alone overshoots)."""
    while True:
        left = target_ns - clock()
        if left <= 0:
            return
        if left > 3_000_000:
            time.sleep((left - 2_000_000) / 1e9)


# ---------------------------------------------------------------- flash --
class FlashPulser:
    """
    Fires the Pro's flash MOSFET (GPIO 19, active HIGH) for a precise number of
    microseconds, through lgpio, holding the line only while TakePhoto runs.
    (mothbox_hw drives pins with `pinctrl`, which takes tens of ms per call --
    fine for switching lights, far too slow and jittery for a 0.8 ms pulse.)
    """

    def __init__(self, pin):
        self.pin = pin
        self.handle = None
        self.lgpio = None

    def open(self):
        import lgpio
        self.lgpio = lgpio
        last_error = None
        for chip in (0, 4):               # Pi 5 header: gpiochip0 on new kernels, 4 on old
            try:
                h = lgpio.gpiochip_open(chip)
            except Exception as e:        # noqa: BLE001
                last_error = e
                continue
            try:
                label = str(lgpio.gpio_get_chip_info(h)[2]).lower()
                if "rp1" in label or chip == 4:
                    lgpio.gpio_claim_output(h, self.pin, 0)
                    self.handle = h
                    return self
            except Exception as e:        # noqa: BLE001
                last_error = e
            lgpio.gpiochip_close(h)
        raise RuntimeError(f"no usable gpiochip for GPIO {self.pin}: {last_error}")

    def pulse(self, micros):
        """Flash on for `micros` us. Returns how long it really was (us)."""
        lg, h, pin = self.lgpio, self.handle, self.pin
        ns = int(micros * 1000)
        t0 = time.perf_counter_ns()
        lg.gpio_write(h, pin, 1)
        while time.perf_counter_ns() - t0 < ns:
            pass
        lg.gpio_write(h, pin, 0)
        return (time.perf_counter_ns() - t0) / 1000.0

    def close(self):
        if self.handle is None:
            return
        try:
            self.lgpio.gpio_write(self.handle, self.pin, 0)
            self.lgpio.gpio_free(self.handle, self.pin)
        finally:
            self.lgpio.gpiochip_close(self.handle)
            self.handle = None


def realtime_priority(on):
    """Best effort: run the pulse at real-time priority so the scheduler cannot stretch it."""
    try:
        if on:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(50))
        else:
            os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
        return True
    except (AttributeError, OSError, PermissionError):
        return False


# -------------------------------------------------------------- capture --
def capture_one(picam2, fire, pulse_us, timing, luma_of, sync="sof", clock=None, wait=wait_until,
                target=None, ambient_limit=0.25, timeout_s=20, log=print):
    """
    One pseudo-global frame. The camera must already be running with the exposure and
    frame duration from plan_timing() and the attract lights off.

      picam2.capture_request(wait=...) -> request with get_metadata() / release()
      fire(pulse_us)   fires the flash
      luma_of(request) -> luma_thirds(...) of its image
      sync             how SensorTimestamp is read: "sof" (start of readout) or "soe"

    Returns (request, info) with the request NOT released (the caller saves it), or
    (None, info) when it could not: info["reason"] is "ambient", "missed", "clock" or
    "exposure did not settle".
    """
    info = {"pulse_us": pulse_us, "exposure_us": timing["exposure_us"], "sync": sync}
    want_e = timing["exposure_us"]

    # The frame before the flash: dark, so it measures the ambient light. Wait (a few
    # frames) for the long exposure to take effect.
    req = None
    for _ in range(8):
        req = picam2.capture_request(wait=timeout_s)
        md = req.get_metadata()
        if md.get("ExposureTime", 0) >= 0.9 * want_e:
            break
        req.release()
        req = None
    if req is None:
        return None, {**info, "reason": "exposure did not settle"}
    md = req.get_metadata()
    ts_k = int(md["SensorTimestamp"])
    frame_ns = int(md.get("FrameDuration", timing["frame_us"]) * 1000)
    ambient = sum(luma_of(req)) / 3.0
    req.release()
    info["ambient"] = round(ambient, 4)

    if ambient_too_bright(ambient, target, ambient_limit):
        return None, {**info, "reason": "ambient"}

    clock = clock or pick_clock(ts_k)
    if clock is None:
        return None, {**info, "reason": "clock"}

    # Pick a frame far enough ahead to fire in time, and fire in the middle of its
    # all-rows-open window.
    j = 1
    while True:
        ts_m = ts_k + j * frame_ns
        center = window_center_ns(ts_m, timing["exposure_us"], timing["readout_us"], sync)
        if center - clock() > 15_000_000:
            break
        j += 1
    log(f"[pseudo_global] firing {pulse_us} us assuming SensorTimestamp = "
        f"{'start of readout (sof)' if sync == 'sof' else 'start of exposure (soe)'}")
    wait(center - pulse_us * 500, clock)
    info["pulse_measured_us"] = round(fire(pulse_us), 1)

    # Collect that frame (skip earlier ones).
    got = None
    for _ in range(6):
        r = picam2.capture_request(wait=timeout_s)
        ts = int(r.get_metadata()["SensorTimestamp"])
        if ts < ts_m - frame_ns // 2:
            r.release()
            continue
        if ts <= ts_m + frame_ns // 2:
            got = r
        else:
            r.release()
        break
    if got is None:
        return None, {**info, "reason": "missed"}
    thirds = luma_of(got)
    info["thirds"] = [round(t, 4) for t in thirds]
    if not is_lit(thirds, ambient, target):
        log(f"[pseudo_global] the flash missed the frame (top/mid/bottom "
            f"{[round(t, 3) for t in thirds]}, dark {ambient:.3f}) -- check PGS_Sync / PGS_WindowMs")
        got.release()
        return None, {**info, "reason": "missed"}
    return got, info
