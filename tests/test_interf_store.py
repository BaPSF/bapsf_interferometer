import datetime
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

import interf_payload
from diag_ioc.link import IocLink
from diag_ioc.records import INVALID_ALARM, NO_ALARM
from interf_analysis import analyze_shot
from interf_ioc import MAX_POINTS, NE_WINDOW_MS, missing_text
from interf_shot import SHOT_ZONE, ShotCounter, ShotIdentifier
from interf_sim.synthetic import make_raw_shot
from interf_sim.trc_replay import TRC_DIR, ReplayLeCroy, iter_shots, trc_shots
from interf_store import ShotWriter, shot_from_group, store_shot
from ioc_harness import REPO, ChildProcess, IocProcess, free_port, have, interferometer_config, publish, pva_available

DAY_S = 86400.0
# 2026-10-09 14:30:00.125 PDT; tests that share an IOC use their own day offset.
T0 = datetime.datetime(2026, 10, 9, 14, 30, 0, tzinfo=SHOT_ZONE).timestamp() + 0.125
SAMPLES = 1 << 14  # 32 phase points: the store only copies arrays
GROUPS = {"P20": "phase_p20", "P29": "phase_p29", "P40": "phase_p40"}  # the legacy layout's phase group of each port
# test-on-Bobby's recordings in TRC_DIR (2026-10-11): shot 82435 has only C2, 82437 has no C4 (and 82436 is absent).
KNOWN_MISSING = {82435: {"P20", "P29"}, 82437: {"P29"}}


def _shot(number, day=0, **kw):
	"""A synthetic shot with its identity, on date T0 + `day` days; its trigger time is its host_time."""
	return make_raw_shot(SAMPLES, host_time=T0 + day * DAY_S + 3.0 * number, rng=np.random.default_rng(number),
	                     shot_number=number, **kw)


def _result(shot):
	"""What the IOC computes for `shot` (phase and time axes do not depend on the NE window)."""
	return analyze_shot(shot, max_points=MAX_POINTS)


def _group(result):
	"""<P>:SHOT as nested dicts: the fields interf_ioc publishes for `result`, the timestamp as softioc stores it."""
	t = result.shot_time
	value = {"timeStamp": {"secondsPastEpoch": int(t), "nanoseconds": int(1e9 * (t % 1.0))},
	         "num": result.shot_number, "date": result.shot_date, "time_source": result.time_source,
	         "host_time": result.host_time, "lost": 0, "missing": missing_text(result.acq_missing, result.ports)}
	for name, port in result.ports.items():
		alarm = {"severity": INVALID_ALARM if port.missing else NO_ALARM}
		field = value[name.lower()] = {key: {"value": values, "alarm": alarm}
		                               for key, values in (("time_array", port.t_ms), ("phase", port.phase), ("ne", port.ne))}
		field["missing"] = port.missing or ""
		field["ne_mean"] = port.ne_mean
	return value


def _stored(data_dir):
	"""{dataset name: {"file": file name, group: (array, attrs)}} over every daily file in data_dir."""
	shots = {}
	for path in sorted(Path(data_dir).glob("interferometer_data_*.hdf5")):
		with h5py.File(path, "r") as f:
			for group in (*GROUPS.values(), "time_array", "time_array_p40"):
				for name, ds in f[group].items():
					shot = shots.setdefault(name, {"file": path.name})
					shot[group] = (ds[()], dict(ds.attrs))
	return shots


class _StoreAssertions(unittest.TestCase):
	def assert_stored(self, shot, result):
		"""`shot`, one entry of _stored(), is `result` in the legacy layout with the C6 attributes."""
		day = datetime.datetime.strptime(str(result.shot_date), "%Y%m%d").date().isoformat()
		self.assertEqual(shot["file"], f"interferometer_data_{day}.hdf5")
		p20, p29 = result.ports["P20"], result.ports["P29"]
		t_lecroy = p20.t_ms if p20.t_ms.size else p29.t_ms
		for port, group in GROUPS.items():
			phase, attrs = shot[group]
			reason = result.ports[port].missing  # this port's own, never SHOT:MISSING's whole text
			expected = result.ports[port].phase
			if reason is not None and port != "P40":  # interf_file's NaN fill at the time axis length
				expected = np.full(t_lecroy.size, np.nan)
			np.testing.assert_array_equal(phase, expected, err_msg=group)
			flag, why = ("rigol_missing", "rigol_missing_reason") if port == "P40" else ("missing", "missing_reason")
			self.assertEqual(bool(attrs.get(flag, False)), reason is not None, group)
			self.assertEqual(attrs.get(why), reason, group)
			if group != "phase_p20":  # the identity goes on the reader index only
				self.assertFalse({"shot_date", "shot_number", "time_source"} & set(attrs), group)
		np.testing.assert_array_equal(shot["time_array"][0], t_lecroy)
		np.testing.assert_array_equal(shot["time_array_p40"][0], result.ports["P40"].t_ms)
		attrs = shot["phase_p20"][1]
		self.assertEqual((attrs["shot_date"], attrs["shot_number"], attrs["time_source"]),
		                 (result.shot_date, result.shot_number, result.time_source))


