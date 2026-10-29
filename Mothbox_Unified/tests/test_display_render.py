#!/usr/bin/python3
"""
The e-paper screens must always render (whatever junk is in the control
files), fit the panel, and flag the field-critical states. UpdateDisplay must
draw before it touches the panel, so a drawing bug leaves the old screen up.

    python3 tests/test_display_render.py
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import display_render as r
import display_mock

failures = 0
def check(label, got, want):
    global failures
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"     got:  {got!r}\n     want: {want!r}")
        failures += 1

def status(**over):
    return r.render_status({**display_mock.BASE, **over})

def black(img, x, y):
    return img.getpixel((x, y)) == 0

# --- every mock scenario renders to exactly what the panel takes ----------
for name, make in display_mock.screens(r):
    try:
        img = make()
        check(f"{name}: 250x122 1-bit", (img.size, img.mode), ((250, 122), "1"))
    except Exception as e:
        check(f"{name}: renders", repr(e), "no exception")

check("an empty info dict still renders", r.render_status({}).size, (250, 122))

# --- field-critical states ------------------------------------------------
check("low storage inverts the SD block", black(status(sd_free_gb=3.9), 60, 72), True)
check("4 GB free is not low", black(status(sd_free_gb=4), 60, 72), False)
check("low battery inverts the battery block", black(status(battery_pct=20), 248, 2), True)
check("21% battery is not low", black(status(battery_pct=21), 248, 2), False)
check("an alert turns the footer into a black bar",
      black(status(alerts=["CAMERA DISCONNECTED"]), 248, 119), True)
check("no alert: white footer", black(status(), 248, 119), False)

# --- the widest real auto-names fit beside "100%" without an ellipsis ------
# (found by measuring every combination wordlist.csv can generate)
batt100 = 26 + r.text_w(r.font("big"), "100%") + 1 + 3
room = r.WIDTH - batt100 - 8 - 1
for n in ("commonMammal", "commonWombat", "momotoNombre"):
    check(f"auto-name {n!r} fits the header", r.text_w(r.font("name"), n) <= room, True)

# --- formatting -----------------------------------------------------------
check("night order", r.sort_hours_night_order("2;4;19;21;23"), "19;21;23;2;4")
check("night order keeps junk", r.sort_hours_night_order("errorhours"), "errorhours")
check("hours", r.format_hours("19;21;23;2;4"), "19 21 23 2 4")
check("hours with start minutes", r.format_hours("19;21", "15;45"), "19 21 at :15 :45")
check("long hour lists collapse to ranges",
      r.format_hours("18;19;20;21;22;23;0;1;2;3;4;5;6", "0", r.font("value_s"), 100), "18-6")
check("short runs stay listed", r.format_hours("19;20;2", "0", r.font("value_s"), 1), "19 20 2")
check("every day", r.format_days("1;2;3;4;5;6;7"), "every day")
check("weekdays", r.format_days("1,2,3,4,5"), "Mon-Fri")
check("some days", r.format_days("1;3;7"), "Mo We Su")
check("junk days", r.format_days("errorweekdays"), "?")
check("unset wake", r.format_when(-1), None)
check("junk wake", r.format_when("soon"), None)
check("counts", [r.format_count(n) for n in (0, 238, 48210, 99999, 123456, None)],
      ["0", "238", "48.2k", "100k", "123k", "?"])
check("GB", [r.format_gb(g) for g in (2.64, 105.4, None)], ["2.6", "105", "?"])
check("GPS", (r.format_gps("8.99312", "-79.51987"), r.format_gps("n/a", "n/a")),
      ("8.993,-79.520", None))
check("fit adds an ellipsis", r.fit(r.font("text"), "x" * 100, 30).endswith("…"), True)

# --- a broken FreeType must not blank the screen --------------------------
# Pretend _imagingft is missing (Sept 2026 apt upgrade): truetype() raises and,
# on Pillow >= 10.1, load_default() only avoids FreeType when `core` isn't the
# real module.
real_tt, real_core = r.ImageFont.truetype, r.ImageFont.core
try:
    def broken(*a, **k):
        raise OSError("The _imagingft C module is not installed")
    r.ImageFont.truetype = broken
    r.ImageFont.core = object()
    r._font_cache.clear()
    check("renders with the built-in fallback font", status(alerts=["X"]).size, (250, 122))
finally:
    r.ImageFont.truetype, r.ImageFont.core = real_tt, real_core
    r._font_cache.clear()

# --- UpdateDisplay draws first, then talks to the panel -------------------
src = open(os.path.join(ROOT, "UpdateDisplay.py")).read()
main = src[src.index("# ------------- Drawing"):]
check("status is rendered before the panel is opened",
      0 <= main.find("render_status(") < main.find("EPD()"), True)
check("UpdateDisplay has no drawing code of its own left",
      ("load_font(" in src.split("'''")[0], "ImageDraw" in src.split("'''")[0]), (False, False))

if failures:
    print(f"\n{failures} check(s) FAILED")
    sys.exit(1)
print("\nALL CHECKS PASSED")
