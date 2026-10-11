import dataclasses
import datetime
import math
import os
import queue
import tomllib
import unittest

import numpy as np

import interf_payload
from diag_ioc.host import parse_config
from diag_ioc.link import IocLink
from diag_ioc.records import INVALID_ALARM, NO_ALARM
from interf_analysis import PORTS, analyze_shot, get_calibration_factor
from interf_ioc import MAX_POINTS, NE_WINDOW_MS, InterferometerModule, LostShots, missing_text, time_text
from interf_shot import SHOT_ZONE
from interf_sim.synthetic import make_raw_shot
from ioc_harness import REPO, IocProcess, epics_client, free_port, have, isolated_epics_env, monitor_all, pva_available, wait_for

DEPLOY = REPO / "deploy" / "diag-ioc"
WINDOW_MS = (0.1, 0.3)  # inside the 0.65 ms synthetic records, so NE_MEAN is a number
SAMPLES = 1 << 16
DAY_S = 86400.0
# 2026-10-09 14:30:00.125 PDT; each test publishes on its own date (day offset), so SHOT:LOST baselines never mix.
T0 = datetime.datetime(2026, 10, 9, 14, 30, 0, tzinfo=SHOT_ZONE).timestamp() + 0.125
CHAIN = ["SHOT:NUM", "SHOT:DATE", "SHOT:TIME_SOURCE", "SHOT:HOST_TIME", "SHOT:TIME_STR", "SHOT:CRIT_PATH", "STAT:TRIG_LAG",
         "SHOT:LOST", "SHOT:MISSING"] + [f"{p.name}:{s}" for p in PORTS for s in ("TIME_ARRAY", "PHASE", "NE", "NE_MEAN")]


def _shot(day, number, n=SAMPLES, **kw):
	"""A synthetic shot on date T0 + `day` days with its identity; its trigger time is its host_time."""
	return make_raw_shot(n, host_time=T0 + day * DAY_S + 3.0 * number, rng=np.random.default_rng(number), shot_number=number, **kw)


def _expected(shot):
	return analyze_shot(shot, ports=PORTS, plasma_length=0.4, max_points=MAX_POINTS, ne_window_ms=WINDOW_MS)


def _config(prefix, listen):
	"""The deployed config's sizes and access file, with a private prefix and link."""
	deployed = tomllib.loads((DEPLOY / "interferometer.toml").read_text())
	return f"""
[ioc]
name = "{prefix}:IOC"
ca_max_array_bytes = {deployed["ioc"]["ca_max_array_bytes"]}
acf = "{(DEPLOY / deployed["ioc"]["acf"]).as_posix()}"

[[module]]
name = "interferometer"
factory = "interf_ioc:InterferometerModule"
prefix = "{prefix}"
listen = "{listen}"
stale_seconds = 60

[module.options]
max_points = {deployed["module"][0]["options"]["max_points"]}
ne_window_ms = [{WINDOW_MS[0]}, {WINDOW_MS[1]}]
"""


