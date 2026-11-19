"""
Tests for capture_yuv.py. Run: python3 tests/test_capture_yuv.py
The JPEG tests need simplejpeg + Pillow (on the Pi: always there, picamera2
depends on simplejpeg); they are skipped where those are missing.
"""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import capture_yuv as cy

try:
    import numpy as np
except ImportError:
    np = None
try:
    import simplejpeg  # noqa: F401
    from PIL import Image
    HAVE_JPEG = np is not None
except ImportError:
    HAVE_JPEG = False


class Env(unittest.TestCase):
    def _model(self, text):
        f = tempfile.NamedTemporaryFile("w", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_pi5_gets_config(self):
        env = {}
        msg = cy.setup_libcamera_env(env, self._model("Raspberry Pi 5 Model B Rev 1.1\x00"))
        self.assertEqual(env[cy.ENV_VAR], cy.PISP_CONFIG)
        self.assertIn("HDR", msg)

    def test_cm5_and_pi500(self):
        for m in ("Raspberry Pi Compute Module 5 Rev 1.0", "Raspberry Pi 500 Rev 1.0"):
            env = {}
            cy.setup_libcamera_env(env, self._model(m))
            self.assertIn(cy.ENV_VAR, env, m)

    def test_pi4_left_alone(self):
        env = {}
        cy.setup_libcamera_env(env, self._model("Raspberry Pi 4 Model B Rev 1.5"))
        self.assertNotIn(cy.ENV_VAR, env)

    def test_existing_setting_respected(self):
        env = {cy.ENV_VAR: "/somewhere/else.yaml"}
        cy.setup_libcamera_env(env, self._model("Raspberry Pi 5 Model B"))
        self.assertEqual(env[cy.ENV_VAR], "/somewhere/else.yaml")

    def test_missing_yaml(self):
        env = {}
        cy.setup_libcamera_env(env, self._model("Raspberry Pi 5 Model B"), config="/nope.yaml")
        self.assertNotIn(cy.ENV_VAR, env)

    def test_shipped_yaml(self):
        with open(cy.PISP_CONFIG) as f:
            text = "".join(l for l in f if not l.lstrip().startswith("#"))
        self.assertIn('"disable_hdr": true', text)
        self.assertIn('"target": "pisp"', text)
        self.assertNotIn('"disable_tdn": true', text)   # camera won't start without TDN


class ChooseFormat(unittest.TestCase):
    def test_auto(self):
        self.assertEqual(cy.choose_format("auto", 991, 0)[0], cy.YUV)      # Pi 5 1 GB
        self.assertEqual(cy.choose_format("auto", 2003, 0)[0], cy.RGB)     # Pi 5 2 GB
        self.assertEqual(cy.choose_format(None, 8000, 0)[0], cy.RGB)
        self.assertEqual(cy.choose_format("auto", None, 0)[0], cy.RGB)     # unknown RAM: as before

    def test_forced(self):
        self.assertEqual(cy.choose_format("yuv", 8000, 0)[0], cy.YUV)
        self.assertEqual(cy.choose_format(" YUV420 ", 8000, 0)[0], cy.YUV)
        self.assertEqual(cy.choose_format("rgb", 991, 0)[0], cy.RGB)

    def test_png_bmp_always_rgb(self):
        for ft in (1, 2):
            self.assertEqual(cy.choose_format("yuv", 991, ft)[0], cy.RGB)


class RamMb(unittest.TestCase):
    def test_reads_meminfo(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("MemTotal:        1015132 kB\nMemFree: 1 kB\n")
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(cy.ram_mb(f.name), 991)
        self.assertIsNone(cy.ram_mb("/no/such/file"))


def _yuv_buffer(width, height, stride, y, u, v, pad=77):
    """A YUV420 buffer laid out like the camera's: padded rows, padding = junk."""
    cs = stride // 2
    buf = np.full(stride * height + 2 * cs * (height // 2), pad, dtype=np.uint8)
    yp = buf[:stride * height].reshape(height, stride)
    yp[:, :width] = y
    up = buf[stride * height:stride * height + cs * (height // 2)].reshape(height // 2, cs)
    up[:, :width // 2] = u
    vp = buf[stride * height + cs * (height // 2):].reshape(height // 2, cs)
    vp[:, :width // 2] = v
    return buf


@unittest.skipIf(np is None, "numpy not installed")
class Planes(unittest.TestCase):
    def test_padded_rows_dropped(self):
        w, h, s = 8, 4, 12
        y = np.arange(w * h, dtype=np.uint8).reshape(h, w)
        buf = _yuv_buffer(w, h, s, y, 100, 200)
        Y, U, V = cy.yuv420_planes(buf, w, h, s)
        self.assertEqual(Y.shape, (h, w))
        self.assertEqual(U.shape, (h // 2, w // 2))
        self.assertTrue((Y == y).all())
        self.assertTrue((U == 100).all() and (V == 200).all())
        self.assertTrue(all(p.flags["C_CONTIGUOUS"] for p in (Y, U, V)))

    def test_2d_buffer_like_picamera2(self):
        w, h, s = 8, 4, 12
        buf = _yuv_buffer(w, h, s, 50, 60, 70).reshape(-1, s)    # (h*3/2, stride)
        Y, U, V = cy.yuv420_planes(buf, w, h, s)
        self.assertTrue((Y == 50).all() and (U == 60).all() and (V == 70).all())

    def test_too_small(self):
        with self.assertRaises(ValueError):
            cy.yuv420_planes(np.zeros(10, np.uint8), 8, 4, 12)


@unittest.skipUnless(HAVE_JPEG, "simplejpeg / Pillow not installed")
class Jpeg(unittest.TestCase):
    def _decode(self, data):
        return np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).astype(int)

    def test_colours_survive(self):
        # Full-range BT.601 (sYCC, what the camera is configured for):
        # mid grey, then a saturated red patch.
        w, h, s = 64, 32, 80
        rgb = self._decode(cy.encode_yuv420_jpeg(_yuv_buffer(w, h, s, 128, 128, 128), w, h, s))
        self.assertEqual(rgb.shape, (h, w, 3))
        self.assertTrue((abs(rgb - 128) <= 2).all(), "grey should stay grey")
        # Y=76 Cb=85 Cr=255 is pure red (255, 0, 0) in sYCC
        rgb = self._decode(cy.encode_yuv420_jpeg(_yuv_buffer(w, h, s, 76, 85, 255), w, h, s))
        r, g, b = rgb[h // 2, w // 2]
        self.assertGreater(r, 240)
        self.assertLess(g, 15)
        self.assertLess(b, 15)

    def test_padding_never_reaches_the_image(self):
        w, h, s = 64, 32, 96
        buf = _yuv_buffer(w, h, s, 128, 128, 128, pad=255)   # white junk in the padding
        rgb = self._decode(cy.encode_yuv420_jpeg(buf, w, h, s))
        self.assertTrue((abs(rgb[:, -1] - 128) <= 3).all(), "right edge must not pick up the padding")

    def test_exif_added(self):
        import piexif
        w, h, s = 64, 32, 64
        jpg = cy.encode_yuv420_jpeg(_yuv_buffer(w, h, s, 128, 128, 128), w, h, s)
        exif = piexif.dump({"0th": {piexif.ImageIFD.Make: "Mothbox"}})
        out, err = cy.add_exif(jpg, exif)
        self.assertIsNone(err)
        self.assertEqual(piexif.load(out)["0th"][piexif.ImageIFD.Make], b"Mothbox")
        self.assertEqual(Image.open(io.BytesIO(out)).size, (w, h))

    def test_bad_exif_keeps_the_photo(self):
        w, h, s = 16, 16, 16
        jpg = cy.encode_yuv420_jpeg(_yuv_buffer(w, h, s, 128, 128, 128), w, h, s)
        out, err = cy.add_exif(jpg, b"not exif")
        self.assertIsNotNone(err)
        self.assertEqual(out, jpg)
        self.assertEqual(cy.add_exif(jpg, None), (jpg, None))

    def test_write_atomically(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.jpg")
            cy.write_atomically(p, b"abc")
            with open(p, "rb") as f:
                self.assertEqual(f.read(), b"abc")
            self.assertEqual(os.listdir(d), ["x.jpg"])


class MemReport(unittest.TestCase):
    def test_silent_by_default(self):
        out = []
        cy.mem_report("x", environ={}, out=out.append)
        self.assertEqual(out, [])

    def test_reports(self):
        with tempfile.TemporaryDirectory() as d:
            db, mi = os.path.join(d, "bufinfo"), os.path.join(d, "meminfo")
            with open(db, "w") as f:
                f.write("junk\nTotal 35 objects, 679395328 bytes\n")
            with open(mi, "w") as f:
                f.write("MemTotal: 1015132 kB\nMemAvailable:  563200 kB\n")
            out = []
            cy.mem_report("frame", environ={"MOTHBOX_MEMLOG": "1"}, dmabuf=db, meminfo=mi, out=out.append)
            self.assertEqual(out, ["[memlog] frame: camera 647 MB, available 550 MB"])

    def test_unreadable_never_raises(self):
        out = []
        cy.mem_report("x", environ={"MOTHBOX_MEMLOG": "1"}, dmabuf="/nope", meminfo="/nope", out=out.append)
        self.assertEqual(out, ["[memlog] x: camera ?, available ?"])


if __name__ == "__main__":
    unittest.main()