class GroupTests(unittest.TestCase):
	def test_a_group_value_becomes_the_shot_with_its_arrays_copied(self):
		result = _result(_shot(1, rigol=False, flat=("C3",)))
		value = _group(result)
		shot = shot_from_group(value)
		self.assertEqual((shot.shot_date, shot.shot_number, shot.shot_time, shot.time_source),
		                 (result.shot_date, 1, result.shot_time, "trigger"))
		for name, port in result.ports.items():
			np.testing.assert_array_equal(shot.phase[name], port.phase)
			np.testing.assert_array_equal(shot.t_ms[name], port.t_ms)
			self.assertFalse(np.shares_memory(shot.phase[name], value[name.lower()]["phase"]["value"]), name)

	def test_the_timestamp_gives_back_the_exact_shot_time(self):
		# The dataset name is str(shot_time); it must survive softioc's s + ns split (see shot_from_group).
		value = _group(_result(_shot(1)))
		for t in T0 + np.random.default_rng(0).uniform(-5 * 365 * DAY_S, 5 * 365 * DAY_S, 2000):
			t = float(t)
			value["timeStamp"] = {"secondsPastEpoch": int(t), "nanoseconds": int(1e9 * (t % 1.0))}
			self.assertEqual(str(shot_from_group(value).shot_time), str(t))

	def test_each_missing_port_takes_its_own_reason(self):
		value = _group(_result(_shot(1, rigol=False, flat=("C3",))))
		value["missing"] = "SHOT:MISSING, not read"
		self.assertEqual(shot_from_group(value).missing, {"P29": "C3 flat (no signal)", "P40": "synthetic shot without Rigol"})

	def test_the_group_before_the_first_shot_is_no_shot(self):
		for key, zero in (("date", 0), ("timeStamp", {"secondsPastEpoch": 0, "nanoseconds": 0})):
			with self.subTest(key):
				value = _group(_result(_shot(1)))
				value[key] = zero
				self.assertIsNone(shot_from_group(value))

	def test_a_reason_served_as_a_char_array(self):
		value = _group(_result(_shot(1, rigol=False)))
		value["p40"]["missing"] = np.frombuffer("no µ reply\0".encode(), dtype=np.int8)  # bytes above 127 are negative
		self.assertEqual(shot_from_group(value).missing, {"P40": "no µ reply"})


