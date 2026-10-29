#!/usr/bin/python3
"""
display_render.py -- draws every screen the Waveshare 2.13in e-paper shows.

Pure drawing: each render_*() takes plain values and returns a 250x122 1-bit
PIL image. Nothing here touches the panel, GPIO or the control files, so the
exact same code runs on a Mac (tools/display_mock.py) to iterate on the design
without a box. UpdateDisplay.py gathers the values and sends the image.

Fonts
-----
The panel is 1-bit, so text is drawn without anti-aliasing (fontmode "1").
Outline fonts only rasterise cleanly at large sizes; below ~16 px we use pixel
fonts at their NATIVE sizes, where every glyph is hand-placed on the grid:
    Galmuri11 Bold @ 12 px (OFL) -- the headline values (dates, GB free).
    Galmuri7 @ 8 px (OFL)        -- the small caps labels.
    Terminus @ 12 px (OFL)       -- schedule rows and notes. Monospaced, with
                                    0/O and 1/l/I all distinct.
    Galmuri9 @ 20 px (OFL)       -- the name and battery figure: exactly 2x its
                                    native 10 px, drawn with a 1 px faux bold
                                    (bold_text) so it outweighs the values.
(Galmuri11 REGULAR is thin and oversized at 12 px -- don't use it. The Galmuri
files are subset to Latin; the originals carry 16 MB of Hangul.)
Drawing a pixel font at any other size smears it (scientifica at 13 px was the
blobby "RUN TIME" text on the 6.0.2 screen), so the sizes below are fixed.
"""
import os
import time

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 250, 122            # landscape; the panel itself is 122x250
HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "graphics", "fonts")
LOGO_PATH = os.path.join(HERE, "graphics", "display", "mothbox_logo_bw.png")

BLACK, WHITE = 0, 255
LOW_STORAGE_GB = 4                  # below this the storage block inverts
LOW_BATTERY_PCT = 20                # at or below this the battery block inverts
NAME_MAX_CHARS = 12                 # auto-names never exceed this

# (file, size) -- sizes are the fonts' native pixel sizes, see above
FONTS = {
    "name":   ("Galmuri/Galmuri9.ttf", 20),       # 2x native, see bold_text()
    "big":    ("Galmuri/Galmuri9.ttf", 20),
    "value":  ("Galmuri/Galmuri11-Bold.ttf", 12),
    "value_s": ("Terminus/TerminusTTF-Bold.ttf", 12),
    "text":   ("Terminus/TerminusTTF.ttf", 12),
    "label":  ("Galmuri/Galmuri7.ttf", 8),
    "alert":  ("Terminus/TerminusTTF-Bold.ttf", 12),
    "banner": ("Galmuri/Galmuri11-Bold.ttf", 12),
}

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MODE_NAMES = {"HI_POW": "HI POWER"}                   # mode.txt value -> what the screen says
DAY_ABBR = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]      # CSV weekday 1 = Monday

# ---------------------------------------------------------------------------
# Font loading that cannot leave a blank screen
# ---------------------------------------------------------------------------
# load_font() needs Pillow's _imagingft C extension. If that is missing
# (Sept 2026: an apt full-upgrade left python3-pil without it) every font call
# raises AFTER the panel has already been cleared to white, so the box shows a
# blank display and says nothing about why. Falling back to Pillow's built-in
# bitmap font keeps the layout ugly but READABLE, which is what matters when
# the only thing you have in the field is the screen.
_FONT_FALLBACK_WARNED = False
_font_cache = {}


def load_font(path, size):
    global _FONT_FALLBACK_WARNED
    try:
        return ImageFont.truetype(path, size)
    except Exception as e:
        if not _FONT_FALLBACK_WARNED:
            print(f"[display] Cannot load TrueType fonts ({e}). "
                  f"Falling back to the built-in bitmap font -- the display will "
                  f"look wrong but still be readable. Fix with: "
                  f"sudo apt install --reinstall python3-pil libfreetype6")
            _FONT_FALLBACK_WARNED = True
        try:
            return ImageFont.load_default(size=size)   # Pillow >= 10.1 can scale it
        except Exception:
            return ImageFont.load_default()


