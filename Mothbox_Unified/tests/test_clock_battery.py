"""Tests for clock_battery.py. Run: python3 -m pytest tests/test_clock_battery.py"""
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import clock_battery as cb


class FakeRTC:
    """
    Behaves like the Pi 5 measured in Nov 2026: with charging on,
    battery_voltage reads the charger (3.005 V) when no cell is plugged in;
    with charging off an unplugged reading decays to ~0 over a few seconds.
    """
    def __init__(self, cell_v, charging_uv=cb.CHARGE_UV, decay_s=3.0, settable=True):
        self.cell_v = cell_v              # None = no cell
        self.charging_uv = charging_uv
        self.decay_s = decay_s
        self.settable = settable
        self.t = 0.0
        self.off_at = None if charging_uv else -100.0
        self.sets = []

    def sleep(self, s):
        self.t += s

    def read_v(self):
        if self.cell_v is not None:
            return self.cell_v
        if self.charging_uv:
            return 3.005
        frac = max(0.0, 1 - (self.t - self.off_at) / self.decay_s)
        return 3.005 * frac + 0.0017

    def read_chg(self):
        return self.charging_uv

    def set_chg(self, uv):
        self.sets.append(uv)
        if not self.settable:
            return True
        if uv == 0 and self.charging_uv:
            self.off_at = self.t
        self.charging_uv = uv
        return True

    def check(self, setting):
        return cb.check(setting, read_v=self.read_v, read_chg=self.read_chg,
                        set_chg=self.set_chg, sleep=self.sleep)


class ParseSetting(unittest.TestCase):
    def test_values(self):
        self.assertEqual(cb.parse_setting("rechargeable"), (cb.RECHARGEABLE, False))
        self.assertEqual(cb.parse_setting(" Rechargeable "), (cb.RECHARGEABLE, False))
        self.assertEqual(cb.parse_setting("ML2032"), (cb.RECHARGEABLE, False))
        self.assertEqual(cb.parse_setting("regular"), (cb.REGULAR, False))
        self.assertEqual(cb.parse_setting("CR2032"), (cb.REGULAR, False))

    def test_missing_row_leaves_it_alone(self):
        self.assertEqual(cb.parse_setting(None), (None, False))

    def test_typo_never_charges(self):
        self.assertEqual(cb.parse_setting("recharable"), (cb.REGULAR, True))
        self.assertEqual(cb.parse_setting(""), (cb.REGULAR, True))


class Check(unittest.TestCase):
    def test_rechargeable_healthy(self):
        rtc = FakeRTC(2.95)
        r = rtc.check("rechargeable")
        self.assertAlmostEqual(r["cell_v"], 2.95)
        self.assertEqual(rtc.charging_uv, cb.CHARGE_UV)
        self.assertEqual(r["charging_uv"], cb.CHARGE_UV)
        self.assertEqual(r["alerts"], [])
        self.assertEqual(rtc.sets[0], 0, "must measure with charging off first")

    def test_regular_charging_turned_off(self):
        rtc = FakeRTC(3.05)                      # kernel turned charging on from an old config.txt
        r = rtc.check("regular")
        self.assertEqual(rtc.charging_uv, 0)
        self.assertEqual(r["alerts"], [])

    def test_regular_never_charged_even_if_off_at_start(self):
        rtc = FakeRTC(2.9, charging_uv=0)
        rtc.check("regular")
        self.assertNotIn(cb.CHARGE_UV, rtc.sets)
        self.assertEqual(rtc.charging_uv, 0)

    def test_missing_cell_caught_on_charging_box(self):
        # The charger reads a healthy-looking 3.005 V with no cell -- the
        # reading must be taken with charging off, after it decays.
        rtc = FakeRTC(None)
        r = rtc.check("rechargeable")
        self.assertLess(r["cell_v"], cb.NO_CELL_V)
        self.assertIn(cb.ALERT_NO_CELL, r["alerts"])
        self.assertEqual(rtc.charging_uv, 0, "nothing to charge")

    def test_slow_decay_still_caught(self):
        rtc = FakeRTC(None, decay_s=10.0)
        r = rtc.check("rechargeable")
        self.assertIn(cb.ALERT_NO_CELL, r["alerts"])

    def test_low_cell(self):
        r = FakeRTC(2.5).check("rechargeable")
        self.assertIn(cb.ALERT_LOW, r["alerts"])

    def test_fresh_regular_cell_set_as_rechargeable(self):
        rtc = FakeRTC(3.25)
        r = rtc.check("rechargeable")
        self.assertEqual(rtc.charging_uv, 0)
        self.assertIn(cb.ALERT_LOOKS_REGULAR, r["alerts"])

    def test_typo_turns_charging_off_and_says_so(self):
        rtc = FakeRTC(2.95)
        r = rtc.check("rechargable")
        self.assertEqual(rtc.charging_uv, 0)
        self.assertIn(cb.ALERT_SETTING, r["alerts"])

    def test_missing_row_restores_what_config_txt_set(self):
        rtc = FakeRTC(2.95)
        r = rtc.check(None)
        self.assertEqual(rtc.charging_uv, cb.CHARGE_UV)
        self.assertEqual(r["alerts"], [])
        rtc = FakeRTC(2.95, charging_uv=0)
        rtc.check(None)
        self.assertEqual(rtc.charging_uv, 0)

    def test_set_that_does_not_take_is_reported(self):
        rtc = FakeRTC(2.95, charging_uv=0, settable=False)
        r = rtc.check("rechargeable")
        self.assertIn(cb.ALERT_CHARGE_NOT_SET, r["alerts"])

    def test_no_rtc_files(self):
        # Pi 4 / Mac: nothing to read, nothing to complain about
        r = cb.check("rechargeable", read_v=lambda: None, read_chg=lambda: None,
                     set_chg=lambda uv: False, sleep=lambda s: None)
        self.assertIsNone(r["cell_v"])
        self.assertEqual(r["alerts"], [])

    def test_measurement_time_is_bounded(self):
        rtc = FakeRTC(None, decay_s=1000.0)
        rtc.check("rechargeable")
        self.assertLessEqual(rtc.t, cb.SETTLE_MAX_S + 1)


