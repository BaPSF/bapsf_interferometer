# -*- coding: utf-8 -*-
"""Same-shot raw acquisition from the interferometer scopes. Linux main thread only (SIGALRM).

The LeCroy (ports 20, 29) is the trigger master; its trigger-out is hard-wired to the Rigol
DHO804 (port 40) trigger input; software assumes this wiring and does not verify it. The Rigol
free-runs in AUTO sweep and is never armed. Same-shot rules, cited by number below:
  1. The Rigol :STOP precedes the LeCroy re-arm: a captured SINGLE LeCroy emits no further
     trigger-out until re-armed, so the Rigol sees no newer triggered shot before it stops.
  2. A free-running LeCroy (NORM/AUTO) is switched to SINGLE; free-running, it emits a
     trigger-out every shot, breaking rule 1.
  3. A LeCroy capture is read at most once: `lecroy_armed` is cleared when a capture is consumed
     and set only by a successful arm, so a failed re-arm re-arms, never re-reads.
A missed shot is skipped, never back-filled.

Kept so the Rigol can move into a worker process (Python 3.14 Linux defaults to forkserver,
which pickles the target and arguments): each Rigol function opens and closes its own
connection under one `_deadline`; the LeCroy connection lives within one `acquire_shot()` call;
no connection object is module-wide; scope state is the plain `AcqState` dataclass owned by
the caller.
"""
import logging
import os
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass

import numpy as np
from lab_scopes.errors import ScopeConnectionError, ScopeProtocolError, ScopeTimeoutError
from lab_scopes.lecroy import LeCroyScope
from lab_scopes.rigol import RigolDHO800

log = logging.getLogger(__name__)


def _env(name, default):
	"""Env var `name` converted to type(default) (tuple: comma-separated), else `default`."""
	raw = os.environ.get(name)
	if raw is None:
		return default
	if isinstance(default, tuple):
		return tuple(s.strip() for s in raw.split(","))
	return type(default)(raw)


#===============================================================================================================================================
LECROY_IP = _env("LECROY_IP", "10.10.10.10")  # placeholder until the real address is known
LECROY_CHANNELS = _env("LECROY_CHANNELS", ("C1", "C2", "C3", "C4"))  # [0] carries the sweep counter
LECROY_TIMEOUT = _env("LECROY_TIMEOUT", 5.0)  # s, VICP socket timeout; bounds every LeCroy call
LECROY_RETRY_INTERVAL = _env("LECROY_RETRY_INTERVAL", 5.0)  # s between attempts while the LeCroy errors
TRIGGER_TIMEOUT = _env("TRIGGER_TIMEOUT", 10.0)  # s; > 3 shot periods, so a timeout means a pause

RIGOL_IP = _env("RIGOL_IP", "192.168.7.63")
RIGOL_REF_CH = _env("RIGOL_REF_CH", "C1")
RIGOL_PLA_CH = _env("RIGOL_PLA_CH", "C2")
RIGOL_RETRY_INTERVAL = _env("RIGOL_RETRY_INTERVAL", 100)  # shots the Rigol is skipped after a failure
RIGOL_CONNECT_TIMEOUT = _env("RIGOL_CONNECT_TIMEOUT", 1.0)  # s, TCP connect
# s deadline for rigol_read (also rigol_run on exit). Covers BOTH channel reads: WORD runs
# ~2.6 MB/s on the DHO804 (fw 00.01.05), ~0.77 s per 1M-point channel, so keep the Rigol at
# <= 1M points or every read overruns and the Rigol is dropped. A slow-but-alive read is
# abandoned like a stalled one.
RIGOL_OPERATION_TIMEOUT = _env("RIGOL_OPERATION_TIMEOUT", 2.5)

_WAIT_SLICE_S = 0.5  # stop-request latency while waiting for a trigger or a retry
_IDLE_LOG_INTERVAL_S = 300.0
# LeCroy errors after which the VICP stream may hold a late or partial reply, so the connection
# is not used again this iteration. Any other error comes after a complete reply.
_LECROY_LINK_ERRORS = (ScopeConnectionError, ScopeTimeoutError, ScopeProtocolError, OSError)
#===============================================================================================================================================


@dataclass
class AcqState:
	"""Scope state carried between acquire_shot() calls; plain data so a parent process can own it."""
	lecroy_armed: bool = False  # armed (or seen SINGLE) and its capture not yet consumed
	rigol_skip_shots: int = 0  # backoff: shots left before the Rigol is used again
	idle_kind: str | None = None  # throttled idle logging, see _note_idle
	idle_since: float = 0.0
	idle_logged: float = 0.0


