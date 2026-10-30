import dataclasses
import json
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import nacl.public

import interf_main
from diag_ioc import outage
from interf_raw import RawShot
from interf_shot import ShotCounter, ShotIdentifier
from streamer import RawOutput
from streamer.adios_io import ADIOS2_AVAILABLE, AdiosIO
from streamer.arguments import RawOutputSettings, settings_from_env
from streamer.consumer_output import RotatingConsumerOutput
from streamer.raw_output_io import IO, unused_path
from streamer.connection_security import (
	CONNECTION_FILE_FORMAT,
	decrypt_connection_info,
	encrypt_connection_info,
)
from streamer.payload import decode_json, shot_variables
from streamer.socket_protocol import receive_message, send_arrays


class PayloadTests(unittest.TestCase):
	def test_shot_variables_include_samples_and_scope_metadata(self):
		lecroy_samples = np.arange(5, dtype=np.int16)
		rigol_samples = np.arange(4, dtype=np.uint16)
		shot = RawShot(
			host_time=123.5,
			lecroy={"C1": (lecroy_samples, b"WAVEDESC")},
			rigol={
				"C2": (
					rigol_samples,
					{"y_increment": np.float32(0.25), "flags": np.array([1, 2])},
				)
			},
			missing={"lecroy": "C4 not read"},
			critical_path_s=0.75,
		)

		data = shot_variables(shot, 7)

		np.testing.assert_array_equal(data["lecroy_c1_samples"], lecroy_samples)
		np.testing.assert_array_equal(data["rigol_c2_samples"], rigol_samples)
		self.assertEqual(data["lecroy_c1_wavedesc"].tobytes(), b"WAVEDESC")
		self.assertEqual(
			decode_json(data["rigol_c2_metadata_json"]),
			{"flags": [1, 2], "y_increment": 0.25},
		)
		self.assertEqual(decode_json(data["missing_json"]), shot.missing)
		self.assertEqual(data["shot_index"], 7)
		self.assertTrue(data["lecroy_c1_samples"].flags.aligned)
		self.assertTrue(data["lecroy_c1_samples"].flags.owndata)


