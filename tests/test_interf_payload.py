import dataclasses
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

import interf_payload
from interf_analysis import analyze_shot
from interf_archive import ADIOS2_AVAILABLE, iter_steps
from interf_sim.synthetic import make_raw_shot
from streamer import RawOutput
from streamer.arguments import RawOutputSettings
from streamer.payload import shot_variables
from test_interf_analysis import HOST_TIME, REPO_ROOT, _assert_channels_equal
# The live path: everything IocLink, the IOC host and its modules import. C5 adds interf_ioc.
LIVE_MODULES = ["diag_ioc", "diag_ioc.framing", "diag_ioc.host", "diag_ioc.link", "diag_ioc.module", "diag_ioc.network",
                "diag_ioc.outage", "diag_ioc.records", "interf_analysis", "interf_payload", "interf_shot"]


def _shot(shot_number=3, **kw):
	return make_raw_shot(8192, 1e-8, noise_v=0.01, host_time=HOST_TIME, rng=np.random.default_rng(0), shot_number=shot_number, **kw)


class RoundTripTests(unittest.TestCase):
	def test_decode_inverts_encode(self):
		for rigol in (True, False):
			with self.subTest(rigol=rigol):
				shot = _shot(rigol=rigol)
				decoded = interf_payload.decode(interf_payload.encode(shot))
				self.assertEqual(decoded.schema_version, interf_payload.SCHEMA_VERSION)
				self.assertEqual((decoded.shot_date, decoded.shot_number, decoded.shot_time, decoded.time_source),
				                 (shot.shot_date, shot.shot_number, shot.shot_time, shot.time_source))
				self.assertEqual((decoded.host_time, decoded.critical_path_s, decoded.missing),
				                 (shot.host_time, shot.critical_path_s, shot.missing))
				_assert_channels_equal(self, decoded.lecroy, shot.lecroy)
				_assert_channels_equal(self, decoded.rigol, shot.rigol)

	def test_analysis_of_the_decoded_shot_matches_and_keeps_the_identity(self):
		shot = _shot()
		direct, via_link = analyze_shot(shot), analyze_shot(interf_payload.decode(interf_payload.encode(shot)))
		self.assertEqual((via_link.shot_date, via_link.shot_number, via_link.shot_time, via_link.time_source),
		                 (shot.shot_date, 3, HOST_TIME, "trigger"))
		for name, port in direct.ports.items():
			np.testing.assert_array_equal(via_link.ports[name].ne, port.ne)

	def test_host_time_source_round_trips(self):
		shot = dataclasses.replace(_shot(), time_source="host")
		self.assertEqual(interf_payload.decode(interf_payload.encode(shot)).time_source, "host")

	def test_scalars_of_shape_one_decode(self):
		variables = interf_payload.encode(_shot())
		for name in ("schema_version", "shot_date", "shot_number", "shot_time", "host_time", "critical_path_s"):
			variables[name] = variables[name].reshape(1)
		self.assertEqual(interf_payload.decode(variables).shot_number, 3)

	def test_shot_without_identity_is_not_encoded(self):
		with self.assertRaisesRegex(ValueError, "no identity"):
			interf_payload.encode(_shot(shot_number=None))

	def test_other_schema_versions_are_rejected(self):
		variables = interf_payload.encode(_shot())
		for version in (1, interf_payload.SCHEMA_VERSION + 1):  # 1: a streamer archive step
			variables["schema_version"] = np.array(version, dtype=np.uint16)
			with self.subTest(version=version), self.assertRaisesRegex(ValueError, "schema_version"):
				interf_payload.decode(variables)


class IndependenceTests(unittest.TestCase):
	def test_the_live_path_loads_no_streamer_module(self):
		code = ("import importlib, sys\n"
		        f"for name in {LIVE_MODULES!r}:\n"
		        "    importlib.import_module(name)\n"
		        "print(sorted(m for m in sys.modules if m == 'streamer' or m.startswith('streamer.')))\n")
		done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
		self.assertEqual(done.returncode, 0, done.stderr)
		self.assertEqual(done.stdout.strip(), "[]")


class ArchiveUnchangedTests(unittest.TestCase):
	"""The shot identity is live-only: the ADIOS archive keeps streamer's schema 1, byte for byte."""

	def test_identity_does_not_reach_the_archive_encoding(self):
		identified, bare = _shot(), _shot(shot_number=None)
		archived, expected = shot_variables(identified, 5), shot_variables(bare, 5)
		self.assertEqual(set(archived), set(expected))
		for name, array in expected.items():
			self.assertEqual(archived[name].dtype, array.dtype)
			np.testing.assert_array_equal(archived[name], array)

	@unittest.skipUnless(ADIOS2_AVAILABLE, "adios2 is not installed")
	def test_raw_output_archives_the_schema_one_steps(self):
		shots = [_shot(shot_number=i, rigol=i % 2 == 0) for i in range(3)]
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "raw.bp"
			with RawOutput(RawOutputSettings(destination=str(path), timing_log=str(Path(tmp) / "timing.jsonl"))) as output:
				for shot in shots:
					output.write(shot)
			steps = list(iter_steps(path))
		self.assertEqual(len(steps), len(shots))
		for i, (shot, step) in enumerate(zip(shots, steps)):
			with self.subTest(step=i):
				expected = shot_variables(dataclasses.replace(shot, shot_date=None, shot_number=None, shot_time=None,
				                                              time_source=None), i)
				self.assertEqual(set(step), set(expected))
				for name, array in expected.items():
					np.testing.assert_array_equal(np.asarray(step[name]).reshape(array.shape), array)


if __name__ == "__main__":
	unittest.main()