class StoreShotTests(_StoreAssertions):
	def setUp(self):
		self.dir = self.enterContext(tempfile.TemporaryDirectory())

	def _store(self, shot):
		result = _result(shot)
		return store_shot(self.dir, shot_from_group(_group(result))), result

	def test_the_legacy_layout_with_the_shot_attributes(self):
		stored, result = self._store(_shot(1, rigol=False))
		self.assertTrue(stored)
		shots = _stored(self.dir)
		self.assertEqual(list(shots), [str(result.shot_time)])
		self.assert_stored(shots[str(result.shot_time)], result)
		with h5py.File(Path(self.dir) / shots[str(result.shot_time)]["file"], "r") as f:
			self.assertIn("calibration factor (m^-3/rad)", f["phase_p20"].attrs)  # init_hdf5_file's group attributes

	def test_a_missing_lecroy_port_is_marked_and_the_time_axis_falls_back(self):
		stored, result = self._store(_shot(2, flat=("C1",)))
		self.assertTrue(stored)
		self.assertIsNotNone(result.ports["P20"].missing)
		self.assert_stored(_stored(self.dir)[str(result.shot_time)], result)

	def test_a_missing_lecroy_port_is_nan_at_the_time_axis_length(self):
		# Legacy readers plot phase_p20 and phase_p29 against the one time_array, so both keep its length.
		for flat, missing in ((("C1",), {"P20"}), (("C1", "C3"), {"P20", "P29"})):
			with self.subTest(missing=sorted(missing)):
				shot = _shot(5 + len(missing), flat=flat, rigol=False)
				result = self._store(shot)[1]
				stored = _stored(self.dir)[str(result.shot_time)]
				t_ms = stored["time_array"][0]
				self.assertEqual(t_ms.size == 0, missing == {"P20", "P29"})  # both missing: no LeCroy time axis
				for port in ("P20", "P29"):
					phase, attrs = stored[GROUPS[port]]
					self.assertEqual(phase.size, t_ms.size, port)
					self.assertEqual(bool(np.isnan(phase).all()) if phase.size else True, port in missing, port)
					self.assertEqual(attrs.get("missing", False), port in missing, port)
				self.assertEqual(stored["phase_p40"][0].size, 0)  # P40 stays empty

	def test_a_shot_already_stored_is_skipped(self):
		shot = _shot(3)
		self.assertTrue(self._store(shot)[0])
		self.assertFalse(self._store(shot)[0])
		self.assertEqual(len(_stored(self.dir)), 1)

	def test_a_new_date_starts_a_new_file(self):
		results = [self._store(_shot(4, day=day))[1] for day in (0, 1)]
		shots = _stored(self.dir)
		self.assertEqual(len({shot["file"] for shot in shots.values()}), 2)
		for result in results:
			self.assert_stored(shots[str(result.shot_time)], result)


class ShotWriterTests(unittest.TestCase):
	def setUp(self):
		self.dir = self.enterContext(tempfile.TemporaryDirectory())

	def test_gaps_and_repeats_are_logged_and_counted(self):
		writer = ShotWriter(self.dir, "X:SHOT")
		with self.assertLogs("interf_store", "INFO") as logs:
			writer.on_update(Exception("disconnected"))  # p4p's notice before the first connection: not an outage
			for number in (0, 1, 3, 3):
				writer.on_update(_group(_result(_shot(number))))
			writer.finish()
			writer.run()
		self.assertEqual((writer.stored, writer.duplicates, writer.not_received), (3, 1, 1))
		text = "\n".join(logs.output)
		self.assertIn("1 shots not received, 20261009-2 to 20261009-2", text)
		self.assertNotIn("failed", text)

	def test_a_full_queue_drops_the_shot(self):
		writer = ShotWriter(self.dir, "X:SHOT", queue_shots=1)
		with self.assertLogs("interf_store", "WARNING"):
			for number in (0, 1):
				writer.on_update(_group(_result(_shot(number))))
		self.assertEqual((writer.dropped, writer.queued()), (1, 1))

	def test_a_disconnect_is_logged_once_and_the_recovery_once(self):
		writer = ShotWriter(self.dir, "X:SHOT")
		value = {"date": 0}  # before the IOC's first shot: no shot, but a connection
		with self.assertLogs("interf_store", "INFO") as logs:
			writer.on_update(value)
			writer.on_update(Exception("lost"))
			writer.on_update(Exception("lost again"))
			writer.on_update(value)
		self.assertEqual(len(logs.output), 3, logs.output)
		self.assertIn("X:SHOT connected", logs.output[0])
		self.assertIn("PVA monitor of X:SHOT failed", logs.output[1])
		self.assertIn("recovered", logs.output[2])


def _store_args(prefix, data_dir):
	return ["-m", "interf_store", "--prefix", prefix, "--data-dir", data_dir]