class MainIntegrationTests(unittest.TestCase):
	def setUp(self):
		interf_main._outages.clear()  # main() does this per run; these tests call _handle_shot directly
		self.identifier = ShotIdentifier(ShotCounter(None))

	def test_handle_shot_writes_before_logging(self):
		events = []
		raw_output = mock.Mock()
		raw_output.write.side_effect = lambda shot: events.append("write")
		shot = RawShot(1.0, {}, {}, {"lecroy": "missing"}, 0.1)
		with mock.patch.object(
			interf_main.log,
			"log",
			side_effect=lambda *args: events.append("log"),
		):
			self.assertIsNone(interf_main._handle_shot(shot, None, self.identifier,[raw_output]))
		self.assertEqual(events, ["write", "log"])

	def test_failing_output_does_not_stop_later_outputs_or_log(self):
		events = []
		broken = mock.Mock()
		broken.write.side_effect = OSError("disk full")
		healthy = mock.Mock()
		healthy.write.side_effect = lambda shot: events.append("write")
		shot = RawShot(1.0, {}, {}, {}, 0.1)
		with mock.patch.object(interf_main.log, "log", side_effect=lambda *args: events.append("log")), \
				mock.patch.object(interf_main.log, "warning") as warning, \
				mock.patch.object(interf_main.log, "info") as info:
			interf_main._handle_shot(shot, None, self.identifier,[broken, healthy])
			interf_main._handle_shot(shot, None, self.identifier,[broken, healthy])
			broken.write.side_effect = None
			interf_main._handle_shot(shot, None, self.identifier,[broken, healthy])
		self.assertEqual(events, ["write", "log"] * 3)
		warning.assert_called_once()  # the outage start; the repeat waits OUTAGE_LOG_INTERVAL_S
		self.assertTrue(warning.call_args.kwargs["exc_info"])  # with the traceback
		messages = [c.args[0] for c in info.call_args_list]
		self.assertEqual(sum("recovered" in m for m in messages), 1)  # recovery
		self.assertEqual(sum(m.startswith("timing ") for m in messages), 3)  # one per shot

	def test_unhashable_output_is_isolated_and_recovers(self):
		@dataclasses.dataclass
		class ListOutput:  # eq=True (the default) sets __hash__ = None
			fail: bool = True
			written: list = dataclasses.field(default_factory=list)

			def write(self, shot):
				if self.fail:
					raise OSError("disk full")
				self.written.append(shot)

		output = ListOutput()
		with self.assertRaises(TypeError):
			hash(output)
		events = []
		shot = RawShot(1.0, {}, {}, {}, 0.1)
		with mock.patch.object(interf_main.log, "log", side_effect=lambda *args: events.append("log")), \
				mock.patch.object(interf_main.log, "warning") as warning, \
				mock.patch.object(interf_main.log, "info") as info:
			interf_main._handle_shot(shot, None, self.identifier,[output])
			output.fail = False
			interf_main._handle_shot(shot, None, self.identifier,[output])
		self.assertEqual(events, ["log", "log"])  # the shot line survived the failing write
		self.assertEqual(output.written, [shot])
		warning.assert_called_once()
		messages = [c.args[0] for c in info.call_args_list]
		self.assertEqual(sum("recovered" in m for m in messages), 1)
		self.assertEqual(sum(m.startswith("timing ") for m in messages), 2)

	def test_repeated_output_failure_warns_with_count(self):
		broken = mock.Mock()
		broken.write.side_effect = OSError("disk full")
		shot = RawShot(1.0, {}, {}, {}, 0.1)
		with mock.patch.object(outage, "OUTAGE_LOG_INTERVAL_S", 0.0), \
				mock.patch.object(interf_main.log, "log"), \
				mock.patch.object(interf_main.log, "warning") as warning:
			for _ in range(3):
				interf_main._handle_shot(shot, None, self.identifier,[broken])
		messages = [c.args[0] % c.args[1:] for c in warning.call_args_list]
		self.assertEqual(len(messages), 3)  # start, then one per (zero-length) interval
		self.assertIn("2 failures", messages[1])
		self.assertIn("3 failures", messages[2])


class TransportTests(unittest.TestCase):
	def test_generic_named_variables_round_trip_over_socket_protocol(self):
		left, right = socket.socketpair()
		try:
			expected = {
				"host_time": np.array(12.5),
				"rigol_c2_metadata_json": np.frombuffer(b'{"scale":2}', dtype=np.uint8),
				"rigol_c2_samples": np.arange(6, dtype=np.uint16),
			}
			send_arrays(left, expected.items())
			actual = receive_message(right)
		finally:
			left.close()
			right.close()
		self.assertEqual(set(actual), set(expected))
		for name in expected:
			np.testing.assert_array_equal(actual[name], expected[name])

	def test_connection_information_remains_encrypted(self):
		private_key = nacl.public.PrivateKey.generate()
		expected = {"id": "singlesocket", "host": "127.0.0.1", "port": 5000}
		envelope = json.loads(
			encrypt_connection_info(expected, private_key.public_key)
		)
		self.assertEqual(envelope["format"], "lapd-curve25519-sealed-box-v1")
		self.assertEqual(envelope["format"], CONNECTION_FILE_FORMAT)
		self.assertEqual(decrypt_connection_info(envelope, private_key), expected)