@dataclass
class RawShot:
	host_time: float  # time.time() when the LeCroy capture was detected
	lecroy: dict[str, tuple[np.ndarray, bytes]]  # {ch: (int16 samples, 346-byte WAVEDESC)}; channels read
	rigol: dict[str, tuple[np.ndarray, dict]]  # {ch: (uint16 12-bit codes, calibration metadata)}; {} if missing
	missing: dict[str, str]  # {"lecroy" | "rigol": reason}; "lecroy" can coexist with partial lecroy data
	critical_path_s: float  # capture detected -> re-armed (or not re-armed, on a stop request)


#===============================================================================================================================================
# Rigol

class RigolDeadline(BaseException):
	"""A Rigol operation overran its wall-clock budget."""
	# BaseException: rigol_functions.command() retries on `except Exception`, which would swallow
	# an Exception subclass and keep waiting.


_RIGOL_ERRORS = (Exception, RigolDeadline)  # every Rigol call site catches this, never bare Exception


@contextmanager
def _deadline(seconds):
	"""Raise RigolDeadline if the block runs longer than `seconds`. Main thread only."""
	# The driver's own waits (15 s per query, >= 15 s per chunk) are not configurable. A raising
	# handler interrupts the blocking select/recv; PEP 475 retries the call only when it returns.
	def expire(signum, frame):
		raise RigolDeadline(f"deadline {seconds:g} s exceeded")
	previous = signal.signal(signal.SIGALRM, expire)
	signal.setitimer(signal.ITIMER_REAL, seconds)
	try:
		yield
	finally:
		signal.setitimer(signal.ITIMER_REAL, 0)
		signal.signal(signal.SIGALRM, previous)


def _rigol_open(ip):
	return RigolDHO800(ip, timeout=RIGOL_CONNECT_TIMEOUT, verbose=False)


def rigol_read(ip, channels, budget):
	""":STOP, read `channels`, :RUN -> {ch: (uint16 codes, metadata)}.

	Caveat: the Rigol free-runs in AUTO sweep, so a forced (untriggered) acquisition between the
	LeCroy edge and :STOP would replace the shot, and nothing here can detect that. Pending bench
	check.
	"""
	with _deadline(budget), _rigol_open(ip) as scope:
		scope.stop()
		try:
			data = {}
			for ch in channels:
				wf = scope.read_channel(ch, fmt="WORD")
				data[ch] = (wf.raw, wf.metadata)
			return data
		finally:
			# Resume free-run even after a failed read; a failed :RUN must not discard data read.
			try:
				scope.run()
			except Exception as e:
				log.warning("Rigol :RUN after read failed: %s", e)


def rigol_run(ip, budget):
	with _deadline(budget), _rigol_open(ip) as scope:
		scope.run()


def _lecroy_open():
	# One connection per acquire_shot(): a VICP link held open across shots is not safe.
	return LeCroyScope(LECROY_IP, verbose=False, timeout=LECROY_TIMEOUT, discover_traces=LECROY_CHANNELS)


def _lecroy_mode(lecroy):
	"""TRIG_MODE? truncated to SIN, STO, NOR, or AUT."""
	return lecroy.scope.query("TRIG_MODE?").strip()[:3].upper()


def _arm_lecroy(lecroy, state):
	# CLEAR_SWEEPS + SINGLE: exactly one trigger-out, and a zeroed counter so a stale STOP never
	# reads as fresh.
	lecroy.arm_master_single(LECROY_CHANNELS[0])
	state.lecroy_armed = True


def _prepare(lecroy, state):
	"""Get the LeCroy listening without disturbing a pending capture."""
	mode = _lecroy_mode(lecroy)
	if mode == "SIN":
		# Waiting for the next shot: re-arming would only clear the sweep counter for nothing.
		state.lecroy_armed = True
	elif mode == "STO" and state.lecroy_armed and lecroy.sweeps_per_acq(LECROY_CHANNELS[0]) >= 1:
		pass  # an unconsumed capture is waiting (it may have landed after last iteration's wait)
	elif mode in ("STO", "NOR", "AUT"):
		# STO with nothing to consume (rule 3): already consumed, predates us, or stopped by hand.
		# NOR/AUT: rule 2.
		_arm_lecroy(lecroy, state)
	else:
		raise RuntimeError(f"unexpected LeCroy TRIG_MODE {mode!r}")


def _arm_and_wait(lecroy, state, stop_requested):
	"""True once a fresh capture is present; False on TRIGGER_TIMEOUT or a stop request.

	Raises on LeCroy communication errors.
	"""
	_prepare(lecroy, state)
	t_end = time.monotonic() + TRIGGER_TIMEOUT
	while time.monotonic() < t_end:
		if stop_requested():
			return False
		if lecroy.wait_for_stop_then_complete(LECROY_CHANNELS[0], timeout=_WAIT_SLICE_S):
			return True
	# No state change: the next iteration's _prepare re-classifies the LeCroy.
	_note_idle(state, "no trigger", f"none within {TRIGGER_TIMEOUT:g} s")
	return False


