import functools
import logging
import sys
import types
import unittest
from unittest import mock

import interf_main
import interf_shot
from interf_png import PngMonitor
from interf_raw import RawShot
from test_interf_shot import PST, _log_line

RUN, SHOT = "TEST:RunNumber", "TEST:ShotNumber"


class _FakeCA:
	def __init__(self):
		self.flushes = 0
		self.error = None  # raised by flush_io when set
		self.on_flush = lambda: None

	def flush_io(self):
		self.flushes += 1
		if self.error is not None:
			raise self.error
		self.on_flush()


class _FakePV:
	"""The pyepics PV surface PngMonitor uses; post() and connect() call its callbacks as a CA thread would."""

	def __init__(self, epics, pvname, auto_monitor=None, form="native", callback=None, connection_callback=None):
		if not (auto_monitor is True and form == "time"):
			raise AssertionError(f"{pvname}: auto_monitor={auto_monitor!r}, form={form!r}")
		self.pvname, self.callback, self.connection_callback = pvname, callback, connection_callback
		epics.pvs[pvname] = self

	def post(self, value, timestamp):
		self.callback(pvname=self.pvname, value=value, timestamp=timestamp, severity=0)

	def connect(self, conn):
		self.connection_callback(pvname=self.pvname, conn=conn, pv=self)


def _fake_epics(first=None):
	"""A stand-in `epics` module; each PV in `first` ({name: (value, timestamp)}) delivers it at the first flush_io(),
	as a monitor subscribes then."""
	epics = types.ModuleType("epics")
	epics.pvs = {}
	epics.ca = _FakeCA()
	epics.PV = functools.partial(_FakePV, epics)
	pending = dict(first or {})

	def deliver():
		for name in [n for n in pending if n in epics.pvs]:
			epics.pvs[name].post(*pending.pop(name))
	epics.ca.on_flush = deliver
	return epics


def _monitor(epics, connect_timeout=1.0):
	with mock.patch.dict(sys.modules, {"epics": epics}):
		return PngMonitor(RUN, SHOT, connect_timeout=connect_timeout)


class PngMonitorTests(unittest.TestCase):
	def test_values_are_stored_with_their_timestamps_and_copied(self):
		epics = _fake_epics({RUN: (12, PST - 60.0), SHOT: (344, PST - 3.0)})
		with self.assertLogs("interf_png", logging.INFO) as logs:
			png = _monitor(epics)
		self.assertIn("PNG monitors ready: run 12, shot 344", logs.output[-1])
		self.assertEqual(png.latest(), {"run": 12, "run_time": PST - 60.0, "shot": 344, "shot_time": PST - 3.0})
		flushes = epics.ca.flushes
		epics.pvs[SHOT].post(345, PST - 0.1)
		self.assertEqual(epics.ca.flushes, flushes)  # the callback makes no CA call
		latest = png.latest()
		self.assertEqual((latest["shot"], latest["shot_time"]), (345, PST - 0.1))
		latest["shot"] = 0
		self.assertEqual(png.latest()["shot"], 345)  # a new dict per call

	def test_latest_before_a_first_value_is_none_and_the_start_gives_up(self):
		epics = _fake_epics({RUN: (12, PST - 60.0)})  # PNG:ShotNumber never delivers
		with self.assertLogs("interf_png", logging.WARNING) as logs:
			png = _monitor(epics, connect_timeout=0.2)
		self.assertEqual(len(logs.output), 1)
		self.assertIn("PNG not connected: no value from TEST:ShotNumber", logs.output[0])
		self.assertEqual(png.latest(), {"run": 12, "run_time": PST - 60.0, "shot": None, "shot_time": None})
		epics.pvs[SHOT].post(345, PST - 0.1)  # a late first value still arrives
		self.assertEqual(png.latest()["shot"], 345)

	def test_flush_never_raises(self):
		epics = _fake_epics({RUN: (12, PST), SHOT: (345, PST)})
		png = _monitor(epics)
		epics.ca.error = OSError("CA context gone")
		with self.assertLogs("interf_png", logging.WARNING) as logs:
			png.flush()
			png.flush()
		self.assertEqual(len(logs.output), 1)
		self.assertIn("PNG CA flush failed (OSError: CA context gone", logs.output[0])
		epics.ca.error = None
		with self.assertLogs("interf_png", logging.INFO) as logs:
			png.flush()
		self.assertIn("PNG CA flush recovered", logs.output[0])

	def test_each_disconnect_and_reconnect_is_logged_once(self):
		epics = _fake_epics({RUN: (12, PST), SHOT: (345, PST)})
		png = _monitor(epics)
		pv = epics.pvs[SHOT]
		with self.assertNoLogs("interf_png", logging.INFO):
			pv.connect(True)  # the first connection
		with self.assertLogs("interf_png", logging.INFO) as logs:
			pv.connect(False)
			pv.connect(False)
			pv.connect(True)
			pv.connect(True)
		self.assertEqual(len(logs.output), 2, logs.output)
		self.assertIn(f"WARNING:interf_png:PNG {SHOT} failed (disconnected; shots keep its last value", logs.output[0])
		self.assertIn(f"INFO:interf_png:PNG {SHOT} recovered after", logs.output[1])
		self.assertEqual(png.latest()["shot"], 345)  # kept through the outage


class ShotLinePngTests(unittest.TestCase):
	def _line(self, png, missing=None):
		shot = RawShot(PST, {}, {}, missing or {}, 0.1, shot_date=20260115, shot_number=7, shot_time=PST, time_source="host",
		               png=png)
		return _log_line(interf_main._shot_line(shot, None))

	def test_png_part_is_last(self):
		png = {"run": 12, "run_time": PST - 60.0, "shot": 345, "shot_time": PST - 0.15}
		line = self._line(png, missing={"rigol": "disabled (RIGOL_IP empty)"})
		self.assertTrue(line.endswith(" | missing rigol: disabled (RIGOL_IP empty) | png 12-345 lag 0.150"), line)

	def test_no_png_or_no_shot_value(self):
		for png in (None, {"run": 12, "run_time": PST, "shot": None, "shot_time": None}):
			with self.subTest(png=png):
				self.assertTrue(self._line(png).endswith(" | path 0.10 s | png -"))
		run_unknown = {"run": None, "run_time": None, "shot": 345, "shot_time": PST - 3.0}  # the run PV never delivered
		self.assertTrue(self._line(run_unknown).endswith(" | png ?-345 lag 3.000"))

	def test_shot_counter_still_reads_the_line(self):
		# _LOGGED_SHOT matches the line's start, so the PNG part cannot change it.
		line = self._line({"run": 12, "run_time": PST, "shot": 345, "shot_time": PST})
		self.assertEqual(interf_shot._LOGGED_SHOT.search(line).groups(), ("20260115", "7"))


if __name__ == "__main__":
	unittest.main()
