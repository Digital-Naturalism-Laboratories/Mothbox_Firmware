#!/usr/bin/python3
"""
mothbox_exif.py -- build a correct, information-rich EXIF block for Mothbox photos.

Used by TakePhoto.py. Everything here is best-effort and never raises out of
build_exif_bytes(): a photo must never be lost because a tag could not be
written. Values come from three places:

  1. The libcamera request metadata for the frame that was actually captured
     (real ExposureTime / AnalogueGain / LensPosition, not what was asked for).
  2. Static facts about the camera module, keyed by the sensor name libcamera
     reports, so a different camera gets no tag rather than a wrong one.
  3. The most recent Mothbox diagnostics run, read from the tail of the
     existing Diagnostics*.log files. Nothing is measured at photo time.

Standard tags are used wherever one exists (EXIF 2.31 'Temperature' for the
board thermometer, GPS IFD for position, SubjectDistance from the lens
diopter). Everything else, plus the raw numbers, goes into UserComment as a
compact JSON object so downstream tools have one machine-readable place to
look.
"""
import glob
import json
import os
import re
from datetime import datetime

import piexif
import piexif.helper

LOG_DIR = "/home/pi/Desktop/Mothbox/logs"
TAIL_BYTES = 16 * 1024  # a diagnostics run is ~600 bytes; this covers many

# Camera module facts, keyed by the sensor name from picam2.camera_properties['Model'].
# Only put in numbers you can cite. Unknown sensors get no lens tags at all.
CAMERA_MODULES = {
    "ov64a40": {  # Arducam 64MP OwlSight, module B0483 (docs.arducam.com)
        "model":    "Arducam 64MP OwlSight (OV64A40)",
        "lens":     "Arducam B0483 6.65mm F1.9",
        "focal_mm": 6.65,
        "fnumber":  1.9,
    },
}


# --------------------------------------------------------------------------
# Diagnostics log parsing
# --------------------------------------------------------------------------
_RUN_HEADER = re.compile(r"^Current time: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$", re.M)
_PATTERNS = {
    # key            regex                                                  cast
    "label":        (re.compile(r"^Run label: (.+?)\s*$", re.M),            str),
    "cpu_temp_c":   (re.compile(r"^cputemp: ([-\d.]+)", re.M),              float),
    "board_temp_c": (re.compile(r"^Temperature: ([-\d.]+)\s*°?C", re.M),   float),
    "lux":          (re.compile(r"^Lux: ([-\d.eE+]+)", re.M),               float),
    "light_sensor": (re.compile(r"^Using sensor: (.+?)\s*$", re.M),         str),
    # Pro read_Vin.py:        "Vin Voltage: 12.401 V, Current: 0.312 A"
    # DIY mbDIYINA_voltage.py: "Current: 312.00 mA  Voltage: 12.40 V  Power: 3869.00 mW"
    "vin_v":        (re.compile(r"Voltage: ([-\d.]+) V\b", re.M),           float),
    "current_a":    (re.compile(r"Current: ([-\d.]+) A\b", re.M),           float),
    "current_ma":   (re.compile(r"Current: ([-\d.]+) mA\b", re.M),          float),
    "pi_5v":        (re.compile(r"^5v to pi: ([-\d.]+)", re.M),             float),
}


def _parse_last_run(text):
    """Return (datetime, dict) for the last diagnostics run in text, or None."""
    headers = list(_RUN_HEADER.finditer(text))
    if not headers:
        return None
    block = text[headers[-1].start():]
    try:
        when = datetime.strptime(headers[-1].group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    out = {}
    for key, (rx, cast) in _PATTERNS.items():
        m = rx.search(block)
        if not m:
            continue
        try:
            out[key] = cast(m.group(1))
        except ValueError:
            pass
    if "current_ma" in out:
        out.setdefault("current_a", round(out["current_ma"] / 1000.0, 4))
        del out["current_ma"]
    return when, out


def read_latest_diagnostics(log_dir=LOG_DIR, now=None):
    """
    Most recent sensor readings already logged by Diagnostics.py, without
    running anything. Reads only the tail of each Diagnostics*.log and picks
    the newest run across them. Returns {} if nothing usable is found.
    Keys: time (ISO), age_s, and any of cpu_temp_c, board_temp_c, lux,
    light_sensor, vin_v, current_a, pi_5v, label.
    """
    best = None
    try:
        for path in glob.glob(os.path.join(log_dir, "Diagnostics*.log")):
            try:
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - TAIL_BYTES))
                    text = f.read().decode("utf-8", errors="replace")
                parsed = _parse_last_run(text)
                if parsed and (best is None or parsed[0] > best[0]):
                    best = parsed
            except OSError:
                continue
    except Exception:
        return {}
    if best is None:
        return {}
    when, values = best
    now = now or datetime.now()
    result = {"time": when.strftime("%Y-%m-%dT%H:%M:%S"),
              "age_s": int(max(0, (now - when).total_seconds()))}
    result.update(values)
    return result


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _ascii(s, limit=200):
    return str(s).encode("ascii", "replace").decode("ascii")[:limit]


