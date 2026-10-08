import os
import queue
import re
import socket
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path

import numpy as np

from diag_ioc.host import ModuleConfig, parse_config
from diag_ioc.link import IocLink
from diag_ioc.module import DiagnosticModule, ShotPipeline
from diag_ioc.records import MAJOR_ALARM, NO_ALARM
from dummy_ioc_module import WAVE_NELM
from ioc_harness import IocProcess, as_is, epics_client, free_port, have, wait_for

SPEC_EXAMPLE = """
[ioc]
name = "LAPD:DIAG:INTERF:IOC"
ca_max_array_bytes = 16777216

[[module]]
name = "interferometer"
factory = "interf_ioc:InterferometerModule"
prefix = "LAPD:DIAG:INTERF"
listen = "unix:/run/diag-ioc/interferometer/link.sock"
allow = ["127.0.0.1/32"]
mailbox_depth = 2
stale_seconds = 10
[module.options]
plasma_length_m = 0.4
ne_window_ms = [4.0, 6.0]
"""


def _ioc_config(prefix, listen, stale_seconds=10):
	return f"""
[ioc]
name = "{prefix}:IOC"

[[module]]
name = "dummy"
factory = "dummy_ioc_module:DummyModule"
prefix = "{prefix}"
listen = "{listen}"
stale_seconds = {stale_seconds}
[module.options]
scale = 2.0
"""


class ConfigTests(unittest.TestCase):
	def test_spec_example_parses(self):
		config = parse_config(tomllib.loads(SPEC_EXAMPLE))
		self.assertEqual((config.ioc.name, config.ioc.ca_max_array_bytes), ("LAPD:DIAG:INTERF:IOC", 16777216))
		(module,) = config.modules
		self.assertEqual((module.name, module.prefix, module.allow), ("interferometer", "LAPD:DIAG:INTERF", ("127.0.0.1/32",)))
		self.assertEqual(module.options, {"plasma_length_m": 0.4, "ne_window_ms": [4.0, 6.0]})

	def test_defaults(self):
		config = parse_config(tomllib.loads('[ioc]\nname = "X:IOC"\n[[module]]\nname = "m"\nfactory = "a.b:C"\nprefix = "X"\n'))
		self.assertEqual(config.ioc.ca_max_array_bytes, 16777216)
		self.assertEqual(config.modules[0], ModuleConfig("m", "a.b:C", "X"))

	def test_invalid_configs_name_the_problem(self):
		base = tomllib.loads(SPEC_EXAMPLE)
		cases = {
			"top level: unknown keys": dict(base, extra=1),
			"[ioc] table is required": {"module": base["module"]},
			"unknown keys": dict(base, module=[dict(base["module"][0], stale_second=3)]),
			"prefix must be": dict(base, module=[dict(base["module"][0], prefix="LAPD:DIAG:INTERF:")]),
			"factory must be": dict(base, module=[dict(base["module"][0], factory="interf_ioc.Module")]),
			"listen": dict(base, module=[dict(base["module"][0], listen="udp:host:1")]),
			"allow must be a list": dict(base, module=[dict(base["module"][0], allow="127.0.0.1/32")]),
			"allow: ": dict(base, module=[dict(base["module"][0], allow=["not-a-range"])]),
			"mailbox_depth": dict(base, module=[dict(base["module"][0], mailbox_depth=0)]),
			"stale_seconds": dict(base, module=[dict(base["module"][0], stale_seconds=-1)]),
			"must be unique": dict(base, module=[base["module"][0], dict(base["module"][0], name="other", listen=None)]),
			"at least one [[module]]": {"ioc": base["ioc"]},
		}
		for fragment, data in cases.items():
			with self.subTest(fragment), self.assertRaisesRegex(ValueError, re.escape(fragment)):
				parse_config(data)


class _RecordingModule(DiagnosticModule):
	"""analyze() passes the message through, raising for {"fail": ...}; publish() can be held by `gate`."""

	def __init__(self):
		super().__init__("recording", {})
		self.published = []
		self.busy = threading.Event()
		self.gate = threading.Event()
		self.gate.set()

	def analyze(self, variables):
		if "fail" in variables:
			raise ValueError("bad shot")
		return variables["i"]

	def publish(self, result):
		self.busy.set()
		self.gate.wait(5)
		self.published.append(result)


class ShotPipelineTests(unittest.TestCase):
	def test_full_mailbox_drops_the_oldest(self):
		module = _RecordingModule()
		module.gate.clear()
		pipeline = ShotPipeline(module, depth=2)
		try:
			pipeline.submit({"i": 0})
			self.assertTrue(module.busy.wait(5))  # shot 0 is in publish(), held by the gate
			for i in (1, 2, 3):
				pipeline.submit({"i": i})
			self.assertEqual(pipeline.dropped, 1)  # 1 dropped for 3; 2 and 3 wait
			module.gate.set()
			wait_for(lambda: module.published == [0, 2, 3], 5, "shots 0, 2, 3 published")
		finally:
			pipeline.close()

	def test_failure_is_reported_and_cleared_by_the_next_shot(self):
		done = queue.Queue()
		pipeline = ShotPipeline(_RecordingModule(), on_done=lambda seconds, error: done.put((seconds, error)))
		try:
			pipeline.submit({"fail": 1})
			self.assertEqual(done.get(timeout=5)[1], "ValueError: bad shot")
			pipeline.submit({"i": 1})
			seconds, error = done.get(timeout=5)
			self.assertEqual(error, "")
			self.assertGreaterEqual(seconds, 0.0)
		finally:
			pipeline.close()

	def test_close_discards_late_messages(self):
		module = _RecordingModule()
		pipeline = ShotPipeline(module)
		pipeline.close()  # joins the worker, so nothing can still be in flight
		pipeline.submit({"i": 1})
		self.assertEqual(module.published, [])


