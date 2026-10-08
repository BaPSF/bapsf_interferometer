import dataclasses
import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import nacl.public

import interf_main
from diag_ioc import outage
from interf_raw import RawShot
from interf_shot import ShotCounter, ShotIdentifier
from streamer.adios_io import ADIOS2_AVAILABLE, AdiosIO
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
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.identifier = ShotIdentifier(ShotCounter(Path(tmp.name) / "shot_counter.json"))

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
		info.assert_called_once()  # recovery

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
		info.assert_called_once()

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


if __name__ == "__main__":
	unittest.main()