@unittest.skipUnless(have("softioc", "epics") and pva_available(), "needs softioc, pyepics, p4p and kernel IPv6")
class StoreIocTests(_StoreAssertions):
	"""interf_store as a process on the real IOC's <P>:SHOT."""

	def test_three_shots_give_three_datasets_and_a_restart_adds_none(self):
		prefix, listen = f"INTERFSTORE{os.getpid()}", f"tcp:127.0.0.1:{free_port()}"
		data_dir = self.enterContext(tempfile.TemporaryDirectory())
		ioc = self.enterContext(IocProcess(interferometer_config(prefix, listen, NE_WINDOW_MS), f"{prefix}:IOC:HEARTBEAT"))
		link = self.enterContext(IocLink(listen, encode=interf_payload.encode))
		shots = [_shot(number) for number in (0, 1, 3)]  # 2 is never sent: a gap
		with ChildProcess(_store_args(prefix, data_dir)) as store:
			store.wait_for_lines(f"{prefix}:SHOT connected")
			for shot in shots:
				publish(link, shot, prefix, ioc)
			store.wait_for_lines("stored as", count=len(shots))
			self.assertEqual(store.stop(), 0, store.log())
		self.assertTrue(store.lines("1 shots not received, 20261009-2 to 20261009-2"), store.log())
		self.assertTrue(store.lines("stopped: 3 shots stored, 0 already stored, 1 not received"), store.log())
		with ChildProcess(_store_args(prefix, data_dir)) as restarted:
			restarted.wait_for_lines("already stored")  # the IOC sends its last shot to every new monitor
			self.assertEqual(restarted.stop(), 0, restarted.log())
		self.assertTrue(restarted.lines("stopped: 0 shots stored, 1 already stored"), restarted.log())
		stored = _stored(data_dir)
		self.assertEqual(sorted(stored), sorted(str(shot.shot_time) for shot in shots))
		for shot in shots:
			self.assert_stored(stored[str(shot.shot_time)], _result(shot))


def _replayed(trc_dir):
	"""{dataset name: (file counter, ShotResult)} of every recorded shot as interf_sim replays it: the same interf_raw
	reads, and the same identity (ShotCounter(None) numbers from 0 on every run)."""
	lecroy = ReplayLeCroy(trc_shots(trc_dir))
	identifier = ShotIdentifier(ShotCounter(None), trig_lag_max_s=0)
	expected = {}
	for shot in iter_shots(lecroy):
		identifier.identify(shot)
		expected[str(shot.shot_time)] = (lecroy.counter, _result(shot))
	return expected


@unittest.skipUnless(have("softioc", "epics") and pva_available() and TRC_DIR.is_dir(),
                     f"needs softioc, pyepics, p4p, kernel IPv6 and recorded .trc files in {TRC_DIR}")
class RecordedShotTests(_StoreAssertions):
	"""test-on-Bobby: the recorded .trc shots, replayed by interf_sim through the real IOC, stored by interf_store."""
	PERIOD_S = 0.5  # replay trigger period, far above the 0.064 s analysis, so neither the link nor the IOC drops a shot

	def test_stored_shots_equal_the_replayed_recordings(self):
		expected = _replayed(TRC_DIR)
		counters = dict(expected.values())
		self.assertTrue(all(result.ports["P40"].missing for result in counters.values()))  # the recordings hold no Rigol traces
		for counter, ports in KNOWN_MISSING.items():
			if counter in counters:
				self.assertEqual({name for name, port in counters[counter].ports.items() if port.missing}, ports | {"P40"}, counter)

		prefix, listen = f"INTERFREC{os.getpid()}", f"tcp:127.0.0.1:{free_port()}"
		data_dir = self.enterContext(tempfile.TemporaryDirectory())
		self.enterContext(IocProcess(interferometer_config(prefix, listen, NE_WINDOW_MS), f"{prefix}:IOC:HEARTBEAT"))
		with ChildProcess(_store_args(prefix, data_dir)) as store:
			store.wait_for_lines(f"{prefix}:SHOT connected")
			sim = subprocess.run([sys.executable, "-m", "interf_sim", "--trc-dir", str(TRC_DIR), "--period", str(self.PERIOD_S),
			                      "--ioc-link", listen], cwd=REPO, capture_output=True, text=True, timeout=600)
			self.assertEqual(sim.returncode, 0, sim.stdout[-3000:] + sim.stderr[-3000:])
			self.assertIn(f"IOC link: sent {len(expected)}, dropped 0, failed 0", sim.stdout, sim.stdout[-3000:])
			store.wait_for_lines("stored as", count=len(expected), timeout=60)
			self.assertEqual(store.stop(), 0, store.log())
		self.assertTrue(store.lines(f"stopped: {len(expected)} shots stored, 0 already stored, 0 not received"), store.log())
		stored = _stored(data_dir)
		self.assertEqual(sorted(stored), sorted(expected))  # dataset names are the WAVEDESC trigger times
		for name, (counter, result) in expected.items():
			with self.subTest(shot=counter):
				self.assert_stored(stored[name], result)


if __name__ == "__main__":
	unittest.main()
