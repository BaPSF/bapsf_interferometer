import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from lab_scopes.lecroy import LeCroyWavedesc, wavedesc_trigger_timestamp

from interf_analysis import (FT_len, OFFSET_WINDOWS, analyze_shot, get_calibration_factor, lecroy_trace, phase_from_raw,
                             rigol_trace)
from interf_sim.synthetic import gaussian_phase, make_raw_shot, make_wavedesc, synthetic_shots, write_trc_shots
from interf_sim.trc_replay import ReplayLeCroy, iter_shots, trc_shots
from streamer.adios_io import ADIOS2_AVAILABLE, AdiosIO, iter_steps, read_step
from streamer.payload import SCHEMA_VERSION, shot_from_variables, shot_variables

N = FT_len * 200
DT = 1e-8
PHASE = gaussian_phase(N * DT / 2, N * DT / 8)
HOST_TIME = 1_700_000_000.25
REPO_ROOT = Path(__file__).resolve().parent.parent
WINDOW_MS = (0.4, 0.6)  # around the bump centre of the 1.024 ms record


def _shot(**kw):
	return make_raw_shot(N, DT, phase=PHASE, noise_v=0.01, host_time=HOST_TIME, rng=np.random.default_rng(0), **kw)


def _assert_channels_equal(test, got, want):
	"""{ch: (samples, wavedesc | metadata)} dicts, as on RawShot.lecroy / .rigol."""
	test.assertEqual(set(got), set(want))
	for ch, (samples, info) in want.items():
		np.testing.assert_array_equal(got[ch][0], samples)
		test.assertEqual(got[ch][1], info)


class TraceTests(unittest.TestCase):
	def test_lecroy_trace_matches_wavedesc_formula(self):
		wavedesc = make_wavedesc(1000, 1e-6, -1e-4, gain=0.01, offset=0.2)
		samples = np.arange(-500, 500, dtype=np.int16)
		t, volts = lecroy_trace(samples, wavedesc)
		wd = LeCroyWavedesc(wavedesc).wd
		self.assertEqual(wd.vertical_gain, float(np.float32(0.01)))  # float32 field
		np.testing.assert_allclose(volts, wd.vertical_gain * samples - wd.vertical_offset, rtol=1e-12)
		np.testing.assert_allclose(t, wd.horiz_offset + np.arange(1000) * wd.horiz_interval, rtol=1e-12, atol=1e-15)

	def test_lecroy_trace_trims_to_the_shorter_length(self):
		t, volts = lecroy_trace(np.zeros(900, dtype=np.int16), make_wavedesc(1000, 1e-6))
		self.assertEqual((t.size, volts.size), (900, 900))

	def test_rigol_trace_matches_guide_formula(self):
		md = {"x_increment": 2e-8, "x_origin": -1e-3, "x_reference": 3.0,
		      "y_increment": 1 / 2048, "y_origin": 5.0, "y_reference": 2048.0}
		codes = np.array([0, 2048, 4095], dtype=np.uint16)
		t, volts = rigol_trace(codes, md)
		np.testing.assert_allclose(volts, (codes - 5.0 - 2048.0) / 2048)
		np.testing.assert_allclose(t, -1e-3 + (np.arange(3) - 3.0) * 2e-8)

	def test_wavedesc_trigger_time_round_trips(self):
		wd = LeCroyWavedesc(make_wavedesc(10, 1e-6, trigger_time=HOST_TIME)).wd
		self.assertAlmostEqual(wavedesc_trigger_timestamp(wd), HOST_TIME, places=6)


class AnalyzeShotTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.shot = _shot()
		cls.result = analyze_shot(cls.shot, ne_window_ms=WINDOW_MS)

	def test_phase_recovered_on_every_port(self):
		dts = {"P20": DT, "P29": DT, "P40": self.shot.rigol["C1"][1]["x_increment"]}
		for name, dt in dts.items():
			with self.subTest(port=name):
				port = self.result.ports[name]
				self.assertIsNone(port.missing)
				# A window's CSD phase is that of its centre; phase_from_raw stamps the window start and
				# subtracts the mean of the first 5 windows.
				expected = PHASE(port.t_ms / 1e3 + FT_len * dt / 2)
				expected -= expected[:5].mean()
				rms = np.sqrt(np.mean((port.phase - expected)[1:-1] ** 2))
				self.assertLess(rms, 0.05)
				self.assertGreater(port.phase.max(), 5.0)  # the bump was unwrapped, not folded into (-π, π]

	def test_density_is_phase_times_calibration(self):
		for port in self.result.ports.values():
			with self.subTest(port=port.name):
				self.assertEqual(port.cal, get_calibration_factor(port.freq_hz, 0.4))
				np.testing.assert_array_equal(port.ne, port.phase * port.cal)
				self.assertEqual(port.decimation, 1)

	def test_ne_mean_averages_the_window(self):
		for port in self.result.ports.values():
			with self.subTest(port=port.name):
				inside = (port.t_ms >= WINDOW_MS[0]) & (port.t_ms <= WINDOW_MS[1])
				self.assertGreater(inside.sum(), 1)
				self.assertEqual(port.ne_mean, port.ne[inside].mean())

	def test_ne_mean_is_nan_without_a_full_window(self):
		record_ms = N * DT * 1e3
		for window in (None, (record_ms / 2, record_ms * 2), (0.5, 0.5001)):  # unset, past the trace, between points
			with self.subTest(window=window):
				self.assertTrue(math.isnan(analyze_shot(self.shot, ne_window_ms=window).ports["P20"].ne_mean))

	def test_reversed_window_is_rejected(self):
		with self.assertRaises(ValueError):
			analyze_shot(self.shot, ne_window_ms=(0.6, 0.4))

	def test_matches_main_branch_pipeline(self):
		# main (bench-validated): read_trc_data_simplified / RigolDHO800.read_channel volts, minus each
		# channel's mean, then phase_from_raw; P20 = C1/C2, P29 = C3/C4, P40 = Rigol C1/C2.
		for name, scope, ref_ch, pla_ch, trace in (("P20", "lecroy", "C1", "C2", lecroy_trace),
		                                           ("P29", "lecroy", "C3", "C4", lecroy_trace),
		                                           ("P40", "rigol", "C1", "C2", rigol_trace)):
			with self.subTest(port=name):
				t_s, ref = trace(*getattr(self.shot, scope)[ref_ch])
				_, pla = trace(*getattr(self.shot, scope)[pla_ch])
				t_ms, phase = phase_from_raw(t_s, ref - np.mean(ref), pla - np.mean(pla))
				np.testing.assert_array_equal(self.result.ports[name].t_ms, t_ms)
				np.testing.assert_array_equal(self.result.ports[name].phase, phase)

	def test_raw_shot_has_no_shot_index(self):
		self.assertIsNone(self.result.shot_index)
		self.assertEqual(self.result.host_time, HOST_TIME)

	def test_missing_rigol_marks_only_p40(self):
		shot = _shot(rigol=False)
		result = analyze_shot(shot)
		p40 = result.ports["P40"]
		self.assertEqual(p40.missing, shot.missing["rigol"])
		self.assertEqual((p40.t_ms.size, p40.phase.size, p40.ne.size), (0, 0, 0))
		self.assertTrue(math.isnan(p40.ne_mean))
		self.assertEqual(result.acq_missing, shot.missing)
		self.assertIsNone(result.ports["P20"].missing)
		self.assertIsNone(result.ports["P29"].missing)

	def test_absent_channel_takes_the_acquisition_reason(self):
		shot = _shot()
		del shot.lecroy["C4"]
		shot.missing["lecroy"] = "C4 not read"
		result = analyze_shot(shot)
		self.assertEqual(result.ports["P29"].missing, "C4 not read")
		self.assertIsNone(result.ports["P20"].missing)

	def test_channel_mapping_mismatch_names_the_channels_present(self):
		shot = _shot()
		shot.rigol = {"C3": shot.rigol["C1"], "C4": shot.rigol["C2"]}  # e.g. RIGOL_REF_CH=C3 in acquisition
		self.assertEqual(analyze_shot(shot).ports["P40"].missing, "C1/C2 not acquired; rigol has C3, C4")

	def test_analysis_error_is_confined_to_its_port(self):
		shot = _shot()
		shot.lecroy["C1"] = (shot.lecroy["C1"][0], b"bad")
		result = analyze_shot(shot)
		self.assertTrue(result.ports["P20"].missing.startswith("analysis error: "))
		self.assertIsNone(result.ports["P29"].missing)

	def test_flat_channel_marks_only_its_port_missing(self):
		for channel, port, other in (("C2", "P20", "P29"), ("C1", "P20", "P29"), ("C4", "P29", "P20")):
			with self.subTest(flat=channel):
				result = analyze_shot(_shot(flat=[channel]), ne_window_ms=WINDOW_MS)
				self.assertEqual(result.ports[port].missing, f"{channel} flat (no signal)")
				self.assertEqual(result.ports[port].ne.size, 0)
				self.assertTrue(math.isnan(result.ports[port].ne_mean))
				self.assertIsNone(result.ports[other].missing)
				self.assertIsNone(result.ports["P40"].missing)

	def test_too_short_trace_has_a_clear_reason(self):
		limit = OFFSET_WINDOWS * FT_len  # 2560
		short = make_raw_shot(2048, DT, noise_v=0.01, host_time=HOST_TIME, rng=np.random.default_rng(0))
		self.assertEqual(analyze_shot(short).ports["P20"].missing, f"trace too short: 2048 samples, need > {limit}")
		# The bound is exact: one sample more gives OFFSET_WINDOWS full windows.
		just_enough = make_raw_shot(limit + 1, DT, noise_v=0.01, host_time=HOST_TIME, rng=np.random.default_rng(0))
		port = analyze_shot(just_enough).ports["P20"]
		self.assertIsNone(port.missing)
		self.assertEqual(port.t_ms.size, OFFSET_WINDOWS)

	def test_max_points_strides_the_arrays(self):
		full = self.result.ports["P20"]
		port = analyze_shot(self.shot, max_points=30, ne_window_ms=WINDOW_MS).ports["P20"]
		k = math.ceil(full.t_ms.size / 30)
		self.assertEqual(port.decimation, k)
		self.assertLessEqual(port.t_ms.size, 30)
		np.testing.assert_array_equal(port.t_ms, full.t_ms[::k])
		np.testing.assert_array_equal(port.ne, full.ne[::k])
		self.assertEqual(port.ne_mean, full.ne_mean)  # averaged before striding


