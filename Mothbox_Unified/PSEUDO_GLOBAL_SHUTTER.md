# Pseudo-global shutter (firmware 6.1.4)

The OwlSight is a rolling-shutter camera: it reads its rows out one after another over roughly 0.4 s at full resolution. An insect that moves during that time comes out smeared or bent.

A pseudo-global shutter freezes the insect anyway:

1. The attract lights (Mothbeams) switch off before the sensor starts, so the scene is dark.
2. The exposure is a little longer than the readout, so for a short time every row is exposing at once.
3. The flash fires for about 0.8 ms in the middle of that time.

Every row sees the same 0.8 ms of light, so the photo behaves like a 0.8 ms global-shutter exposure. The code is in `pseudo_global.py` and is used by `TakePhoto.py`.

## Settings (camera_settings.csv, and the camera settings editor page)

| Setting | Default | What it does |
|---|---|---|
| `PseudoGlobalShutter` | `1` | On or off. Turn it off where ambient light is too bright, such as street lights, dusk, or a bright moon on open ground. |
| `PGS_FlashUs` | `800` | Flash pulse in µs (100–5000). The gain rises to keep the brightness. The pulse lengthens only if the gain runs out (16×). |
| `PGS_WindowMs` | `60` | Extra exposure beyond the readout: how long every row is open together. Longer tolerates more timing error but lets in more ambient light. |
| `PGS_AmbientLimit` | `0.25` | If the dark frame before the flash is brighter than this share of a normal photo, the box takes a normal photo instead. |
| `PGS_Sync` | `sof` | How the flash is timed from the camera's frame timestamp: `sof` reads it as the start of readout (the Pi's frame-start time), `soe` as the start of exposure. Logged with every photo. |
| `PGS_UseLightSensor` | `0` | Off by default. `1` lets the Pro's light sensor decide: it is read with the Mothbeams off, and above `PGS_MaxLux` the photo is taken the normal way. |
| `PGS_MaxLux` | `5` | The light sensor limit, in lux. For scale: night is under 1, moonlight about 0.3, a street light 5–20. |

**Mothbox Pro only.** The Pro's flash is a MOSFET on GPIO 19, which switches in microseconds. The DIY's flash is a mechanical relay: it takes 5–15 ms to close and it bounces. A DIY therefore always takes normal photos, and the log says why.

## How a photo works

1. **Lights off, light sensor.** The Mothbeams switch off before the camera starts. If `PGS_UseLightSensor` is on, the light sensor is read now, in the dark, with the same reader Diagnostics logs with (`scripts/readLightSensor.py`). Above `PGS_MaxLux`, the Mothbeams come back on and the photo is a normal one. An unreadable sensor is logged, and the photo goes ahead.
2. **Readout.** The camera starts as before. The firmware reads the frame duration while the camera runs at its fastest, and takes that as the readout time R (an upper bound).
3. **Exposure.** Exposure is R + window (for example 385 + 60 ms). The frame duration is a little more than that.
4. **Ambient check.** One frame is taken in the dark, with no Mothbeams and no flash. It measures the ambient light. If it is too bright, the firmware takes a normal photo.
5. **Flash timing.** The flash fires in the middle of the all-rows-open window of a frame about two frames ahead. The timing comes from the camera's `SensorTimestamp`, read as `PGS_Sync` says (`sof`, the start of readout, by default). The log says which reading was used.
6. **Missed flash.** The firmware checks that both the top and the bottom of the frame got the flash. If they did not, it logs that and takes the photo the normal way, rather than saving a dark or banded one.
7. **Lights back on.** The Mothbeams come back on. The flash GPIO is used through `lgpio` only during the pulse, and is released after. (`pinctrl`, which the rest of the firmware uses, takes tens of ms per call.) The pulse itself runs at real-time priority when that is allowed.

**HDR.** HDR brackets the flash pulse (×1, ×2, ×½, …) rather than the exposure.

## Calibration: two passes

- **Pass 1, the calibration itself (unchanged).** The lights are on continuously, because autofocus needs light. Auto-exposure finds the gain for the usual ~1.1 ms exposure.
- **Pass 2, just before the next pseudo-global photo.** The camera takes one normal frame (lights on, calibrated exposure and gain). The firmware measures its brightness and stores it as the target (`target_luma` in `pgs_state.txt`), then throws the frame away; it is never saved. So every saved photo is taken the same way.
- **Every pseudo-global photo after that:**
  - It starts from the same light: a pulse of P µs lights the frame like a continuous P µs exposure, so gain = calibrated gain × calibrated exposure ÷ P.
  - It measures its own brightness and nudges a stored `correction` toward the target. That also absorbs an LED driver that is slow to turn on, which would make short pulses dimmer than expected.

## EXIF

Pseudo-global photos record `capture.pseudo_global_shutter` in the EXIF UserComment:
- `pulse_us`, and `pulse_measured_us` (the measured pulse length);
- `exposure_us`, `gain` and `sync`;
- `ambient` (the dark frame's brightness).

The EXIF `ExposureTime` is the real sensor exposure, about 0.45 s. The flash pulse is what froze the insect.

A normal photo taken while the mode is on records why instead, as `{"skipped": "…"}`. The reasons are:
- a DIY board;
- the light sensor reading too much light;
- too much ambient light in the dark frame;
- a missed flash;
- a setup problem.

So every saved photo says how it was taken.

## First test on a real Pro: what to check

Nothing here has run on hardware yet. The logic is tested against a simulated rolling-shutter camera (`tests/test_pseudo_global.py`).

1. **Log lines.** Run one photo and read the log:
   - `pseudo-global shutter ON/off: …`
   - `readout … ms, exposure … ms, flash … us, gain …`
   - `pseudo-global frame 0: {…}`

   After a calibration the log shows `pseudo-global reference (calibration pass 2, not saved)` before the photo.
2. **Sync.** Every photo logs `firing … assuming SensorTimestamp = start of readout (sof)`. If photos log `the flash missed the frame`, set `PGS_Sync` to `soe` and try again. If both miss, check that `SensorTimestamp` and `time.monotonic_ns()` share a clock.
3. **Banding.** A bright band across part of the photo means the flash fired while only some rows were exposing. Raise `PGS_WindowMs`.
4. **LED rise time.** The pulse is 0.8 ms. If the flash LEDs' driver ramps up slowly, photos come out dark until the correction catches up. Better: look at the flash with a photodiode and oscilloscope, or a phone in slow-motion video.
5. **Brightness.** Check that it settles within 2–3 photos: watch `correction` in `pgs_state.txt`.
6. **Ambient fallback.** At dusk or under a light, photos should fall back to normal (`too much ambient light`).
7. **Light sensor (if you turn it on).** Check that the logged lux makes sense: near 0 at night with the Mothbeams off. Then set `PGS_MaxLux` from what you see at the site.
8. **Motion.** Compare insects in flight, or a fan blade, with `PseudoGlobalShutter` on and off.
9. **Two fallbacks that point at the setup, not the scene.** Each makes the photo fall back to normal:
   - `exposure did not settle`: the sensor would not run the ~0.45 s exposure (a lower cap in this mode).
   - `flash GPIO unavailable`: `lgpio` could not claim GPIO 19.
10. **Timing and heat.** Each pseudo-global photo takes a few seconds longer: several ~0.45 s frames instead of ~1.1 ms ones. The Mothbeams are off for that time.
