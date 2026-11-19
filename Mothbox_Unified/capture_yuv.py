#!/usr/bin/python3
"""
capture_yuv.py -- take full-resolution photos with less memory.

WHY
---
A 64 MP frame is huge, and on a Pi 5 the camera stack (libcamera) also keeps
its own full-resolution working buffers. Measured on a Pi 5 1 GB, Nov 2026,
total camera (dma-buf) memory for one 9248x6944 still:

    RGB888, stock libcamera                     > 900 MB  -> killed (OOM)
    YUV420, stock libcamera                       808 MB  -> fits, barely
    YUV420, multi-frame HDR buffers disabled      679 MB  -> fits, JPEG saved OK
    RGB888, multi-frame HDR buffers disabled            -> still killed

Two changes, both here:

1. pisp_mothbox.yaml turns off the Pi's multi-frame HDR in the camera
   pipeline (LIBCAMERA_RPI_CONFIG_FILE). We never use that feature -- our
   exposure bracketing ("HDR" in camera_settings.csv) is separate -- so this
   only frees two full-resolution Bayer buffers (~129 MB). Temporal denoise
   stays ON: it may help our stills, and the camera will not even start at
   full resolution without it ("Failed to start camera: Invalid argument").

2. On boxes with little RAM, capture YUV420 (1.5 bytes/pixel, 96 MB) instead
   of RGB888 (3 bytes/pixel, 193 MB) and encode the JPEG straight from it.
   Nothing is lost for a JPEG: brightness stays full resolution, and JPEGs
   already store colour at half resolution (Pillow's default 4:2:0) -- the
   RGB route just converts RGB back to the same YUV inside the encoder.
   PNG / BMP (ImageFileType 1 / 2) need real RGB, so they always use RGB.

The buffer the camera hands back has padded rows (stride 9344 for width
9248), so the planes are copied once into tight arrays for the encoder. The
copy and the encode happen while the camera buffer is still mapped; only the
finished JPEG bytes leave encode_yuv420_jpeg().
"""
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PISP_CONFIG = os.path.join(HERE, "pisp_mothbox.yaml")
ENV_VAR = "LIBCAMERA_RPI_CONFIG_FILE"

LOW_RAM_MB = 1536          # below this (a "1 GB" Pi reports ~991 MB), capture YUV
YUV = "YUV420"
RGB = "RGB888"


# ---------------------------------------------------------------------------
# Camera pipeline config (must run before picamera2 is imported)
# ---------------------------------------------------------------------------
def is_pisp_board(model_text):
    """Pi 5 family boards (Pi 5, Pi 500, CM5) have the PiSP camera pipeline."""
    t = (model_text or "").replace("\x00", "")
    return any(s in t for s in ("Raspberry Pi 5", "Raspberry Pi 500", "Compute Module 5"))


def setup_libcamera_env(environ=None, model_path="/proc/device-tree/model", config=PISP_CONFIG):
    """
    Point libcamera at pisp_mothbox.yaml. Returns what was done, for the log.
    Leaves an existing LIBCAMERA_RPI_CONFIG_FILE alone, and does nothing on a
    board without the PiSP pipeline (the file says target: pisp).
    """
    environ = os.environ if environ is None else environ
    if environ.get(ENV_VAR):
        return f"{ENV_VAR} already set to {environ[ENV_VAR]} -- left as is"
    try:
        with open(model_path) as f:
            model = f.read()
    except OSError:
        model = ""
    if not is_pisp_board(model):
        return "not a Pi 5 -- stock camera pipeline"
    if not os.path.exists(config):
        return f"{config} missing -- stock camera pipeline (more memory)"
    environ[ENV_VAR] = config
    return f"camera pipeline: {os.path.basename(config)} (Pi multi-frame HDR buffers off)"


# ---------------------------------------------------------------------------
# YUV or RGB?
# ---------------------------------------------------------------------------
def ram_mb(meminfo="/proc/meminfo"):
    try:
        with open(meminfo) as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def choose_format(setting, total_ram_mb, image_file_type):
    """
    -> (pixel format, reason)

    setting: CaptureFormat from camera_settings.csv -- auto (default), yuv, rgb
    """
    s = str(setting if setting is not None else "auto").strip().lower()
    if int(image_file_type or 0) != 0:
        return RGB, "PNG/BMP need full RGB"
    if s in ("yuv", "yuv420"):
        return YUV, "CaptureFormat=yuv"
    if s in ("rgb", "rgb888"):
        return RGB, "CaptureFormat=rgb"
    if total_ram_mb is not None and total_ram_mb < LOW_RAM_MB:
        return YUV, f"only {total_ram_mb} MB RAM -- RGB does not fit"
    return RGB, "auto"


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------
def yuv420_planes(buf, width, height, stride):
    """
    Y, U, V as C-contiguous arrays from a YUV420 buffer with padded rows.
    Copies when the rows are padded; returns views when they are not -- so
    only use the result while `buf` is still valid.
    """
    import numpy as np
    flat = np.asarray(buf).reshape(-1)
    cs, ch, cw = stride // 2, height // 2, width // 2
    y_end = stride * height
    u_end = y_end + cs * ch
    need = u_end + cs * ch
    if flat.size < need:
        raise ValueError(f"YUV420 buffer too small: {flat.size} < {need} "
                         f"({width}x{height}, stride {stride})")
    y = flat[:y_end].reshape(height, stride)[:, :width]
    u = flat[y_end:u_end].reshape(ch, cs)[:, :cw]
    v = flat[u_end:need].reshape(ch, cs)[:, :cw]
    return tuple(p if p.flags["C_CONTIGUOUS"] else np.ascontiguousarray(p) for p in (y, u, v))


def encode_yuv420_jpeg(buf, width, height, stride, quality=96):
    """JPEG bytes from a YUV420 camera buffer. Nothing returned refers to `buf`."""
    import simplejpeg
    y, u, v = yuv420_planes(buf, width, height, stride)
    return bytes(simplejpeg.encode_jpeg_yuv_planes(y, u, v, quality=quality))


def add_exif(jpeg_bytes, exif_bytes):
    """The JPEG with EXIF inserted; the JPEG unchanged if that fails."""
    if not exif_bytes:
        return jpeg_bytes, None
    try:
        import piexif
        out = io.BytesIO()
        piexif.insert(exif_bytes, jpeg_bytes, out)
        return out.getvalue(), None
    except Exception as e:
        return jpeg_bytes, f"{e.__class__.__name__}: {e}"


def mem_report(label, environ=None, dmabuf="/sys/kernel/debug/dma_buf/bufinfo",
               meminfo="/proc/meminfo", out=print):
    """
    With MOTHBOX_MEMLOG=1 in the environment, print camera (dma-buf) memory
    and available RAM at this point. Silent otherwise; never raises.
    """
    environ = os.environ if environ is None else environ
    if environ.get("MOTHBOX_MEMLOG") != "1":
        return
    cam = avail = "?"
    try:
        with open(dmabuf) as f:
            last = f.read().strip().splitlines()[-1]          # "Total N objects, B bytes"
        cam = f"{int(last.split(',')[1].split()[0]) // 2**20} MB"
    except Exception:
        pass
    try:
        with open(meminfo) as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail = f"{int(line.split()[1]) // 1024} MB"
    except Exception:
        pass
    out(f"[memlog] {label}: camera {cam}, available {avail}")


def write_atomically(path, data):
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