class PayloadRoundTripTests(unittest.TestCase):
	def test_shot_from_variables_inverts_shot_variables(self):
		shot = _shot()
		decoded = shot_from_variables(shot_variables(shot, 7))
		self.assertEqual((decoded.schema_version, decoded.shot_index), (SCHEMA_VERSION, 7))
		self.assertEqual((decoded.host_time, decoded.critical_path_s), (shot.host_time, shot.critical_path_s))
		self.assertEqual(decoded.missing, shot.missing)
		_assert_channels_equal(self, decoded.lecroy, shot.lecroy)
		_assert_channels_equal(self, decoded.rigol, shot.rigol)

		direct, via_payload = analyze_shot(shot), analyze_shot(decoded)
		self.assertEqual(via_payload.shot_index, 7)
		for name, port in direct.ports.items():
			np.testing.assert_array_equal(via_payload.ports[name].ne, port.ne)

	def test_scalars_of_shape_one_decode(self):
		shot = _shot()
		variables = shot_variables(shot, 7)
		for name in ("schema_version", "shot_index", "host_time", "critical_path_s"):
			variables[name] = variables[name].reshape(1)  # as adios2 FileReader returns them
		decoded = shot_from_variables(variables)
		self.assertEqual((decoded.shot_index, decoded.host_time, decoded.critical_path_s),
		                 (7, shot.host_time, shot.critical_path_s))

	@unittest.skipUnless(ADIOS2_AVAILABLE, "adios2 is not installed")
	def test_archived_steps_with_changing_layout_decode(self):
		# As in a real archive, the layout changes per step: record length and missing_json length
		# vary, the Rigol is absent from odd steps, and C4 is absent from step 2.
		layouts = [(8192, True), (4096, False), (8192, True), (6144, False)]
		shots = [make_raw_shot(n, DT, noise_v=0.01, host_time=HOST_TIME + 3 * i, rigol=rigol, rng=np.random.default_rng(i))
		         for i, (n, rigol) in enumerate(layouts)]
		del shots[2].lecroy["C4"]
		shots[2].missing["lecroy"] = "C4 LeCroyNoDataError: C4: no .trc file for this shot"
		for engine in ("BP5", "BP4"):  # BP4 keeps a variable's selection from step to step
			with self.subTest(engine=engine):
				with tempfile.TemporaryDirectory() as tmp:
					path = Path(tmp) / "raw.bp"
					output = AdiosIO(SimpleNamespace(destination=str(path), engine=engine, append_output=False))
					for i, shot in enumerate(shots):
						output.write_data(shot_variables(shot, i))
					output.close()
					steps = list(iter_steps(path))
					last = read_step(path, len(shots) - 1)
					with self.assertRaises(IndexError):
						read_step(path, len(shots))
				self.assertEqual(len(steps), len(shots))
				for i, (shot, variables) in enumerate(zip(shots, steps)):
					with self.subTest(step=i):
						decoded = shot_from_variables(variables)
						self.assertEqual((decoded.shot_index, decoded.host_time, decoded.missing),
						                 (i, shot.host_time, shot.missing))
						_assert_channels_equal(self, decoded.lecroy, shot.lecroy)
						_assert_channels_equal(self, decoded.rigol, shot.rigol)
						via_archive = analyze_shot(decoded)
						for name, port in analyze_shot(shot).ports.items():
							np.testing.assert_array_equal(via_archive.ports[name].ne, port.ne)
				self.assertEqual(set(last), set(steps[-1]))

	@unittest.skipUnless(ADIOS2_AVAILABLE, "adios2 is not installed")
	def test_reading_stops_at_the_last_step_of_a_file_not_closed(self):
		# While acquisition runs, or after it died without closing the file, the end of the file is
		# never marked: reading must return the steps written so far, not wait for the next one. A
		# wait holds the GIL, so the reader runs in a subprocess, where a regression times out.
		reader = ("import sys\n"
		          "from streamer.adios_io import iter_steps, read_step\n"
		          "from streamer.payload import shot_from_variables\n"
		          "print([shot_from_variables(variables).shot_index for variables in iter_steps(sys.argv[1])])\n"
		          "try:\n"
		          "    read_step(sys.argv[1], 2)\n"
		          "except IndexError:\n"
		          "    print('IndexError')\n")
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "raw.bp"
			output = AdiosIO(SimpleNamespace(destination=str(path), engine="BP5", append_output=False))
			try:
				for i in range(2):
					output.write_data(shot_variables(_shot(rigol=False), i))
				done = subprocess.run([sys.executable, "-c", reader, str(path)], cwd=REPO_ROOT, capture_output=True,
				                      text=True, timeout=30)
			finally:
				output.close()
		self.assertEqual(done.stdout.splitlines(), ["[0, 1]", "IndexError"], done.stderr)

	def test_unknown_schema_version_is_rejected(self):
		variables = shot_variables(_shot(rigol=False), 0)
		variables["schema_version"] = np.array(SCHEMA_VERSION + 1, dtype=np.uint16)
		with self.assertRaises(ValueError):
			shot_from_variables(variables)


class SyntheticTrcTests(unittest.TestCase):
	def test_trc_files_replay_through_the_driver(self):
		shots = list(synthetic_shots(3, period=3.0, start_time=HOST_TIME, n=4096, dt=DT, noise_v=0.01,
		                             rigol=False, rng=np.random.default_rng(1)))
		with tempfile.TemporaryDirectory() as tmp:
			# Counters wrap as on the scope, so only trigger-time order gives 99998, 99999, 0.
			write_trc_shots(tmp, shots[:2], first_counter=99998)
			write_trc_shots(tmp, shots[2:], first_counter=0)
			ordered = trc_shots(tmp)
			self.assertEqual([counter for counter, _ in ordered], [99998, 99999, 0])
			replayed = list(iter_shots(ReplayLeCroy(ordered)))
		self.assertEqual(len(replayed), 3)
		for original, shot in zip(shots, replayed):
			_assert_channels_equal(self, shot.lecroy, original.lecroy)
			self.assertIn("rigol", shot.missing)


if __name__ == "__main__":
	unittest.main()
