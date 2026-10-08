import datetime
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from lab_scopes.lecroy import LeCroyWavedesc

import interf_main
import interf_shot
from interf_raw import RawShot
from interf_shot import SHOT_ZONE, ShotCounter, ShotIdentifier, shot_date, trigger_time
from interf_sim.synthetic import make_raw_shot, make_wavedesc


def _la(*fields, fold=0):
	"""Epoch s of an America/Los_Angeles wall-clock time; fold=1 picks the second occurrence of a repeated hour."""
	return datetime.datetime(*fields, tzinfo=SHOT_ZONE, fold=fold).timestamp()


def _wd(trigger_time=None):
	return LeCroyWavedesc(make_wavedesc(10, 1e-6, trigger_time=trigger_time)).wd


def _shot(trigger, host_time):
	"""A RawShot whose WAVEDESC trigger time is `trigger` (None: unset), detected at `host_time`."""
	return RawShot(host_time, {"C1": (np.zeros(10, dtype=np.int16), make_wavedesc(10, 1e-6, trigger_time=trigger))}, {}, {}, 0.0)


PST = _la(2026, 1, 15, 17, 0, 0) + 0.5  # 01:00:00.5 UTC on the 16th
PDT = _la(2026, 7, 15, 23, 30, 0) + 0.25  # 06:30:00.25 UTC on the 16th
REPEAT_FIRST = _la(2026, 11, 1, 1, 30, 15) + 0.125  # 01:30:15 PDT; the wall clock repeats it an hour later in PST
REPEAT_SECOND = _la(2026, 11, 1, 1, 30, 15, fold=1) + 0.125


def _temp_counter(test):
	tmp = tempfile.TemporaryDirectory()
	test.addCleanup(tmp.cleanup)
	return ShotCounter(Path(tmp.name) / "shot_counter.json")


class TriggerTimeTests(unittest.TestCase):
	def test_wavedesc_holds_the_la_wall_clock(self):
		wd = _wd(PST)
		self.assertEqual((wd.tt_year, wd.tt_months, wd.tt_days, wd.tt_hours, wd.tt_minute), (2026, 1, 15, 17, 0))
		self.assertAlmostEqual(wd.tt_second, 0.5)

	def test_standard_and_daylight_time(self):
		for name, t in (("PST", PST), ("PDT", PDT)):
			with self.subTest(name):
				shot_time, source = trigger_time(_wd(t), t + 0.3)
				self.assertEqual(source, "trigger")
				self.assertAlmostEqual(shot_time, t, places=6)
		self.assertEqual((shot_date(PST), shot_date(PDT)), (20260115, 20260715))  # the UTC dates are the 16th

	def test_the_conversion_uses_shot_zone(self):
		with mock.patch.object(interf_shot, "wavedesc_trigger_timestamp", return_value=None) as convert:
			trigger_time(_wd(PST), PST)
		self.assertEqual(convert.call_args.kwargs["tz"], SHOT_ZONE.key)

	def test_repeated_fall_back_hour_takes_the_occurrence_nearer_host_time(self):
		self.assertEqual(make_wavedesc(10, 1e-6, trigger_time=REPEAT_FIRST), make_wavedesc(10, 1e-6, trigger_time=REPEAT_SECOND))
		wd = _wd(REPEAT_SECOND)  # the same fields as REPEAT_FIRST
		self.assertAlmostEqual(trigger_time(wd, REPEAT_FIRST + 0.4)[0], REPEAT_FIRST, places=6)
		self.assertAlmostEqual(trigger_time(wd, REPEAT_SECOND + 0.4)[0], REPEAT_SECOND, places=6)

	def test_no_shift_outside_the_repeated_hour(self):
		before = _la(2026, 11, 1, 0, 59, 59) + 0.9  # the last second before the repeated hour
		self.assertAlmostEqual(trigger_time(_wd(before), before + 3600)[0], before, places=6)

	def test_host_time_without_a_trigger_time(self):
		self.assertEqual(trigger_time(_wd(None), 123.5), (123.5, "host"))  # tt_year 0
		self.assertEqual(trigger_time(None, 123.5), (123.5, "host"))  # no LeCroy data