def _rational(x, den):
    return (int(round(float(x) * den)), den)


def _dms(deg):
    """Decimal degrees -> ((d,1),(m,1),(s*10000,10000)) for the GPS IFD."""
    deg = abs(float(deg))
    d = int(deg)
    m_full = (deg - d) * 60
    m = int(m_full)
    s = (m_full - m) * 60
    return ((d, 1), (m, 1), (int(round(s * 10000)), 10000))


def _offset_str(utc_offset_hours):
    """+HH:MM form required by OffsetTime* tags."""
    total = int(round(float(utc_offset_hours) * 60))
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 60:02d}:{total % 60:02d}"


def _pi_serial():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Serial"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def _r(v, nd=4):
    """Round floats for the JSON blob; leave ints, strings and None alone."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    return v if isinstance(v, int) else round(v, nd)


# --------------------------------------------------------------------------
# The EXIF builder
# --------------------------------------------------------------------------
def build_exif_dict(*, metadata, requested_exposure_us, image_size, capture_time,
                    utc_offset_hours, computer_name, software_version, sensor_name,
                    lat=None, lon=None, diagnostics=None, hdr_index=0, hdr_count=1,
                    image_id=None, flash_fired=True, body_model=None):
    """
    Assemble the piexif dict. Pure function of its inputs (plus the Pi serial),
    so it can be unit-tested off the Pi. See build_exif_bytes() for the
    fail-safe wrapper TakePhoto.py should call.
    """
    md = metadata or {}
    diagnostics = diagnostics or {}
    cam = CAMERA_MODULES.get(str(sensor_name).lower(), {})
    sw = str(software_version)
    # The caller says which body this is (TakePhoto.py in the Pro and DIY
    # folders differ on exactly this). Both run Pi 5 + OwlSight and report
    # 5.x firmware, so it cannot be inferred from anything else.
    body = str(body_model) if body_model else "Mothbox"
    local_ts = capture_time.strftime("%Y:%m:%d %H:%M:%S")
    offset = _offset_str(utc_offset_hours)
    w, h = image_size

    actual_exp_us = md.get("ExposureTime")
    if not actual_exp_us:
        actual_exp_us = abs(int(requested_exposure_us or 0)) or None
    a_gain = float(md.get("AnalogueGain", 1.0) or 1.0)
    d_gain = float(md.get("DigitalGain", 1.0) or 1.0)
    lens_pos = md.get("LensPosition")

    hdr_note = f" HDR frame {hdr_index + 1}/{hdr_count}" if hdr_count > 1 else ""

    zeroth = {
        piexif.ImageIFD.Make:             "Mothbox",
        piexif.ImageIFD.Model:            _ascii(f"{body} + {cam.get('model', sensor_name or 'unknown camera')}"),
        piexif.ImageIFD.Software:         _ascii(f"Mothbox firmware {sw}"),
        piexif.ImageIFD.DateTime:         local_ts,
        piexif.ImageIFD.ImageDescription: _ascii(f"{computer_name}{hdr_note}"),
    }

    exif = {
        piexif.ExifIFD.DateTimeOriginal:   local_ts,
        piexif.ExifIFD.DateTimeDigitized:  local_ts,
        piexif.ExifIFD.OffsetTime:         offset,
        piexif.ExifIFD.OffsetTimeOriginal: offset,
        piexif.ExifIFD.OffsetTimeDigitized: offset,
        piexif.ExifIFD.ExposureProgram:    1,            # manual
        piexif.ExifIFD.ExposureMode:       1,            # manual
        piexif.ExifIFD.WhiteBalance:       1,            # manual (locked ColourGains)
        piexif.ExifIFD.ColorSpace:         1,            # sRGB
        piexif.ExifIFD.PixelXDimension:    int(w),
        piexif.ExifIFD.PixelYDimension:    int(h),
        piexif.ExifIFD.ISOSpeedRatings:    max(1, min(65535, int(round(a_gain * d_gain * 100)))),
        piexif.ExifIFD.Flash:              0x09 if flash_fired else 0x10,  # fired+compulsory / suppressed
        piexif.ExifIFD.LightSource:        4 if flash_fired else 0,        # 4 = flash
    }
    if actual_exp_us:
        exif[piexif.ExifIFD.ExposureTime] = (int(actual_exp_us), 1000000)
    if cam.get("focal_mm"):
        exif[piexif.ExifIFD.FocalLength] = _rational(cam["focal_mm"], 100)
    if cam.get("fnumber"):
        exif[piexif.ExifIFD.FNumber] = _rational(cam["fnumber"], 10)
    if cam.get("lens"):
        exif[piexif.ExifIFD.LensMake] = "Arducam"
        exif[piexif.ExifIFD.LensModel] = cam["lens"]
    if lens_pos is not None:
        try:
            lp = float(lens_pos)
            if lp > 0:  # diopters -> metres
                exif[piexif.ExifIFD.SubjectDistance] = _rational(1.0 / lp, 10000)
        except (TypeError, ValueError):
            pass
    serial = _pi_serial()
    if serial:
        exif[piexif.ExifIFD.BodySerialNumber] = _ascii(serial)
    if image_id:
        exif[piexif.ExifIFD.ImageUniqueID] = _ascii(image_id, 64)
    if "board_temp_c" in diagnostics:        # EXIF 2.31 ambient temperature, degC
        t = _rational(diagnostics["board_temp_c"], 100)
        exif[piexif.ExifIFD.Temperature] = t

    comment = {
        "mothbox": _ascii(computer_name),
        "firmware": sw,
        "capture": {
            "exposure_us": actual_exp_us,
            "exposure_us_requested": requested_exposure_us,
            "analogue_gain": _r(a_gain),
            "digital_gain": _r(d_gain),
            "lens_position_diopters": _r(lens_pos),
            "colour_temperature_k": md.get("ColourTemperature"),
            "camera_lux_estimate": _r(md.get("Lux")),
            "sensor_temperature_c": md.get("SensorTemperature"),
            "frame_duration_us": md.get("FrameDuration"),
            "hdr_index": hdr_index, "hdr_count": hdr_count,
            "flash": bool(flash_fired),
        },
        "camera": {"sensor": str(sensor_name), **{k: v for k, v in cam.items() if k != "model"}},
        "diagnostics": {k: _r(v) for k, v in diagnostics.items()},
    }
    if lat is not None and lon is not None:
        comment["gps"] = {"lat": _r(lat, 6), "lon": _r(lon, 6)}
    exif[piexif.ExifIFD.UserComment] = piexif.helper.UserComment.dump(
        json.dumps(comment, separators=(",", ":"), ensure_ascii=True, default=str), encoding="ascii")

    gps = {}
    try:
        if lat is not None and lon is not None:
            flat, flon = float(lat), float(lon)
            if -90 <= flat <= 90 and -180 <= flon <= 180 and not (flat == 0 and flon == 0):
                gps = {
                    piexif.GPSIFD.GPSVersionID:   (2, 3, 0, 0),
                    piexif.GPSIFD.GPSLatitudeRef:  "N" if flat >= 0 else "S",
                    piexif.GPSIFD.GPSLatitude:     _dms(flat),
                    piexif.GPSIFD.GPSLongitudeRef: "E" if flon >= 0 else "W",
                    piexif.GPSIFD.GPSLongitude:    _dms(flon),
                }
    except (TypeError, ValueError):
        gps = {}

    return {"0th": zeroth, "Exif": exif, "GPS": gps, "1st": {}}


def build_exif_bytes(**kwargs):
    """
    Fail-safe wrapper for TakePhoto.py. Returns EXIF bytes, or None if anything
    at all went wrong -- the caller then saves the photo without EXIF rather
    than losing it.
    """
    try:
        return piexif.dump(build_exif_dict(**kwargs))
    except Exception as e:
        print(f"[mothbox_exif] could not build EXIF ({e!r}); saving photo without it")
        return None
