#!/usr/bin/python3
"""
display_mock.py -- render the e-paper screens on a computer, no Mothbox needed.

Runs the real display_render.py (the same code the box uses) against a set of
field scenarios and writes, for each one:
    <name>.png      exactly the 250x122 1-bit image sent to the panel
    <name>@4x.png   zoomed 4x with e-paper ink/paper colours, for looking at
plus sheet.png, every scenario side by side.

    python3 tools/display_mock.py                 # render once
    python3 tools/display_mock.py --watch         # re-render on every save
    python3 tools/display_mock.py --out ~/Desktop/eink

Needs Pillow (pip install pillow). With --watch, keep sheet.png open in an
image viewer that reloads on change (VS Code's preview does) and edit
display_render.py: the sheet updates within a second of each save.
"""
import argparse
import importlib
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import display_render                                   # noqa: E402

from PIL import Image, ImageDraw                        # noqa: E402

PAPER, INK = (226, 226, 216), (34, 34, 38)              # what the panel looks like
ZOOM = 4


def _t(y, mo, d, h, mi):
    return int(time.mktime((y, mo, d, h, mi, 0, 0, 0, -1)))


BASE = dict(
    name="nastyMara", mode="ACTIVE", hw="pro", version="6.0.4",
    battery_pct=36, next_wake=_t(2026, 10, 21, 9, 0), updated=_t(2026, 10, 20, 11, 0),
    utc_offset="-5.0", sd_free_gb=105, sd_total_gb=114, sd_photos=238, usb=[],
    weekdays="1;2;3;4;5;6;7", hours="9;11", minutes="0", runtime="59", interval="1",
    lat="n/a", lon="n/a", alerts=[],
)

# Each scenario: (file name, overrides on BASE). Add whatever you want to see.
STATUS_SCENARIOS = [
    ("01_active", {}),
    ("02_standby_camera_missing", dict(
        name="lookXarda", mode="STANDBY", battery_pct=92, next_wake=_t(2026, 10, 23, 19, 0),
        updated=_t(2026, 10, 23, 7, 12), sd_photos=0, interval="2", hours="19;21;23;2;4",
        alerts=["CAMERA DISCONNECTED"])),
    ("03_low_storage_low_battery", dict(
        name="sleepyGecko", battery_pct=14, sd_free_gb=2.6, sd_photos=48210,
        usb=[dict(free_gb=880, total_gb=931, photos=48210)],
        lat="8.99312", lon="-79.51987", hours="19;20;21;22;23;0;1;2;3;4;5")),
    ("04_worst_case_text", dict(
        name="WWWWWWWWWWWW", mode="HI_POW", battery_pct=100, sd_free_gb=1840, sd_photos=123456,
        usb=[dict(free_gb=1.2, total_gb=64, photos=99999), dict(free_gb=10, total_gb=32, photos=1)],
        weekdays="1;3;5;7", hours="18;19;20;21;22;23;0;1;2;3;4;5;6", minutes="15;45",
        runtime="120", interval="10", lat="-33.86882", lon="151.20929",
        alerts=["CAMERA DISCONNECTED", "LOW BATTERY"])),
    ("05_spanish_name", dict(name="ñandúAmarillo", battery_pct=67, weekdays="1;2;3;4;5")),
    ("06_unknown_values", dict(
        name="errorname", mode="ERRORMODE", battery_pct=None, next_wake=-1, utc_offset="erroUTC",
        sd_free_gb=None, sd_photos=None, weekdays="errorweekdays", hours="errorhours",
        minutes="errorminutes", runtime="erroruntime", interval="errInt", version="5")),
]
OTHER_SCREENS = [
    ("07_boot_logo", lambda r: r.render_logo("pro", "6.0.4")),
    ("08_first_boot_wait", lambda r: r.render_wait()),
]


def screens(r):
    """(file name, zero-arg function returning the image) for every screen."""
    for name, over in STATUS_SCENARIOS:
        yield name, (lambda o=over: r.render_status({**BASE, **o}))
    for name, make in OTHER_SCREENS:
        yield name, (lambda m=make: m(r))


def to_paper(img):
    """1-bit panel image -> RGB zoomed up with paper and ink colours."""
    rgb = Image.new("RGB", img.size, PAPER)
    rgb.paste(Image.new("RGB", img.size, INK), mask=img.point(lambda v: 255 - v).convert("L"))
    return rgb.resize((img.width * ZOOM, img.height * ZOOM), Image.NEAREST)


def render_all(out):
    os.makedirs(out, exist_ok=True)
    tiles = []
    for name, make in screens(display_render):
        try:
            img = make()
        except Exception:
            print(f"[mock] {name} FAILED:\n{traceback.format_exc()}")
            continue
        assert img.size == (display_render.WIDTH, display_render.HEIGHT) and img.mode == "1", \
            f"{name}: got {img.size} {img.mode}, the panel takes 250x122 mode '1'"
        img.save(os.path.join(out, f"{name}.png"))
        big = to_paper(img)
        big.save(os.path.join(out, f"{name}@4x.png"))
        tiles.append((name, big))

    if tiles:
        gap, label_h, cols = 16, 18, 2
        tw, th = tiles[0][1].size
        rows = (len(tiles) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * tw + (cols + 1) * gap,
                                  rows * (th + label_h) + (rows + 1) * gap), (90, 90, 96))
        d = ImageDraw.Draw(sheet)
        for i, (name, big) in enumerate(tiles):
            x = gap + (i % cols) * (tw + gap)
            y = gap + (i // cols) * (th + label_h + gap)
            d.text((x, y), name, fill=(240, 240, 240))
            sheet.paste(big, (x, y + label_h))
        sheet.save(os.path.join(out, "sheet.png"))
    print(f"[mock] {len(tiles)} screens -> {out}/sheet.png")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(HERE, "mock_out"))
    ap.add_argument("--watch", action="store_true", help="re-render whenever display_render.py changes")
    a = ap.parse_args()
    out = os.path.expanduser(a.out)
    render_all(out)
    if not a.watch:
        return
    src = display_render.__file__
    last = os.path.getmtime(src)
    print(f"[mock] watching {src} (ctrl-c to stop)")
    while True:
        time.sleep(0.5)
        m = os.path.getmtime(src)
        if m != last:
            last = m
            try:
                importlib.reload(display_render)
                render_all(out)
            except Exception:
                traceback.print_exc()


if __name__ == "__main__":
    main()