def font(role):
    if role not in _font_cache:
        rel, size = FONTS[role]
        _font_cache[role] = load_font(os.path.join(FONT_DIR, rel), size)
    return _font_cache[role]


def text_w(f, s):
    try:
        return f.getlength(s)
    except AttributeError:                       # very old Pillow
        return f.getsize(s)[0]


def fit(f, s, max_w):
    """s, cut down with an ellipsis until it is no wider than max_w."""
    if text_w(f, s) <= max_w:
        return s
    while s and text_w(f, s + "…") > max_w:
        s = s[:-1]
    return s + "…"


def centred_y(f, top, height, sample="0Mh%"):
    """y that centres the caps/digits of f in a band (descenders may hang below)."""
    try:
        t, b = f.getbbox(sample)[1], f.getbbox(sample)[3]
    except AttributeError:
        return top
    return top + (height - (b - t)) // 2 - t


def bold_text(draw, xy, s, f, fill):
    """
    Pixel-font faux bold: the text drawn twice, 1 px apart, so 2 px strokes
    become 3 px. Real bold weights at 2x size (Galmuri11 Bold @ 24) were too
    tall and too wide for the header. The result is 1 px wider than text_w().
    """
    x, y = xy
    draw.text((x, y), s, font=f, fill=fill)
    draw.text((x + 1, y), s, font=f, fill=fill)