class ClockLost(unittest.TestCase):
    def test_floor(self):
        self.assertTrue(cb.clock_lost(0))
        self.assertTrue(cb.clock_lost(int(dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc).timestamp())))
        self.assertFalse(cb.clock_lost(int(dt.datetime(2026, 11, 13, tzinfo=dt.timezone.utc).timestamp())))
        self.assertFalse(cb.clock_lost(None))


class StatusFiles(unittest.TestCase):
    def test_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as d:
            r = FakeRTC(2.5).check("rechargeable")
            cb.write_status(d, r, True, 1000)
            self.assertEqual(cb.display_alerts(d), [cb.ALERT_CLOCK_LOST, cb.ALERT_LOW])
            self.assertTrue(cb.was_clock_lost(d))
            self.assertTrue(cb.clear_clock_lost(d, 2000))
            self.assertEqual(cb.display_alerts(d), [cb.ALERT_LOW])
            self.assertFalse(cb.was_clock_lost(d))
            self.assertFalse(cb.clear_clock_lost(d, 3000))
            self.assertEqual(cb.read_status(d)["setting"], "rechargeable")

    def test_no_status_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cb.display_alerts(d), [])
            self.assertFalse(cb.was_clock_lost(d))

    def test_history_is_trimmed(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, cb.HISTORY_FILE)
            r = FakeRTC(2.95).check("rechargeable")
            for i in range(6000):
                cb.append_history(p, 1_790_000_000 + i, r, False)
            self.assertLess(os.path.getsize(p), cb.HISTORY_MAX_BYTES * 1.1)
            with open(p) as f:
                self.assertTrue(f.readline().startswith("time,"))


class WaitForNetworkTime(unittest.TestCase):
    def _lost(self, d):
        cb.write_status(d, FakeRTC(2.95).check("rechargeable"), True, 1000)

    def test_clears_once_synced(self):
        with tempfile.TemporaryDirectory() as d:
            self._lost(d)
            answers = iter([False, False, True])
            saved, slept = [], []
            ok = cb.wait_for_network_time(d, synced=lambda: next(answers), on_good=lambda: saved.append(1),
                                          sleep=slept.append, now=lambda: 2000, log=lambda m: None)
            self.assertTrue(ok)
            self.assertEqual(saved, [1], "the good time goes into the RTC")
            self.assertFalse(cb.was_clock_lost(d))
            self.assertEqual(cb.display_alerts(d), [])
            self.assertEqual(len(slept), 2)

    def test_gives_up(self):
        with tempfile.TemporaryDirectory() as d:
            self._lost(d)
            slept = []
            ok = cb.wait_for_network_time(d, max_s=60, step_s=15, synced=lambda: False,
                                          sleep=slept.append, log=lambda m: None)
            self.assertFalse(ok)
            self.assertTrue(cb.was_clock_lost(d))
            self.assertEqual(sum(slept), 60)

    def test_nothing_to_do(self):
        with tempfile.TemporaryDirectory() as d:
            called = []
            self.assertFalse(cb.wait_for_network_time(d, synced=lambda: called.append(1) or True,
                                                      log=lambda m: None))
            self.assertEqual(called, [], "no lost clock: no checking at all")


if __name__ == "__main__":
    unittest.main()
