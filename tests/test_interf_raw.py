"""interf_raw without scopes: RIGOL_IP empty disables the Rigol; the per-shot on/off check; the channel settings."""
import logging
import os
import subprocess
import sys
import unittest
from unittest import mock

from lab_scopes.errors import ScopeTimeoutError

import interf_raw
from interf_raw import AcqState, StepTimes
from ioc_harness import REPO


class DisabledRigolTests(unittest.TestCase):
	def test_an_empty_rigol_ip_never_contacts_the_rigol(self):
		state, missing, times = AcqState(rigol_skip_shots=3), {}, StepTimes()
		with mock.patch.object(interf_raw, "RIGOL_IP", ""), \
				mock.patch.object(interf_raw, "RigolDHO800", side_effect=AssertionError("Rigol contacted")) as rigol, \
				mock.patch.object(interf_raw, "_lecroy_open", side_effect=OSError("no LeCroy")):
			self.assertEqual(interf_raw._read_rigol(state, missing, times), {})
			with self.assertLogs("interf_raw", logging.ERROR) as logs:
				interf_raw.release_scopes()
		rigol.assert_not_called()
		self.assertEqual(missing, {"rigol": "disabled (RIGOL_IP empty)"})  # P40's missing reason in SHOT:MISSING
		self.assertEqual((times.rigol, state.rigol_skip_shots), ("disabled", 3))  # no backoff counting either
		self.assertEqual(len(logs.records), 1, logs.output)  # the LeCroy's NORM only, no Rigol RUN attempt


class _Scope:
	"""The LeCroyScope surface _consume uses. `trace` is {ch: :TRACE? reply, or an exception it raises}; others ON."""

	def __init__(self, **trace):
		self.trace = trace
		self.scope = self  # LeCroyScope.scope: the transport _lecroy_displayed queries
		self.acquired = []
		self.armed = 0

	def query(self, cmd):
		reply = self.trace.get(cmd.removesuffix(":TRACE?"), "ON")
		if isinstance(reply, Exception):
			raise reply
		return reply

	def acquire(self, ch, raw=False):
		self.acquired.append(ch)
		return (ch, b"wavedesc")

	def arm_master_single(self, channel=None):
		self.armed += 1


class OnOffCheckTests(unittest.TestCase):
	def _consume(self, scope):
		with mock.patch.object(interf_raw, "LECROY_CHANNELS", ("C1", "C2", "C3", "C4")), \
				mock.patch.object(interf_raw, "RIGOL_IP", ""):
			times = StepTimes()  # channels_s is keyed by LECROY_CHANNELS at construction
			shot =interf_raw._consume(scope, AcqState(lecroy_armed=True), lambda: False, times)
		return shot, times

	def test_an_off_channel_is_not_read_and_the_others_are(self):
		scope = _Scope(C3="OFF")
		shot, times = self._consume(scope)
		self.assertEqual(scope.acquired, ["C1", "C2", "C4"])
		self.assertEqual(sorted(shot.lecroy), ["C1", "C2", "C4"])
		self.assertEqual(shot.missing["lecroy"], "C3 trace off")
		self.assertIsNone(times.channels_s["C3"])
		self.assertIsNotNone(times.onoff_s)
		self.assertEqual(scope.armed, 1)  # an off channel is no link error

	def test_a_timeout_or_unexpected_reply_in_the_check_reads_no_channel_and_skips_the_rearm(self):
		cases = {
			"ScopeTimeoutError: no reply": _Scope(C2=ScopeTimeoutError("no reply")),  # not counted as off
			"ScopeProtocolError: C3:TRACE? replied 'GARBLED', neither ON nor OFF": _Scope(C3="garbled"),
		}
		for reason, scope in cases.items():
			with self.subTest(reason):
				shot, times = self._consume(scope)
				self.assertEqual(scope.acquired, [])
				self.assertEqual(shot.lecroy, {})
				self.assertEqual(shot.missing["lecroy"],
				                 f"C1 on/off check failed: {reason}; C2 not read; C3 not read; C4 not read")
				self.assertEqual(scope.armed, 0)
				self.assertIsNone(times.rearm_s)
				self.assertIsNotNone(times.onoff_s)


class ChannelConfigTests(unittest.TestCase):
	def test_channels_within_the_count_pass(self):
		for channels, n in ((("C1", "C2", "C3", "C4"), 4), (("C1", "C8"), 8), (("C2",), 2)):
			with self.subTest(channels=channels, n=n):
				interf_raw._check_lecroy_channels(channels, n)

	def test_a_channel_beyond_the_count_or_a_bad_count_is_refused(self):
		for channels, n, expected in ((("C1", "C5", "C6"), 4, "'C5', 'C6' not among C1..C4"),
		                              (("C1", "c2"), 4, "'c2' not among C1..C4"),
		                              (("C1",), 3, "LECROY_N_CHANNELS=3 must be one of")):
			with self.subTest(channels=channels, n=n):
				with self.assertRaises(ValueError) as caught:
					interf_raw._check_lecroy_channels(channels, n)
				self.assertIn(expected, str(caught.exception))
				self.assertIn("LECROY_N_CHANNELS", str(caught.exception))

	def test_import_fails_before_any_scope_contact(self):
		env = dict(os.environ, LECROY_CHANNELS="C1,C2,C5", LECROY_N_CHANNELS="4", LECROY_IP="192.0.2.1")  # TEST-NET-1
		done = subprocess.run([sys.executable, "-c", "import interf_raw"], cwd=REPO, env=env, capture_output=True,
		                      text=True, timeout=60)
		self.assertNotEqual(done.returncode, 0)
		self.assertIn("ValueError: LECROY_CHANNELS=C1,C2,C5: 'C5' not among C1..C4 (LECROY_N_CHANNELS=4)", done.stderr)


if __name__ == "__main__":
	unittest.main()