def new_canvas():
    image = Image.new("1", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"
    return image, draw


# ---------------------------------------------------------------------------
# Value formatting -- everything here must survive garbage from control files
# ---------------------------------------------------------------------------
def _parse_list(s):
    """'1;2;3' / '1,2,3' / '1 2 3' -> [1, 2, 3], or None if not a list of ints."""
    if s is None:
        return None
    s = str(s).strip()
    for sep in (";", ",", " "):
        if sep in s:
            parts = s.split(sep)
            break
    else:
        parts = [s]
    try:
        return [int(p) for p in parts if p.strip() != ""]
    except ValueError:
        return None


def sort_hours_night_order(hours_str):
    """
    Reorders a schedule hours string so it reads in the order the hours
    happen over a night: noon (12) first, through midnight, ending at 11am.
    Keeps whatever separator the control file used, and returns the string
    unchanged if it can't be parsed as a list of hours.
    """
    if not hours_str:
        return hours_str
    sep = next((c for c in (";", ",", " ") if c in hours_str), ";")
    hrs = _parse_list(hours_str)
    if hrs is None:
        return hours_str
    hrs.sort(key=lambda h: (h - 12) % 24)
    return sep.join(str(h) for h in hrs)


def format_when(epoch):
    """'21 Oct 09:00' in the box's local time, or None for unset / bad values."""
    try:
        epoch = int(epoch)
        if epoch <= 0:
            return None
        t = time.localtime(epoch)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return f"{t.tm_mday} {MONTHS[t.tm_mon - 1]} {t.tm_hour:02d}:{t.tm_min:02d}"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _minutes(v):
    """'59' -> '59 min'; junk ('erroruntime') -> '? min'."""
    n = _num(v)
    return f"{n:g} min" if n is not None else "? min"


def format_days(weekdays):
    days = _parse_list(weekdays)
    if not days:
        return "?"
    days = sorted(set(d for d in days if 1 <= d <= 7))
    if days == [1, 2, 3, 4, 5, 6, 7]:
        return "every day"
    if days == [1, 2, 3, 4, 5]:
        return "Mon-Fri"
    if days == [6, 7]:
        return "weekends"
    return " ".join(DAY_ABBR[d - 1] for d in days)


def _hour_runs(hrs):
    """Night-ordered hours -> runs of consecutive hours: [19,20,21,2] -> [[19,20,21],[2]]."""
    runs = []
    for h in hrs:
        if runs and (runs[-1][-1] + 1) % 24 == h:
            runs[-1].append(h)
        else:
            runs.append([h])
    return runs


def format_hours(hours, minutes="0", f=None, max_w=None):
    """
    '19 21 23 2 4', plus ' at :15 :45' when the start minute isn't :00.
    If that is wider than max_w, consecutive hours collapse to ranges
    ('18-6'), which always fits.
    """
    hrs = _parse_list(sort_hours_night_order(str(hours)))
    if not hrs:
        return "?"
    mins = _parse_list(minutes) or [0]
    at = "" if mins == [0] else " at " + " ".join(f":{m:02d}" for m in mins)
    s = " ".join(str(h) for h in hrs) + at
    if f is None or max_w is None or text_w(f, s) <= max_w:
        return s
    return " ".join(f"{r[0]}-{r[-1]}" if len(r) > 2 else " ".join(map(str, r))
                    for r in _hour_runs(hrs)) + at


def format_gb(gb):
    if gb is None:
        return "?"
    return f"{gb:.1f}" if gb < 10 else f"{gb:.0f}"


def format_count(n):
    """238 / 48.2k / 123k -- at most 5 characters."""
    n = _num(n)
    if n is None:
        return "?"
    if n < 10000:
        return f"{n:.0f}"
    if n < 99950:
        return f"{n / 1000:.1f}k"
    return f"{n / 1000:.0f}k"


def format_gps(lat, lon):
    try:
        return f"{float(lat):.3f},{float(lon):.3f}"
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------
def draw_battery(draw, x, y, percent, inverted=False):
    """22x11 battery outline with a fill bar; percent None -> a '?' inside."""
    ink = WHITE if inverted else BLACK
    w, h = 20, 11
    draw.rectangle([x, y, x + w, y + h], outline=ink)
    draw.rectangle([x + w + 1, y + 3, x + w + 2, y + h - 3], fill=ink)      # nub
    if percent is None:
        f = font("label")
        draw.text((x + 8, y + 1), "?", font=f, fill=ink)
        return
    fill_w = round((w - 3) * max(0, min(100, percent)) / 100)
    if fill_w > 0:
        draw.rectangle([x + 2, y + 2, x + 1 + fill_w, y + h - 2], fill=ink)


def labelled(draw, x, y, label, value, value_font="value", w=None, inverted=False,
             note=None):
    """A small caps label with a value under it; optional note after the value."""
    ink = WHITE if inverted else BLACK
    draw.text((x, y), label, font=font("label"), fill=ink)
    vf = font(value_font)
    value = fit(vf, value, w) if w else value
    draw.text((x, y + 9), value, font=vf, fill=ink)
    if note:
        nx = x + text_w(vf, value) + 4
        nf = font("text")
        if w:
            note = fit(nf, note, max(0, x + w - nx))
        draw.text((nx, y + 10), note, font=nf, fill=ink)        # baselines line up


def tag(draw, x, y, s, f=None, pad=2, inverted=False):
    """White text on a black box (black on white when inverted); returns its right edge."""
    f = f or font("label")
    tw = text_w(f, s)
    box, ink = (WHITE, BLACK) if inverted else (BLACK, WHITE)
    draw.rectangle([x, y, x + tw + 2 * pad, y + 10], fill=box)
    draw.text((x + pad, y + 1), s, font=f, fill=ink)
    return x + tw + 2 * pad


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------
def render_status(info):
    """
    The normal status screen. `info` keys (any may be missing or junk):
      name, mode, hw, version                         strings
      battery_pct                                     0-100, None = unknown
      next_wake, updated                              epoch seconds
      utc_offset                                      e.g. "-5.0"
      sd_free_gb, sd_photos                           numbers
      usb            list of {"free_gb", "photos"}, may be empty
      weekdays, hours, minutes, runtime, interval     control-file strings
      lat, lon
      alerts         short strings, most important first ("CAMERA DISCONNECTED")

    Layout, top to bottom (rows in px):
      0-22    name                           battery
      24-48   NEXT WAKE        | UPDATED
      50-74   SD FREE          | USB FREE    (SD inverts below LOW_STORAGE_GB)
      76-108  DAYS / HOURS / RUN
      110-121 mode tag, GPS, model+version   -- or the alert bar
    """
    image, draw = new_canvas()
    get = info.get
    lf = font("label")

    # --- header: name | battery -----------------------------------------
    HEADER_H = 22
    NAME_SAMPLE = "0Mhy%"      # centre WITH a descender so 'y'/'g' clear the rule
    pct = get("battery_pct")
    try:
        pct = None if pct is None or float(pct) < 0 else float(pct)
    except (TypeError, ValueError):
        pct = None
    pct_txt = "?" if pct is None else f"{pct:.0f}%"
    big = font("big")
    batt_x = WIDTH - (26 + text_w(big, pct_txt) + 1 + 3)
    low_batt = pct is not None and pct <= LOW_BATTERY_PCT
    if low_batt:
        draw.rectangle([batt_x - 3, 0, WIDTH, HEADER_H - 1], fill=BLACK)
    draw_battery(draw, batt_x, 6, pct, inverted=low_batt)
    bold_text(draw, (batt_x + 26, centred_y(big, 0, HEADER_H, NAME_SAMPLE)), pct_txt, big,
              WHITE if low_batt else BLACK)

    name = str(get("name") or "?")
    if len(name) > NAME_MAX_CHARS:
        name = name[:NAME_MAX_CHARS - 1] + "…"
    nf = font("name")
    name_w = batt_x - 8 - 1                      # -1: bold_text is 1 px wider
    bold_text(draw, (2, centred_y(nf, 0, HEADER_H, NAME_SAMPLE)), fit(nf, name, name_w), nf, BLACK)
    draw.line([(0, HEADER_H), (WIDTH, HEADER_H)], fill=BLACK)

    COL = 126                       # left column x 0..COL-1, right column COL+1..
    LW, RW = COL - 4, WIDTH - COL - 5

    # --- when: next wake | last update ----------------------------------
    y = HEADER_H + 2
    labelled(draw, 2, y, "NEXT WAKE", format_when(get("next_wake")) or "not set", w=LW)
    utc = str(get("utc_offset") or "").strip()
    try:
        utc = f"UTC{float(utc):+g}"
    except ValueError:
        utc = ""
    labelled(draw, COL + 4, y, f"LAST UPDATE {utc}".strip(),
             format_when(get("updated")) or "?", w=RW)

    # --- storage: SD | USB ----------------------------------------------
    y = HEADER_H + 28
    draw.line([(0, y - 2), (WIDTH, y - 2)], fill=BLACK)
    free = _num(get("sd_free_gb"))
    low_sd = free is not None and free < LOW_STORAGE_GB
    if low_sd:
        draw.rectangle([0, y - 1, COL - 1, y + 24], fill=BLACK)
    labelled(draw, 2, y, "SD FREE" + ("  -- LOW!" if low_sd else ""),
             f"{format_gb(free)}GB", w=LW, inverted=low_sd,
             note=f"{format_count(get('sd_photos'))} photos")
    usb = get("usb") or []
    if usb:
        d = usb[0]
        more = f"  (+{len(usb) - 1} more)" if len(usb) > 1 else ""
        labelled(draw, COL + 4, y, "USB FREE" + more, f"{format_gb(_num(d.get('free_gb')))}GB",
                 w=RW, note=f"{format_count(d.get('photos'))} photos")
    else:
        labelled(draw, COL + 4, y, "USB", "none", w=RW)
    draw.line([(COL, HEADER_H), (COL, y + 24)], fill=BLACK)

    # --- schedule --------------------------------------------------------
    y = HEADER_H + 54
    draw.line([(0, y - 2), (WIDTH, y - 2)], fill=BLACK)
    vs = font("value_s")
    vx = 34
    rows = [
        ("DAYS", format_days(get("weekdays"))),
        ("HOURS", format_hours(get("hours"), get("minutes", "0"), vs, WIDTH - vx - 2)),
        ("RUN", f"{_minutes(get('runtime'))}, photo every {_minutes(get('interval'))}"),
    ]
    for i, (lab, val) in enumerate(rows):
        ry = y + i * 11
        draw.text((2, ry + 2), lab, font=lf, fill=BLACK)
        draw.text((vx, ry), fit(vs, val, WIDTH - vx - 2), font=vs, fill=BLACK)

    # --- footer: mode tag + GPS + version, or mode tag + alert bar --------
    FOOT_Y = HEIGHT - 11
    mode = str(get("mode") or "?").upper()
    mode = MODE_NAMES.get(mode, mode)
    alerts = [str(a) for a in (get("alerts") or []) if a]
    if alerts:
        # Inverted bar so it can't be mistaken for normal content; e-ink keeps
        # it up until the problem is fixed, unlike a blink pattern.
        draw.rectangle([0, FOOT_Y - 1, WIDTH, HEIGHT], fill=BLACK)
        x = tag(draw, 0, FOOT_Y, mode, inverted=True) + 4
        af = font("alert")
        msg = fit(af, "  ".join("! " + a for a in alerts), WIDTH - x - 2)
        top, bottom = af.getbbox(msg)[1], af.getbbox(msg)[3]
        ty = FOOT_Y - 1 + (HEIGHT - (FOOT_Y - 1) - (bottom - top)) // 2 - top
        draw.text((x + (WIDTH - x - text_w(af, msg)) / 2, ty), msg, font=af, fill=WHITE)
    else:
        draw.line([(0, FOOT_Y - 1), (WIDTH, FOOT_Y - 1)], fill=BLACK)
        x = tag(draw, 0, FOOT_Y, mode)
        gps = format_gps(get("lat"), get("lon"))
        draw.text((x + 4, FOOT_Y + 1), f"GPS {gps}" if gps else "no GPS", font=lf, fill=BLACK)
        ver = f"{str(get('hw') or '').upper()} v{get('version', '?')}".strip()
        draw.text((WIDTH - 1 - text_w(lf, ver), FOOT_Y + 1), ver, font=lf, fill=BLACK)
    return image


def _paste_logo(image, box_w, box_h, y):
    """The logo scaled to fit box_w x box_h, centred, top at y. False if missing."""
    # The source is a large greyscale PNG: scaling that down and THEN
    # thresholding keeps the thin rays and wing lines that a pre-made 1-bit
    # image would lose when resized.
    try:
        logo = Image.open(LOGO_PATH).convert("L")
    except Exception as e:
        print(f"[display] logo unavailable ({e})")
        return False
    scale = min(box_w / logo.width, box_h / logo.height, 1)
    size = (round(logo.width * scale), round(logo.height * scale))
    logo = logo.resize(size, Image.LANCZOS).point(lambda v: 255 if v > 150 else 0).convert("1")
    image.paste(logo, ((WIDTH - logo.width) // 2, y))
    return True


def render_logo(hw="", version=""):
    """Boot splash: the logo with the model and firmware version under it."""
    image, draw = new_canvas()
    _paste_logo(image, WIDTH - 10, 96, 4)
    caption = f"MOTHBOX {str(hw).upper()}  v{version}" if version else ""
    f = font("label")
    draw.text(((WIDTH - text_w(f, caption)) / 2, HEIGHT - 13), caption, font=f, fill=BLACK)
    return image


def render_wait():
    """First boot: the root filesystem is still expanding. Stays up for minutes."""
    image, draw = new_canvas()
    has_logo = _paste_logo(image, WIDTH - 10, 56, 3)
    y = 64 if has_logo else 10
    draw.rectangle([0, y, WIDTH, y + 17], fill=BLACK)
    af = font("banner")
    msg = "FIRST BOOT: UNPACKING DISK"
    draw.text(((WIDTH - text_w(af, msg)) / 2, y + 3), msg, font=af, fill=WHITE)
    tf = font("text")
    for i, line in enumerate(["This takes a few minutes.",
                              "Please wait - do not unplug."]):
        draw.text(((WIDTH - text_w(tf, line)) / 2, y + 22 + i * 14), line, font=tf, fill=BLACK)
    return image