class ShotIdentifierTests(unittest.TestCase):
	def test_identity_from_the_trigger_time(self):
		shot = _shot(PST, PST + 0.3)
		ShotIdentifier(_temp_counter(self)).identify(shot)
		self.assertEqual((shot.shot_date, shot.shot_number, shot.time_source), (20260115, 0, "trigger"))
		self.assertAlmostEqual(shot.shot_time, PST, places=6)

	def test_host_fallback(self):
		for shot in (_shot(None, PST), RawShot(PST, {}, {}, {"lecroy": "C1 not read"}, 0.0)):
			with self.subTest(lecroy=bool(shot.lecroy)):
				ShotIdentifier(_temp_counter(self)).identify(shot)
				self.assertEqual((shot.shot_time, shot.time_source, shot.shot_number), (PST, "host", 0))

	def test_unreadable_wavedesc_falls_back_to_host_time_and_is_logged(self):
		shot = make_raw_shot(4096, host_time=PST, rng=np.random.default_rng(0))
		shot.lecroy["C1"] = (shot.lecroy["C1"][0], b"bad")
		with self.assertLogs("interf_shot", logging.WARNING):
			ShotIdentifier(_temp_counter(self)).identify(shot)
		self.assertEqual((shot.shot_time, shot.time_source, shot.shot_number), (PST, "host", 0))

	def test_lag_outside_the_bound_is_logged_once_per_outage(self):
		identifier = ShotIdentifier(_temp_counter(self), trig_lag_max_s=5)
		with self.assertLogs("interf_shot", logging.WARNING) as logs:
			identifier.identify(_shot(PST, PST - 1.0))  # host clock behind the scope
			identifier.identify(_shot(PST, PST + 60.0))  # same outage: not logged again
		self.assertEqual(len(logs.records), 1)
		self.assertIn("INTERF_TRIG_LAG_MAX_S", logs.output[0])
		with self.assertLogs("interf_shot", logging.INFO) as logs:
			identifier.identify(_shot(PST, PST + 1.0))
		self.assertIn("recovered", logs.output[0])

	def test_lag_check_off_at_zero(self):
		with self.assertNoLogs("interf_shot"):
			ShotIdentifier(_temp_counter(self), trig_lag_max_s=0).identify(_shot(PST, PST - 100.0))


class ShotCounterTests(unittest.TestCase):
	def setUp(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.path = Path(tmp.name) / "state" / "shot_counter.json"

	def test_counts_from_zero_and_persists(self):
		counter = ShotCounter(self.path)
		self.assertEqual([counter.next(PST + i) for i in range(3)], [(20260115, 0), (20260115, 1), (20260115, 2)])
		self.assertEqual(json.loads(self.path.read_text()), {"shot_date": 20260115, "shot_number": 2})

	def test_restart_the_same_day_continues(self):
		ShotCounter(self.path).next(PST)
		self.assertEqual(ShotCounter(self.path).next(PST + 3), (20260115, 1))

	def test_restart_another_day_starts_at_zero(self):
		ShotCounter(self.path).next(PST)
		self.assertEqual(ShotCounter(self.path).next(PST + 86400), (20260116, 0))

	def test_la_midnight_restarts_the_number_and_utc_midnight_does_not(self):
		counter = ShotCounter(self.path)
		before_utc_midnight, after_utc_midnight = _la(2026, 1, 15, 15, 59, 59), _la(2026, 1, 15, 16, 0, 1)
		before_la_midnight, after_la_midnight = _la(2026, 1, 15, 23, 59, 59), _la(2026, 1, 16, 0, 0, 1)
		self.assertEqual([counter.next(t) for t in (before_utc_midnight, after_utc_midnight, before_la_midnight, after_la_midnight)],
		                 [(20260115, 0), (20260115, 1), (20260115, 2), (20260116, 0)])

	def test_unreadable_state_is_logged_and_counting_starts_at_zero(self):
		self.path.parent.mkdir(parents=True)
		for text in ("{not json", json.dumps({"shot_date": 20260115}), json.dumps({"shot_date": "20260115", "shot_number": 4})):
			with self.subTest(text=text):
				self.path.write_text(text)
				with self.assertLogs("interf_shot", logging.WARNING):
					counter = ShotCounter(self.path)
				self.assertEqual(counter.next(PST), (20260115, 0))

	def test_failed_write_counts_on_in_memory(self):
		self.path.parent.parent.joinpath("blocked").write_text("a file where the state directory should be")
		counter = ShotCounter(self.path.parent.parent / "blocked" / "shot_counter.json")
		with self.assertLogs("interf_shot", logging.WARNING) as logs:
			numbers = [counter.next(PST + i)[1] for i in range(3)]
		self.assertEqual(numbers, [0, 1, 2])
		self.assertEqual(len(logs.records), 1)  # throttled: the outage start only


class HandleShotTests(unittest.TestCase):
	def setUp(self):
		interf_main._outages.clear()

	def test_identity_is_assigned_before_the_outputs_and_logged(self):
		seen = []
		output = mock.Mock()
		output.write.side_effect = lambda shot: seen.append((shot.shot_date, shot.shot_number, shot.shot_time, shot.time_source))
		shots = [make_raw_shot(4096, host_time=PST + 3 * i, rng=np.random.default_rng(i)) for i in range(2)]
		identifier = ShotIdentifier(_temp_counter(self))
		with self.assertLogs("interf_main", logging.INFO) as logs:
			prev = interf_main._handle_shot(shots[0], None, identifier, [output])
			interf_main._handle_shot(shots[1], prev, identifier, [output])
		self.assertEqual([s[:2] for s in seen], [(20260115, 0), (20260115, 1)])
		self.assertAlmostEqual(seen[1][2], PST + 3, places=6)
		self.assertEqual(seen[1][3], "trigger")
		self.assertIn("shot 20260115-1", logs.output[1])
		self.assertIn("trig 17:00:03.500 dt 3.000 s", logs.output[1])  # LA wall clock, as the scope shows it

	def test_shot_without_a_trigger_time_resets_dt(self):
		shot = RawShot(PST, {}, {}, {"lecroy": "C1 not read"}, 0.0)
		with self.assertLogs("interf_main", logging.WARNING):
			self.assertIsNone(interf_main._handle_shot(shot, PST - 3, ShotIdentifier(_temp_counter(self))))


if __name__ == "__main__":
	unittest.main()
