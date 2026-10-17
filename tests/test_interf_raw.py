"""interf_raw without scopes: RIGOL_IP empty disables the Rigol."""
import logging
import unittest
from unittest import mock

import interf_raw
from interf_raw import AcqState, StepTimes


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


if __name__ == "__main__":
	unittest.main()
