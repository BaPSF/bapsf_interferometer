"""The fakes against lab_scopes: signatures, decoding, and loud failure where the replay stops being faithful."""
import inspect

import numpy as np
import pytest
from lab_scopes.io.lecroy_files import TRACE_DATA_OFFSET, TRC_BLOCK_PREFIX_BYTES, read_trc_data_simplified
from lab_scopes.lecroy import LeCroyScope
from lab_scopes.rigol import RigolDHO800

import interf_raw
from interf_sim.scopes import TRC_DIR, FakeLeCroyScope, FakeRigolDHO800, ReplayLeCroy, SimFault, simulated_scopes


def _params(fn):
	# Annotations are ignored: lab_scopes has them, the fakes do not.
	return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]


@pytest.mark.parametrize("fake, real", [(FakeLeCroyScope, LeCroyScope), (FakeRigolDHO800, RigolDHO800)])
def test_fake_signatures_match_lab_scopes(fake, real):
	for name, member in vars(fake).items():
		if callable(member) and (name == "__init__" or not name.startswith("_")):
			assert _params(member) == _params(getattr(real, name)), name


def _captured_c1():
	"""A connection holding the first capture; call inside simulated_scopes()."""
	scope = interf_raw.LeCroyScope(interf_raw.LECROY_IP, discover_traces=("C1",))
	scope.arm_master_single("C1")
	assert scope.wait_for_stop_then_complete("C1", timeout=1.0)
	return scope


@pytest.fixture(params=["synthetic", "recorded"])
def trc_file(request, make_trc_dir):
	if request.param == "recorded":
		path = next(TRC_DIR.glob("C1-*.trc"), None)
		if path is None:
			pytest.skip(f"no recorded .trc files in {TRC_DIR}")
		return path
	directory, _ = make_trc_dir([0], channels=("C1",))
	return directory / "C1-interf-shot00000.trc"


def test_acquire_matches_the_lab_scopes_trc_reader(trc_file):
	with simulated_scopes(ReplayLeCroy([(0, {"C1": trc_file})])):
		volts, wavedesc = _captured_c1().acquire("C1")
	assert wavedesc == trc_file.read_bytes()[TRC_BLOCK_PREFIX_BYTES:TRACE_DATA_OFFSET]
	np.testing.assert_array_equal(volts, read_trc_data_simplified(trc_file)[0])


def test_unfaithful_replay_is_not_masked_as_a_scope_error(make_trc_dir):
	assert not issubclass(SimFault, Exception)
	directory, _ = make_trc_dir([0], channels=("C1",))
	with simulated_scopes(ReplayLeCroy([(0, {"C1": directory / "C1-interf-shot00000.trc"})])):
		scope = _captured_c1()
		with pytest.raises(SimFault):
			scope.displayed_channels()  # driver method that is not overridden reaches the transport
		scope.acquire("C1", raw=True)
		with pytest.raises(SimFault):
			scope.acquire("C1", raw=True)  # same-shot rule 3


def test_no_scope_is_reachable_but_the_simulated_lecroy():
	with simulated_scopes(ReplayLeCroy([])):
		with pytest.raises(interf_raw.ScopeConnectionError):
			interf_raw.LeCroyScope("192.0.2.1")
		with pytest.raises(interf_raw.ScopeConnectionError):
			interf_raw.RigolDHO800(interf_raw.RIGOL_IP)