@unittest.skipUnless(ADIOS2_AVAILABLE, "adios2 is not installed")
class AdiosOutputTests(unittest.TestCase):
	def test_variables_can_appear_after_the_first_step(self):
		import adios2

		with tempfile.TemporaryDirectory() as directory:
			path = str(Path(directory) / "raw.bp")
			output = AdiosIO(
				SimpleNamespace(destination=path, engine="BP5", append_output=False)
			)
			output.write_data({"a": np.arange(3, dtype=np.int16)})
			output.write_data(
				{
					"a": np.arange(5, dtype=np.int16),
					"recovered_channel": np.arange(2, dtype=np.uint16),
				}
			)
			output.close()

			reader = adios2.FileReader(path)
			try:
				available = reader.available_variables()
			finally:
				reader.close()
			self.assertEqual(available["a"]["AvailableStepsCount"], "2")
			self.assertNotEqual(available["a"]["Shape"], "")
			self.assertEqual(
				available["recovered_channel"]["AvailableStepsCount"], "1"
			)

	def test_an_existing_directory_gets_timestamped_files_but_a_bp_output_does_not(self):
		with tempfile.TemporaryDirectory() as directory:
			opener = IO._output_opener(SimpleNamespace(destination=directory, engine="BP5"), None)
			self.assertIsInstance(opener(), RotatingConsumerOutput)
			existing = Path(directory) / "raw.bp"
			existing.mkdir()  # a BP5 output is a directory too
			output = IO._output_opener(SimpleNamespace(destination=str(existing), engine="BP5"), None)()
			try:
				self.assertTrue((Path(directory) / "raw-1.bp").exists())
			finally:
				output.close()


def _shot():
	return RawShot(1.0, {}, {}, {}, 0.1)


class _ListOutput(IO):
	"""Appends each written step to `written`; a write waits on `block` (at most 10 s), then fails while `fail`."""

	def __init__(self, written, fail=False, block=None):
		self.written, self.fail, self.block = written, fail, block
		self.closed = False
		self.writing = threading.Event()

	def write_data(self, data):
		self.writing.set()
		if self.block is not None:
			self.block.wait(10)
		if self.fail:
			raise OSError("consumer gone")
		self.written.append(data)

	def close(self):
		self.closed = True