def _read_rigol(state, missing):
	"""Rigol data for this shot, or {} with missing["rigol"] set."""
	if state.rigol_skip_shots > 0:
		state.rigol_skip_shots -= 1
		missing["rigol"] = f"backoff, {state.rigol_skip_shots} shots left"
		return {}
	try:
		return rigol_read(RIGOL_IP, (RIGOL_REF_CH, RIGOL_PLA_CH), RIGOL_OPERATION_TIMEOUT)
	except _RIGOL_ERRORS as e:
		state.rigol_skip_shots = RIGOL_RETRY_INTERVAL
		missing["rigol"] = f"read failed ({e}); skipping Rigol for {RIGOL_RETRY_INTERVAL} shots"
	return {}


def _consume(lecroy, state, stop_requested):
	"""Read a detected capture from both scopes, then re-arm the LeCroy."""
	host_time, t0 = time.time(), time.monotonic()
	_note_resumed(state)
	# Consume before reading: an exception anywhere below must never lead to a re-read (rule 3).
	state.lecroy_armed = False

	missing = {}
	lecroy_data = {}
	failed = {}  # {ch: reason}
	link_ok = True
	for i, ch in enumerate(LECROY_CHANNELS):
		try:
			lecroy_data[ch] = lecroy.acquire(ch, raw=True)
		except _LECROY_LINK_ERRORS as e:
			failed[ch] = f"{type(e).__name__}: {e}"
			failed.update((rest, "not read") for rest in LECROY_CHANNELS[i + 1:])
			link_ok = False
			break
		except Exception as e:
			# One channel without data (e.g. trace switched off) must not cost the other port.
			failed[ch] = f"{type(e).__name__}: {e}"
	if failed:
		missing["lecroy"] = "; ".join(f"{ch} {why}" for ch, why in failed.items())
	# Rule 1: the Rigol :STOP must precede the LeCroy re-arm below.
	rigol_data = _read_rigol(state, missing)

	# Not re-armed on a stop request (release_scopes() sets NORM), nor after a link error (the
	# next iteration reconnects and re-arms).
	if not stop_requested() and link_ok:
		try:
			_arm_lecroy(lecroy, state)
		except Exception as e:
			log.warning("LeCroy re-arm failed (%s); next iteration re-arms", e)
	return RawShot(host_time, lecroy_data, rigol_data, missing, time.monotonic() - t0)


def _note_idle(state, kind, detail):
	"""Log an idle condition when it starts or changes kind, then every _IDLE_LOG_INTERVAL_S."""
	now = time.monotonic()
	if state.idle_kind is None:
		state.idle_since = now
	elif state.idle_kind == kind and now - state.idle_logged < _IDLE_LOG_INTERVAL_S:
		return
	state.idle_kind, state.idle_logged = kind, now
	log.warning("%s: %s (idle %.0f s)", kind, detail, now - state.idle_since)


def _note_resumed(state):
	if state.idle_kind is not None:
		log.info("capture resumed after %.0f s idle", time.monotonic() - state.idle_since)
		state.idle_kind = None


def _sleep_unless_stopped(seconds, stop_requested):
	t_end = time.monotonic() + seconds
	while not stop_requested() and (left := t_end - time.monotonic()) > 0:
		time.sleep(min(_WAIT_SLICE_S, left))


def _lecroy_failed(state, e, stop_requested):
	_note_idle(state, "LeCroy error", f"{type(e).__name__}: {e}")
	_sleep_unless_stopped(LECROY_RETRY_INTERVAL, stop_requested)


def acquire_shot(state, stop_requested):
	"""One iteration: a RawShot if a LeCroy capture was consumed, else None (pause, LeCroy error, stop).

	`stop_requested()` is polled between waits; once it is true the scopes are left un-re-armed
	and the caller must call release_scopes() before exiting.
	"""
	try:
		lecroy = _lecroy_open()
	except Exception as e:
		_lecroy_failed(state, e, stop_requested)
		return None
	try:
		try:
			captured = _arm_and_wait(lecroy, state, stop_requested)
		except Exception as e:
			lecroy.rm_close()  # before the retry sleep
			_lecroy_failed(state, e, stop_requested)
			return None
		# Outside the except above: _consume handles scope errors itself, so anything it raises is
		# a bug for the caller to log with a traceback, not a LeCroy error.
		return _consume(lecroy, state, stop_requested) if captured else None
	finally:
		lecroy.rm_close()


def release_scopes():
	"""Leave both scopes free-running for exit (LeCroy NORM, Rigol RUN). Best effort; failures are logged."""
	try:
		lecroy = _lecroy_open()
		try:
			lecroy.set_trigger_mode("NORM")
		finally:
			lecroy.rm_close()
	except Exception as e:
		log.error("LeCroy not set to NORM: %s", e)
	try:
		rigol_run(RIGOL_IP, RIGOL_OPERATION_TIMEOUT)  # normally already running; covers a failed read
	except _RIGOL_ERRORS as e:
		log.error("Rigol not set to RUN: %s", e)