class OptionTests(unittest.TestCase):
	def test_the_deployed_config_is_valid(self):
		config = parse_config(tomllib.loads((DEPLOY / "interferometer.toml").read_text()))
		self.assertTrue((DEPLOY / config.ioc.acf).is_file())
		(module_config,) = config.modules
		module = InterferometerModule(module_config.name, module_config.options)
		self.assertEqual((module.ports, module.max_points, module.ne_window_ms), (PORTS, MAX_POINTS, NE_WINDOW_MS))
		# The real record (1,000,002 samples: 1953 phase points) keeps every point, and one waveform fits a CA message.
		self.assertGreaterEqual(module.max_points, 1_000_002 // 512)
		self.assertGreater(config.ioc.ca_max_array_bytes, 8 * module.max_points)

	def test_configured_ports(self):
		module = InterferometerModule("m", {"ports": [{"name": "P20", "scope": "lecroy", "ref": "c3", "plasma": "c4", "freq_hz": 282e9}]})
		(port,) = module.ports
		self.assertEqual((port.name, port.ref_ch, port.plasma_ch, port.freq_hz), ("P20", "C3", "C4", 282e9))

	def test_invalid_options_name_the_problem(self):
		port = {"name": "P20", "scope": "lecroy", "ref": "C1", "plasma": "C2", "freq_hz": 288e9}
		cases = {
			"unknown keys": {"max_point": 10},
			"max_points": {"max_points": 0},
			"ne_window_ms": {"ne_window_ms": [10.0, 5.0]},
			"plasma_length_m": {"plasma_length_m": -1},
			"non-empty list": {"ports": []},
			"keys must be": {"ports": [dict(port, extra=1)]},
			"name must be": {"ports": [dict(port, name="P:20")]},
			"scope must be": {"ports": [dict(port, scope="tek")]},
			"freq_hz": {"ports": [dict(port, freq_hz=0)]},
			"unique": {"ports": [port, port]},
		}
		for fragment, options in cases.items():
			with self.subTest(fragment), self.assertRaisesRegex(ValueError, fragment):
				InterferometerModule("m", options)


class PureHelperTests(unittest.TestCase):
	def test_time_text_is_los_angeles_time(self):
		self.assertEqual(time_text(T0), "2026-10-09 14:30:00.125")  # 21:30 UTC
		winter = datetime.datetime(2026, 1, 15, 23, 59, 59, tzinfo=SHOT_ZONE).timestamp() + 0.5
		self.assertEqual(time_text(winter), "2026-01-15 23:59:59.500")  # 07:59 UTC on the 16th

	def test_lost_counts_gaps_and_resets_its_baseline(self):
		lost = LostShots()
		steps = [((1, 5), 0),  # first shot after start: baseline only
		         ((1, 6), 0), ((1, 9), 2), ((1, 10), 2),
		         ((1, 4), 2),  # a decrease (acquisition restarted the day's count): new baseline
		         ((1, 6), 3),
		         ((2, 3), 3),  # a new date: shots 0-2 of it are not counted
		         ((2, 3), 3)]  # a repeat adds nothing
		self.assertEqual([lost.update(*identity) for identity, _ in steps], [total for _, total in steps])

	def test_missing_text_gives_each_reason_once(self):
		shot = _shot(0, 1, rigol=False, flat=("C3",))
		text = missing_text(shot.missing, _expected(shot).ports)
		self.assertEqual(text, "rigol: synthetic shot without Rigol; P29: C3 flat (no signal)")  # P40 repeats the rigol reason

	def test_analyze_equals_analyze_shot_of_the_decoded_shot(self):
		shot = _shot(0, 2)
		module = InterferometerModule("m", {"ne_window_ms": list(WINDOW_MS)})
		result, expected = module.analyze(interf_payload.encode(shot)), _expected(shot)
		self.assertEqual((result.shot_date, result.shot_number, result.shot_time), (expected.shot_date, 2, shot.shot_time))
		for name, port in expected.ports.items():
			np.testing.assert_array_equal(result.ports[name].ne, port.ne)
			self.assertEqual(result.ports[name].ne_mean, port.ne_mean)


class _FakeRecord:
	def __init__(self, sets, name, fields):
		self.name, self.fields, self.infos, self._sets = name, fields, [], sets

	def add_info(self, key, value):
		self.infos.append((key, value))

	def set(self, value, **kw):
		self._sets.append((self.name, value, kw))


class _FakeBuilder:
	"""The softioc builder calls interf_ioc and diag_ioc.records make; records and set() calls are kept in order."""

	def __init__(self, prefix):
		self.prefix, self.records, self.sets = prefix, {}, []

	def RecordName(self, name):
		return f"{self.prefix}:{name}"

	def _make(self, name, **fields):
		record = self.records[name] = _FakeRecord(self.sets, name, fields)
		return record

	aIn = longIn = stringIn = longStringIn = WaveformIn = _make


class RecordLayoutTests(unittest.TestCase):
	"""create_records and publish against a fake builder: the D7 order without an IOC."""

	def setUp(self):
		self.builder = _FakeBuilder("X")
		self.module = InterferometerModule("m", {"ne_window_ms": list(WINDOW_MS)})
		self.tail = self.module.create_records(self.builder)

	def test_the_chain_runs_in_publish_order_and_returns_its_tail(self):
		order, record = [], self.builder.records["SHOT:NUM"]
		while record is not None:
			order.append(record.name)
			record = record.fields.get("FLNK")
		self.assertEqual(order, CHAIN)
		self.assertIs(self.tail, self.builder.records["P40:NE_MEAN"])
		for name in CHAIN[1:]:
			self.assertEqual((self.builder.records[name].fields["SCAN"], self.builder.records[name].fields["TSE"]), ("Passive", -2), name)
		self.assertNotIn("SCAN", self.builder.records["SHOT:NUM"].fields)  # builder default I/O Intr: set() runs the chain

	def test_the_group_triggers_once_on_the_last_record(self):
		fields = {}
		for record in self.builder.records.values():
			for key, value in record.infos:
				self.assertEqual((key, list(value)), ("Q:group", ["X:SHOT"]))
				fields.update(value["X:SHOT"])
		expected = {"", "num", "date", "time_source", "host_time", "lost", "missing"}
		expected |= {f"{p.name.lower()}.{k}" for p in PORTS for k in ("time_array", "phase", "ne", "ne_mean")}
		self.assertEqual(set(fields), expected)
		self.assertEqual([name for name, spec in fields.items() if "+trigger" in spec], ["p40.ne_mean"])
		self.assertEqual(fields[""]["+type"], "meta")

	def test_publish_stamps_every_record_with_the_shot_time_and_sets_the_head_last(self):
		shot = _shot(0, 4, rigol=False)
		self.module.publish(self.module.analyze(interf_payload.encode(shot)))
		self.assertEqual(sorted(name for name, _, _ in self.builder.sets), sorted(CHAIN))
		self.assertEqual(self.builder.sets[-1][:2], ("SHOT:NUM", 4))
		for name, _, kw in self.builder.sets:
			self.assertEqual(kw["timestamp"], shot.shot_time, name)
		p40 = {name: (value, kw) for name, value, kw in self.builder.sets if name.startswith("P40:")}
		for name in ("P40:TIME_ARRAY", "P40:PHASE", "P40:NE"):
			self.assertEqual((len(p40[name][0]), p40[name][1]["severity"]), (0, INVALID_ALARM), name)
		self.assertTrue(math.isnan(p40["P40:NE_MEAN"][0]))


def _metadata(epics, name, as_string=False):
	pv = epics.get_pv(name, form="time", connect=True, timeout=5)
	data = pv.get_with_metadata(as_string=as_string, use_monitor=False, form="time", timeout=5)
	if data is None:
		raise AssertionError(f"{name}: no reply")
	return data


@unittest.skipUnless(have("softioc", "epics"), "needs softioc and pyepics (pip install -e '.[ioc,clients]' -c constraints.txt)")
class InterferometerIocTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.prefix = f"INTERFTEST{os.getpid()}"
		cls.listen = f"tcp:127.0.0.1:{free_port()}"
		cls.ioc = cls.enterClassContext(IocProcess(_config(cls.prefix, cls.listen), f"{cls.prefix}:IOC:HEARTBEAT"))
		cls.link = cls.enterClassContext(IocLink(cls.listen, encode=interf_payload.encode))
		cls.epics = epics_client()

	def _read(self, suffix, as_string=False):
		return _metadata(self.epics, f"{self.prefix}:{suffix}", as_string)

	def _array(self, suffix):
		"""(the record's NORD values, metadata): pyepics may pad a waveform to NELM."""
		data = self._read(suffix)
		return np.asarray(data["value"]).ravel()[:int(self._read(f"{suffix}.NORD")["value"])], data

	def _publish(self, shot, timeout=15):
		"""Send `shot` and wait until SHOT:NUM shows it (number and time)."""
		self.link.write(shot)
		wait_for(lambda: (lambda d: d["value"] == shot.shot_number and abs(d["timestamp"] - shot.shot_time) < 1e-5)(self._read("SHOT:NUM")),
		         timeout, f"shot {shot.shot_number} at {shot.shot_time} on SHOT:NUM\n{self.ioc.log()}")

	def _assert_published(self, shot):
		expected = _expected(shot)
		scalars = {"SHOT:NUM": shot.shot_number, "SHOT:DATE": shot.shot_date, "SHOT:HOST_TIME": shot.host_time,
		           "SHOT:CRIT_PATH": shot.critical_path_s, "STAT:TRIG_LAG": shot.host_time - shot.shot_time}
		for suffix, value in scalars.items():
			data = self._read(suffix)
			self.assertEqual(data["value"], value, suffix)
			self.assertAlmostEqual(data["timestamp"], shot.shot_time, places=5, msg=suffix)
		texts = {"SHOT:TIME_SOURCE": shot.time_source, "SHOT:TIME_STR": time_text(shot.shot_time),
		         "SHOT:MISSING": missing_text(shot.missing, expected.ports)}
		for suffix, value in texts.items():
			data = self._read(suffix, as_string=True)
			self.assertEqual(data["value"], value, suffix)
			self.assertAlmostEqual(data["timestamp"], shot.shot_time, places=5, msg=suffix)
		for name, port in expected.ports.items():
			for suffix, values in (("TIME_ARRAY", port.t_ms), ("PHASE", port.phase), ("NE", port.ne)):
				read, data = self._array(f"{name}:{suffix}")
				np.testing.assert_array_equal(read, values, err_msg=f"{name}:{suffix}")
				self.assertEqual(data["severity"], INVALID_ALARM if port.missing else NO_ALARM, f"{name}:{suffix}")
				self.assertAlmostEqual(data["timestamp"], shot.shot_time, places=5, msg=f"{name}:{suffix}")
			mean = self._read(f"{name}:NE_MEAN")
			if math.isnan(port.ne_mean):
				self.assertTrue(math.isnan(mean["value"]))
				self.assertEqual(mean["severity"], INVALID_ALARM)
			else:
				self.assertEqual(mean["value"], port.ne_mean)

	def test_every_pv_equals_analyze_shot_and_carries_the_shot_time(self):
		shot = _shot(1, 1)
		self._publish(shot)
		self._assert_published(shot)

	def test_a_shot_without_a_trigger_time_shows_host(self):
		shot = dataclasses.replace(_shot(2, 1), time_source="host")
		self._publish(shot)
		self.assertEqual(self._read("SHOT:TIME_SOURCE", as_string=True)["value"], "host")

	def test_a_rigol_missing_shot_publishes_empty_invalid_p40_and_the_reason(self):
		shot = _shot(3, 1, rigol=False)
		self._publish(shot)
		self._assert_published(shot)
		self.assertIn("rigol: synthetic shot without Rigol", self._read("SHOT:MISSING", as_string=True)["value"])
		self.assertEqual(len(self._array("P40:NE")[0]), 0)

	def test_lost_counts_gaps_and_resets(self):
		def lost():
			return self._read("SHOT:LOST")["value"]
		self._publish(_shot(4, 5))  # a new date: baseline only
		start = lost()
		self._publish(_shot(4, 6))
		self._publish(_shot(4, 9))
		self.assertEqual(lost(), start + 2)
		self._publish(_shot(4, 2))  # a decrease resets the baseline
		self._publish(_shot(4, 3))
		self._publish(_shot(5, 4))  # a new date resets it too
		self.assertEqual(lost(), start + 2)

	def test_fixed_records_come_from_the_config(self):
		for port in PORTS:
			self.assertEqual(self._read(f"{port.name}:CAL")["value"], get_calibration_factor(port.freq_hz, 0.4))
			self.assertEqual(self._read(f"{port.name}:FREQ")["value"], port.freq_hz / 1e9)
		np.testing.assert_array_equal(self._array("CFG:NE_WINDOW_MS")[0], WINDOW_MS)

	def test_back_to_back_publishing_waits_for_every_ack(self):
		shots = [_shot(6, i) for i in range(20)]
		for shot in shots[:-1]:  # as fast as the link takes them; the link and mailbox may drop some, which is fine here
			self.link.write(shot)
		self._publish(shots[-1])
		published = self._read("STAT:PUBLISHED")["value"]
		wait_for(lambda: self._read("SHOT:ACK")["value"] == published, 5, f"SHOT:ACK == STAT:PUBLISHED ({published})")
		self.assertEqual(self._read("STAT:ACK_TIMEOUTS")["value"], 0, self.ioc.log())

	def test_a_monitor_sees_one_update_per_member_per_shot(self):
		updates = queue.Queue()
		names = [f"{self.prefix}:{suffix}" for suffix in CHAIN]
		pvs = monitor_all(names, lambda name, value, timestamp, severity: updates.put((name, timestamp)), timeout=10)
		try:
			shots = [_shot(7, i) for i in range(5)]
			for shot in shots:
				self._publish(shot)
			seen = {name: [] for name in names}
			wait_for(lambda: _drain(updates, seen) and all(len(t) >= len(shots) for t in seen.values()), 10,
			         "an update of every member for every shot")
			_drain(updates, seen)
		finally:
			for pv in pvs:
				pv.disconnect()
		for name, timestamps in seen.items():
			self.assertEqual(len(timestamps), len(shots), name)
			for timestamp, shot in zip(timestamps, shots):
				self.assertAlmostEqual(timestamp, shot.shot_time, places=5, msg=name)

	def test_caget_returns_the_full_array_of_a_real_length_record(self):
		# The lab record: 1,000,002 LeCroy samples at 0.1 us, no Rigol. 1 MHz IF stays clear of the 5 MHz Nyquist.
		shot = make_raw_shot(1_000_002, 1e-7, f_if=1e6, rigol=False, host_time=T0 + 8 * DAY_S,
		                     rng=np.random.default_rng(8), shot_number=1)
		self._publish(shot, timeout=30)
		expected = _expected(shot).ports["P20"].ne
		self.assertEqual(expected.size, 1_000_002 // 512)  # 1953: phase_from_raw keeps whole windows only
		value = self.epics.caget(f"{self.prefix}:P20:NE", timeout=10)
		np.testing.assert_array_equal(np.asarray(value)[:expected.size], expected)
		self.assertEqual(int(self._read("P20:NE.NORD")["value"]), expected.size)

	def test_ca_puts_are_rejected(self):
		for suffix in ("SHOT:NUM", "P20:NE", "CFG:NE_WINDOW_MS", "SHOT:ACK", "STAT:PUBLISHED", "IOC:HEARTBEAT"):
			with self.subTest(suffix):
				pv = self.epics.get_pv(f"{self.prefix}:{suffix}.DESC", connect=True, timeout=5)
				before = pv.get(use_monitor=False, as_string=True)
				self.assertTrue(pv.read_access)
				# Access rights come from the access file alone: DISP=1 also blocks puts, but leaves the right granted.
				self.assertFalse(pv.write_access)
				try:
					pv.put("changed", wait=True, timeout=5)
				except Exception:
					pass  # pyepics may refuse up front; either way the value must not change
				self.assertEqual(pv.get(use_monitor=False, as_string=True), before)

	def test_the_db_file_lists_every_pv(self):
		text = self.ioc.db_path.read_text()
		for suffix in CHAIN + ["SHOT:ACK", "STAT:PUBLISHED", "STAT:ACK_TIMEOUTS", "CFG:NE_WINDOW_MS", "P20:CAL", "P20:FREQ"]:
			self.assertIn(f'"{self.prefix}:{suffix}"', text, suffix)
		self.assertIn(f'"{self.prefix}:SHOT"', text)  # the Q:group info


def _drain(updates, seen):
	"""Move queued (name, timestamp) updates into `seen`; always True, for use inside a wait_for predicate."""
	while True:
		try:
			name, timestamp = updates.get_nowait()
		except queue.Empty:
			return True
		seen[name].append(timestamp)


@unittest.skipUnless(have("softioc") and pva_available(), "needs softioc, p4p and kernel IPv6")
class InterferometerPvaTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		from p4p.client.thread import Context
		cls.prefix = f"INTERFPVA{os.getpid()}"
		cls.listen = f"tcp:127.0.0.1:{free_port()}"
		cls.ioc = cls.enterClassContext(IocProcess(_config(cls.prefix, cls.listen), f"{cls.prefix}:IOC:HEARTBEAT"))
		cls.link = cls.enterClassContext(IocLink(cls.listen, encode=interf_payload.encode))
		env = isolated_epics_env()
		cls.ctx = Context("pva", useenv=False, conf={key: env[key] for key in (
			"EPICS_PVA_ADDR_LIST", "EPICS_PVA_AUTO_ADDR_LIST", "EPICS_PVA_SERVER_PORT", "EPICS_PVA_BROADCAST_PORT")})
		cls.addClassCleanup(cls.ctx.close)
		cls.epics = epics_client()

	def _publish(self, shot):
		self.link.write(shot)
		wait_for(lambda: _metadata(self.epics, f"{self.prefix}:SHOT:NUM")["value"] == shot.shot_number, 15,
		         f"shot {shot.shot_number}\n{self.ioc.log()}")

	def _assert_group_is(self, value, shot):
		expected = _expected(shot)
		self.assertEqual((value["num"], value["date"], value["time_source"]), (shot.shot_number, shot.shot_date, shot.time_source))
		stamp = value["timeStamp"]
		self.assertAlmostEqual(stamp["secondsPastEpoch"] + stamp["nanoseconds"] * 1e-9, shot.shot_time, places=5)
		for name, port in expected.ports.items():
			np.testing.assert_array_equal(value[name.lower()]["ne"]["value"], port.ne)

	def test_the_group_matches_the_shot(self):
		shot = _shot(11, 1)
		self._publish(shot)
		self._assert_group_is(self.ctx.get(f"{self.prefix}:SHOT", timeout=5), shot)

	def test_a_monitor_gets_one_consistent_update_per_shot(self):
		updates = queue.Queue()

		def next_value(timeout):
			while True:
				value = updates.get(timeout=timeout)
				if not isinstance(value, Exception):  # notify_disconnect also delivers Disconnected notices
					return value

		subscription = self.ctx.monitor(f"{self.prefix}:SHOT", updates.put, notify_disconnect=True)
		try:
			next_value(10)  # the connect-time value, not a shot
			shots = [_shot(12, i) for i in range(3)]
			for shot in shots:
				self._publish(shot)
			received = [next_value(10) for _ in shots]
			with self.assertRaises(queue.Empty):
				next_value(1)
		finally:
			subscription.close()
		for value, shot in zip(received, shots):
			self._assert_group_is(value, shot)

	def test_pva_puts_are_rejected(self):
		# IOC:HEARTBEAT has DISP=0, so only the access file can refuse a put to it (D8: verify QSRV2 applies it).
		for suffix in ("SHOT:LOST.DESC", "IOC:HEARTBEAT.DESC"):
			with self.subTest(suffix):
				name = f"{self.prefix}:{suffix}"
				before = str(self.ctx.get(name, timeout=5))
				with self.assertRaises(Exception):
					self.ctx.put(name, "changed", timeout=5)
				self.assertEqual(str(self.ctx.get(name, timeout=5)), before)


if __name__ == "__main__":
	unittest.main()