class NeverBlockTests(unittest.TestCase):
	"""RawOutput never holds up its caller; the destination is a fake opened by the writer thread."""

	def setUp(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.timing_log = Path(tmp.name) / "timing.jsonl"
		self.settings = RawOutputSettings(destination=str(Path(tmp.name) / "unused"), timing_log=str(self.timing_log),
		                                  retry_delay_seconds=0.0)

	def _raw_output(self, open_output):
		with mock.patch.object(IO, "_output_opener", return_value=open_output):
			output = RawOutput(self.settings)
		self.addCleanup(output.close, 0)  # before the temporary directory's cleanup (LIFO)
		return output

	def _events(self):
		return [json.loads(line)["event"] for line in self.timing_log.read_text(encoding="utf-8").splitlines()]

	def test_write_returns_at_once_while_opening_hangs(self):
		release = threading.Event()
		self.addCleanup(release.set)
		written = []

		def open_output():
			release.wait(10)
			return _ListOutput(written)

		output = self._raw_output(open_output)
		start = time.perf_counter()
		for _ in range(3):
			output.write(_shot())
		self.assertLess(time.perf_counter() - start, 0.5)
		release.set()
		output.close(5)
		self.assertEqual([int(step["shot_index"]) for step in written], [0, 1, 2])  # converted on the writer
		self.assertEqual((output.written_shots, output.unsent_shots), (3, 0))

	def test_a_write_error_is_retried_on_a_new_output_without_reaching_the_caller(self):
		written, outputs = [], []

		def open_output():
			outputs.append(_ListOutput(written, fail=not outputs))  # only the first output fails
			return outputs[-1]

		output = self._raw_output(open_output)
		output.write(_shot())
		output.write(_shot())
		output.close(5)
		self.assertEqual([int(step["shot_index"]) for step in written], [0, 1])  # the failed step first, in order
		self.assertEqual(len(outputs), 2)
		self.assertTrue(outputs[0].closed)
		self.assertEqual(output.dropped_shots, 0)
		self.assertIn("io.error", self._events())

	def test_close_gives_up_after_its_timeout_and_counts_unsent_shots(self):
		release = threading.Event()
		self.addCleanup(release.set)
		fake = _ListOutput([], block=release)
		output = self._raw_output(lambda: fake)
		for _ in range(3):
			output.write(_shot())
		self.assertTrue(fake.writing.wait(5))  # the first shot is in flight
		start = time.perf_counter()
		output.close(0.2)
		self.assertLess(time.perf_counter() - start, 2.0)
		self.assertEqual(output.unsent_shots, 3)  # one in flight, two queued
		self.assertIn("io.close", self._events())
		with self.assertRaises(RuntimeError):
			output.write(_shot())


class DirectOutputNameTests(unittest.TestCase):
	def test_an_existing_path_is_never_reused(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "raw.bp"
			self.assertEqual(unused_path(path), path)
			path.mkdir()
			self.assertEqual(unused_path(path), Path(directory) / "raw-1.bp")
			(Path(directory) / "raw-1.bp").write_bytes(b"")
			self.assertEqual(unused_path(path), Path(directory) / "raw-2.bp")

	def test_directory_output_rotates_and_never_reuses_a_name(self):
		# Pickle files without adios2, BP5 directories with it; the names follow the same rule.
		with tempfile.TemporaryDirectory() as directory:
			now = [0.0]
			stamp = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)  # every name would collide

			def rotating():
				return RotatingConsumerOutput(directory, "BP5", interval_seconds=10, clock=lambda: now[0],
				                              timestamp_factory=lambda: stamp)

			step = {"a": np.arange(3, dtype=np.int16)}
			output = rotating()
			output.write_data(step)
			now[0] = 5.0
			output.write_data(step)
			self.assertEqual(len(list(Path(directory).iterdir())), 1)
			now[0] = 10.0
			output.write_data(step)
			output.close()
			restarted = rotating()
			restarted.write_data(step)
			restarted.close()
			self.assertEqual(len(list(Path(directory).iterdir())), 3)


class SettingsFromEnvTests(unittest.TestCase):
	def test_unset_or_empty_destination_gives_none(self):
		self.assertIsNone(settings_from_env("RAW", "t.jsonl", environ={}))
		self.assertIsNone(settings_from_env("RAW", "t.jsonl", environ={"RAW": "", "RAW_BUFFER_S": "60"}))

	def test_variables_override_defaults(self):
		environ = {"RAW": "/data/raw/", "RAW_PRIVATE_KEY": "k.key", "RAW_COMPRESSION": "c.conf", "RAW_BUFFER_S": "90",
		           "RAW_FILE_S": "300", "RAW_RETRY_S": "0", "RAW_SOCKET_TIMEOUT_S": "10"}
		self.assertEqual(
			settings_from_env("RAW", Path("t.jsonl"), environ=environ, buffer_seconds=60.0, retry_delay_seconds=5.0),
			RawOutputSettings(destination="/data/raw/", timing_log="t.jsonl", private_key="k.key",
			                  compression_config="c.conf", buffer_seconds=90.0, file_interval_seconds=300.0,
			                  retry_delay_seconds=0.0, socket_timeout_seconds=10.0))
		defaults = settings_from_env("RAW", "t.jsonl", environ={"RAW": "raw.bp"}, buffer_seconds=60.0)
		self.assertEqual((defaults.buffer_seconds, defaults.retry_delay_seconds), (60.0, 3.0))

	def test_invalid_values_are_rejected(self):
		with self.assertRaises(ValueError):
			settings_from_env("RAW", "t.jsonl", environ={"RAW": "raw.bp", "RAW_BUFFER_S": "0"})
		with self.assertRaises(ValueError):
			settings_from_env("RAW", "t.jsonl", environ={"RAW": "raw.bp", "RAW_RETRY_S": "-1"})


if __name__ == "__main__":
	unittest.main()