@unittest.skipUnless(have("softioc", "epics"), "needs softioc and pyepics (pip install -e '.[ioc,clients]')")
class DiagIocTests(unittest.TestCase):
	STALE_SECONDS = 2

	@classmethod
	def setUpClass(cls):
		cls.prefix = f"DIAGTEST{os.getpid()}"
		cls.listen = f"tcp:127.0.0.1:{free_port()}"
		cls.ioc = cls.enterClassContext(IocProcess(_ioc_config(cls.prefix, cls.listen, cls.STALE_SECONDS), f"{cls.prefix}:IOC:HEARTBEAT"))
		cls.epics = epics_client()

	def _read(self, suffix, as_string=False):
		"""{value, severity, timestamp, ...} read fresh from the IOC (not a cached monitor)."""
		pv = self.epics.get_pv(f"{self.prefix}:{suffix}", form="time", connect=True, timeout=5)
		data = pv.get_with_metadata(as_string=as_string, use_monitor=False, form="time", timeout=5)
		self.assertIsNotNone(data, f"{suffix}: no reply\n{self.ioc.log()}")
		return data

	def _wait_read(self, suffix, accept, timeout, what, as_string=False):
		"""The first fresh read of `suffix` whose metadata dict passes accept()."""
		def check():
			data = self._read(suffix, as_string)
			return data if accept(data) else None
		return wait_for(check, timeout, f"{suffix} {what}")

	def _wait_value(self, suffix, expected, timeout=10, as_string=False):
		return self._wait_read(suffix, lambda d: d["value"] == expected, timeout, f"== {expected!r}", as_string)

	def test_published_values_carry_the_message_timestamp(self):
		with IocLink(self.listen, encode=as_is) as link:
			for seq in (101, 102, 103):  # not 0: SEQ starts at 0
				ts = 1_700_000_000.25 + seq
				wave = np.arange(WAVE_NELM, dtype=float) * seq  # full NELM, so CA padding cannot matter
				link.write({"seq": np.array(seq), "value": np.array(1.5 * seq), "wave": wave, "ts": np.array(ts)})
				self._wait_value("SEQ", seq)
				value, wave_read = self._read("VALUE"), self._read("WAVE")
				self.assertEqual(value["value"], 1.5 * seq * 2.0)
				np.testing.assert_array_equal(np.asarray(wave_read["value"]), wave)
				for data in (self._read("SEQ"), value, wave_read):
					self.assertAlmostEqual(data["timestamp"], ts, places=5)

	def test_connected_and_stale_follow_the_link(self):
		with IocLink(self.listen, encode=as_is) as link:
			link.write({"seq": np.array(201), "value": np.array(0.0), "wave": np.zeros(WAVE_NELM), "ts": np.array(time.time())})
			self._wait_value("SEQ", 201)
			self._wait_value("STAT:CONNECTED", 1, timeout=5)
			self._wait_value("STAT:STALE", 0, timeout=5)
			stale = self._wait_value("STAT:STALE", 1, timeout=self.STALE_SECONDS + 5)
			self.assertEqual(stale["severity"], MAJOR_ALARM)
			self.assertGreater(self._read("STAT:AGE_S")["value"], self.STALE_SECONDS)
		self._wait_value("STAT:CONNECTED", 0, timeout=5)

	def test_analysis_error_sets_and_clears_stat_error(self):
		with IocLink(self.listen, encode=as_is) as link:
			link.write({"fail": np.array(1)})
			error = self._wait_read("STAT:ERROR", lambda d: d["value"], 10, "set", as_string=True)
			self.assertIn("asked to fail", error["value"])
			self.assertEqual(error["severity"], MAJOR_ALARM)
			link.write({"seq": np.array(301), "value": np.array(0.0), "wave": np.zeros(WAVE_NELM), "ts": np.array(time.time())})
			self._wait_value("SEQ", 301)
			cleared = self._wait_value("STAT:ERROR", "", as_string=True)
			self.assertEqual(cleared["severity"], NO_ALARM)

	def test_heartbeat_advances(self):
		first = self._read("IOC:HEARTBEAT")["value"]
		wait_for(lambda: self._read("IOC:HEARTBEAT")["value"] != first, 5, "HEARTBEAT to change")


@unittest.skipUnless(have("softioc", "epics"), "needs softioc and pyepics (pip install -e '.[ioc,clients]')")
class ShutdownTests(unittest.TestCase):
	def test_sigterm_exits_promptly_and_removes_the_socket(self):
		unix = sys.platform.startswith("linux") and hasattr(socket, "AF_UNIX")
		prefix = f"DIAGSTOP{os.getpid()}"
		with tempfile.TemporaryDirectory(dir="/tmp" if unix else None) as tmp:
			sock = Path(tmp) / "link.sock"
			listen = f"unix:{sock}" if unix else f"tcp:127.0.0.1:{free_port()}"
			with IocProcess(_ioc_config(prefix, listen), f"{prefix}:IOC:HEARTBEAT") as ioc:
				if unix:
					self.assertTrue(sock.is_socket(), ioc.log())
				code = ioc.stop(timeout=10)  # raises past 10 s
			self.assertEqual(code, 0, ioc.log())
			if unix:
				self.assertFalse(sock.exists(), "atexit cleanup did not remove the link socket")


if __name__ == "__main__":
	unittest.main()
