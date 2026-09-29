"""interf_raw and interf_main behavior, run unmodified on the fakes.

These pin current acquisition behavior; when a bench result changes interf_raw or interf_main,
update the expectation here in the same change.
"""
import logging
import signal
import threading

import numpy as np

import interf_main
import interf_raw
from interf_sim.scopes import ReplayLeCroy, iter_shots, run_main, trc_shots

WRAPPED = [99998, 99999, 0, 1]  # counter order of a recording that wrapped, in trigger order
MAIN_TIMEOUT_S = 30.0  # watchdog; a healthy run_main on 3 shots takes well under 1 s


def test_every_shot_is_served_once_in_trigger_order(make_trc_dir):
	directory, expected = make_trc_dir(WRAPPED)
	lecroy = ReplayLeCroy(trc_shots(directory))
	shots = list(iter_shots(lecroy))
	assert lecroy.captured == WRAPPED
	assert len(shots) == len(WRAPPED)
	for counter, shot in zip(lecroy.captured, shots):
		assert list(shot.lecroy) == list(interf_raw.LECROY_CHANNELS)
		for ch, (samples, _) in shot.lecroy.items():
			assert samples.dtype == np.int16
			np.testing.assert_array_equal(samples, expected[counter, ch])
		assert shot.rigol == {}
		assert set(shot.missing) == {"rigol"}


def test_rigol_is_missing_every_shot_with_backoff(make_trc_dir, monkeypatch):
	monkeypatch.setattr(interf_raw, "RIGOL_RETRY_INTERVAL", 2)
	directory, _ = make_trc_dir(range(5))
	reasons = [shot.missing["rigol"] for shot in iter_shots(ReplayLeCroy(trc_shots(directory)))]
	assert reasons[0].startswith("read failed (ScopeConnectionError")
	assert reasons[1:3] == ["backoff, 1 shots left", "backoff, 0 shots left"]
	assert reasons[3].startswith("read failed (ScopeConnectionError")
	assert reasons[4] == "backoff, 1 shots left"


def test_a_channel_without_data_costs_only_that_channel(make_trc_dir):
	directory, _ = make_trc_dir([0, 1])
	(directory / "C3-interf-shot00001.trc").unlink()
	shots = list(iter_shots(ReplayLeCroy(trc_shots(directory))))
	assert "lecroy" not in shots[0].missing
	assert list(shots[1].lecroy) == ["C1", "C2", "C4"]
	assert shots[1].missing["lecroy"].startswith("C3 LeCroyNoDataError")


def test_main_logs_each_shot_then_stops_and_releases(make_trc_dir, tmp_path, caplog):
	directory, _ = make_trc_dir([10, 11, 12], spacing=2.5)
	lecroy = ReplayLeCroy(trc_shots(directory))
	caplog.set_level(logging.INFO)
	handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
	watchdog = threading.Timer(MAIN_TIMEOUT_S, signal.raise_signal, (signal.SIGINT,))
	watchdog.start()
	try:
		run_main(lecroy, tmp_path / "log")
	finally:
		watchdog.cancel()
	assert not interf_main._stop and {s: signal.getsignal(s) for s in handlers} == handlers  # rerunnable
	assert lecroy.captured == [10, 11, 12]
	assert lecroy.mode == "NORM"  # release_scopes()
	lines = [r.getMessage() for r in caplog.records if r.name == "interf_main" and r.getMessage().startswith("shot host")]
	assert len(lines) == 3
	assert " dt - " in lines[0]
	assert all(" dt 2.500 s " in line for line in lines[1:])  # recorded spacing, not shot_period
	assert all("LeCroy C1:64 C2:64 C3:64 C4:64" in line and "missing rigol: " in line for line in lines)
	assert any(r.getMessage().startswith("Rigol not set to RUN") for r in caplog.records)
