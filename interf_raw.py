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
from dataclasses import dataclass, field

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
# Given, the driver skips its channel-count probe, which costs a full LECROY_TIMEOUT per open on the HDO4104
# (C5:TRACE? never answers). _check_lecroy_channels bounds LECROY_CHANNELS by it.
LECROY_N_CHANNELS = _env("LECROY_N_CHANNELS", 4)
LECROY_RETRY_INTERVAL = _env("LECROY_RETRY_INTERVAL", 5.0)  # s between attempts while the LeCroy errors
TRIGGER_TIMEOUT = _env("TRIGGER_TIMEOUT", 10.0)  # s; > 3 shot periods, so a timeout means a pause

RIGOL_IP = _env("RIGOL_IP", "192.168.7.63")  # "": Rigol disabled, never contacted (missing["rigol"] says so)
RIGOL_DISABLED = "disabled (RIGOL_IP empty)"
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


def _check_lecroy_channels(channels, n_channels):
	"""ValueError unless `n_channels` is 2, 4 or 8 and every entry of `channels` is one of C1..C<n_channels>."""
	# Run at import: a channel the scope lacks never answers, so in the loop it would cost a LECROY_TIMEOUT at every
	# open (trace discovery of discover_traces=LECROY_CHANNELS) and every shot (the on/off check).
	if n_channels not in LeCroyScope._VALID_CHANNEL_COUNTS:
		raise ValueError(f"LECROY_N_CHANNELS={n_channels} must be one of {LeCroyScope._VALID_CHANNEL_COUNTS}")
	valid = {f"C{i}" for i in range(1, n_channels + 1)}
	if bad := [ch for ch in channels if ch not in valid]:
		raise ValueError(f"LECROY_CHANNELS={','.join(channels)}: {', '.join(map(repr, bad))} not among C1..C{n_channels}"
		                 f" (LECROY_N_CHANNELS={n_channels})")


_check_lecroy_channels(LECROY_CHANNELS, LECROY_N_CHANNELS)
#===============================================================================================================================================


@dataclass
class AcqState:
	"""Scope state carried between acquire_shot() calls; plain data so a parent process can own it."""
	lecroy_armed: bool = False  # armed (or seen SINGLE) and its capture not yet consumed
	rigol_skip_shots: int = 0  # backoff: shots left before the Rigol is used again
	idle_kind: str | None = None  # throttled idle logging, see _note_idle
	idle_since: float = 0.0
	idle_logged: float = 0.0
	last_detected: float | None = None  # perf_counter() at the last capture detection, for StepTimes.cycle_s


@dataclass
class StepTimes:
	"""s per step of the acquire_shot() call that captured a shot, by time.perf_counter(); None: the step did not run."""
	open_s: float | None = None
	prepare_s: float | None = None
	prepare_armed: bool = False  # _prepare armed the LeCroy (else it was SINGLE already, or a capture was pending)
	wait_s: float | None = None  # end of _prepare -> capture detected
	onoff_s: float | None = None  # _lecroy_displayed(), before the channel reads; timed even when it raised
	# Every LECROY_CHANNELS entry, in order; None for a channel that failed or was not read.
	channels_s: dict[str, float | None] = field(default_factory=lambda: dict.fromkeys(LECROY_CHANNELS))
	rigol_s: float | None = None
	rigol: str | None = None  # "tried", "skipped" (backoff) or "disabled" (RIGOL_IP empty)
	rearm_s: float | None = None  # None: not re-armed (stop request or link error); a failed re-arm is timed
	close_s: float | None = None
	cycle_s: float | None = None  # the previous capture detection -> this one; None for the first


@dataclass
class RawShot:
	host_time: float  # time.time() when the LeCroy capture was detected
	lecroy: dict[str, tuple[np.ndarray, bytes]]  # {ch: (int16 samples, 346-byte WAVEDESC)}; channels read
	rigol: dict[str, tuple[np.ndarray, dict]]  # {ch: (uint16 12-bit codes, calibration metadata)}; {} if missing
	missing: dict[str, str]  # {"lecroy" | "rigol": reason}; "lecroy" can coexist with partial lecroy data
	critical_path_s: float  # capture detected -> re-armed (or not re-armed, on a stop request)
	# Shot identity, assigned by interf_main after the re-arm (interf_shot; docs/ARCHITECTURE.md D14, D15). Not
	# in raw output: streamer.payload ignores these, so the ADIOS output is unchanged.
	shot_date: int | None = None  # YYYYMMDD, America/Los_Angeles
	shot_number: int | None = None  # from 0 each shot_date
	shot_time: float | None = None  # epoch s: the LeCroy trigger time, or host_time when time_source is "host"
	time_source: str | None = None  # "trigger" | "host"
	# Logged by interf_main only: interf_payload and streamer.payload encode named fields, so neither carries it.
	timing: StepTimes = field(default_factory=StepTimes)
	png: dict | None = None  # PngMonitor.latest() at capture detection; None without PNG. Raw output only (D16)


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
	return LeCroyScope(LECROY_IP, verbose=False, timeout=LECROY_TIMEOUT, discover_traces=LECROY_CHANNELS,
	                   n_channels=LECROY_N_CHANNELS)


def _lecroy_mode(lecroy):
	"""TRIG_MODE? truncated to SIN, STO, NOR, or AUT."""
	return lecroy.scope.query("TRIG_MODE?").strip()[:3].upper()


def _lecroy_displayed(lecroy):
	"""LECROY_CHANNELS entries whose :TRACE? reads ON; a reply neither ON nor OFF raises ScopeProtocolError."""
	# Not LeCroyScope.displayed_channels(): it counts a timed-out reply as off, and a late reply would then answer
	# the next query. Every error propagates here, so it takes the link-error path.
	displayed = []
	for ch in LECROY_CHANNELS:
		reply = lecroy.scope.query(f"{ch}:TRACE?").strip().upper()
		if reply.startswith("ON"):
			displayed.append(ch)
		elif not reply.startswith("OFF"):
			raise ScopeProtocolError(f"{ch}:TRACE? replied {reply!r}, neither ON nor OFF")
	return displayed


def _arm_lecroy(lecroy, state):
	# CLEAR_SWEEPS + SINGLE: exactly one trigger-out, and a zeroed counter so a stale STOP never
	# reads as fresh.
	lecroy.arm_master_single(LECROY_CHANNELS[0])
	state.lecroy_armed = True


def _prepare(lecroy, state):
	"""Get the LeCroy listening without disturbing a pending capture; True if it armed the LeCroy."""
	mode = _lecroy_mode(lecroy)
	if mode == "SIN":
		# Waiting for the next shot: re-arming would only clear the sweep counter for nothing.
		state.lecroy_armed = True
		return False
	if mode == "STO" and state.lecroy_armed and lecroy.sweeps_per_acq(LECROY_CHANNELS[0]) >= 1:
		return False  # an unconsumed capture is waiting (it may have landed after last iteration's wait)
	if mode in ("STO", "NOR", "AUT"):
		# STO with nothing to consume (rule 3): already consumed, predates us, or stopped by hand.
		# NOR/AUT: rule 2.
		_arm_lecroy(lecroy, state)
		return True
	raise RuntimeError(f"unexpected LeCroy TRIG_MODE {mode!r}")


def _arm_and_wait(lecroy, state, stop_requested, times):
	"""True once a fresh capture is present; False on TRIGGER_TIMEOUT or a stop request.

	Raises on LeCroy communication errors.
	"""
	t0 = time.perf_counter()
	times.prepare_armed = _prepare(lecroy, state)
	t_prepared = time.perf_counter()
	times.prepare_s = t_prepared - t0
	t_end = time.monotonic() + TRIGGER_TIMEOUT
	while time.monotonic() < t_end:
		if stop_requested():
			return False
		if lecroy.wait_for_stop_then_complete(LECROY_CHANNELS[0], timeout=_WAIT_SLICE_S):
			detected = time.perf_counter()
			times.wait_s = detected - t_prepared
			if state.last_detected is not None:
				times.cycle_s = detected - state.last_detected
			state.last_detected = detected
			return True
	# No state change: the next iteration's _prepare re-classifies the LeCroy.
	_note_idle(state, "no trigger", f"none within {TRIGGER_TIMEOUT:g} s")
	return False


def _read_rigol(state, missing, times):
	"""Rigol data for this shot, or {} with missing["rigol"] set."""
	if not RIGOL_IP:
		times.rigol = "disabled"
		missing["rigol"] = RIGOL_DISABLED
		return {}
	if state.rigol_skip_shots > 0:
		times.rigol = "skipped"
		state.rigol_skip_shots -= 1
		missing["rigol"] = f"backoff, {state.rigol_skip_shots} shots left"
		return {}
	times.rigol = "tried"
	try:
		return rigol_read(RIGOL_IP, (RIGOL_REF_CH, RIGOL_PLA_CH), RIGOL_OPERATION_TIMEOUT)
	except _RIGOL_ERRORS as e:
		state.rigol_skip_shots = RIGOL_RETRY_INTERVAL
		missing["rigol"] = f"read failed ({e}); skipping Rigol for {RIGOL_RETRY_INTERVAL} shots"
	return {}


def _consume(lecroy, state, stop_requested, times, read_png=None):
	"""Read a detected capture from both scopes, then re-arm the LeCroy."""
	host_time, t0 = time.time(), time.monotonic()
	try:
		png = read_png() if read_png else None  # beside host_time, before scope I/O: a slow transfer must not shift it (D16)
	except Exception:
		png = None  # PngMonitor.latest() never raises; another read_png must not cost the shot
	_note_resumed(state)
	# Consume before reading: an exception anywhere below must never lead to a re-read (rule 3).
	state.lecroy_armed = False

	missing = {}
	lecroy_data = {}
	failed = {}  # {ch: reason}
	link_ok = True
	t = time.perf_counter()
	try:
		displayed = _lecroy_displayed(lecroy)
	except _LECROY_LINK_ERRORS as e:
		# As a link error in the reads below: no channel is read, and no re-arm.
		failed[LECROY_CHANNELS[0]] = f"on/off check failed: {type(e).__name__}: {e}"
		failed.update((rest, "not read") for rest in LECROY_CHANNELS[1:])
		link_ok = False
	times.onoff_s = time.perf_counter() - t
	for i, ch in enumerate(LECROY_CHANNELS if link_ok else ()):
		if ch not in displayed:
			failed[ch] = "trace off"
			continue
		t = time.perf_counter()
		try:
			lecroy_data[ch] = lecroy.acquire(ch, raw=True)
		except _LECROY_LINK_ERRORS as e:
			failed[ch] = f"{type(e).__name__}: {e}"
			failed.update((rest, "not read") for rest in LECROY_CHANNELS[i + 1:])
			link_ok = False
			break
		except Exception as e:
			# One channel without data (e.g. a displayed trace with no capture) must not cost the other port.
			failed[ch] = f"{type(e).__name__}: {e}"
		else:
			times.channels_s[ch] = time.perf_counter() - t
	if failed:
		missing["lecroy"] = "; ".join(f"{ch} {why}" for ch, why in failed.items())
	# Rule 1: the Rigol :STOP must precede the LeCroy re-arm below.
	t = time.perf_counter()
	rigol_data = _read_rigol(state, missing, times)
	times.rigol_s = time.perf_counter() - t

	# Not re-armed on a stop request (release_scopes() sets NORM), nor after a link error (the
	# next iteration reconnects and re-arms).
	if not stop_requested() and link_ok:
		t = time.perf_counter()
		try:
			_arm_lecroy(lecroy, state)
		except Exception as e:
			log.warning("LeCroy re-arm failed (%s); next iteration re-arms", e)
		times.rearm_s = time.perf_counter() - t
	return RawShot(host_time, lecroy_data, rigol_data, missing, time.monotonic() - t0, png=png)


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


def acquire_shot(state, stop_requested, read_png=None):
	"""One iteration: a RawShot if a LeCroy capture was consumed, else None (pause, LeCroy error, stop).

	`stop_requested()` is polled between waits; once it is true the scopes are left un-re-armed
	and the caller must call release_scopes() before exiting. `read_png()`, if given, is called once
	per capture for RawShot.png and must not block (interf_png.PngMonitor.latest).
	"""
	times = StepTimes()
	t = time.perf_counter()
	try:
		lecroy = _lecroy_open()
	except Exception as e:
		_lecroy_failed(state, e, stop_requested)
		return None
	times.open_s = time.perf_counter() - t
	shot = None
	try:
		try:
			captured = _arm_and_wait(lecroy, state, stop_requested, times)
		except Exception as e:
			lecroy.rm_close()  # before the retry sleep
			_lecroy_failed(state, e, stop_requested)
			return None
		# Outside the except above: _consume handles scope errors itself, so anything it raises is
		# a bug for the caller to log with a traceback, not a LeCroy error.
		if captured:
			shot = _consume(lecroy, state, stop_requested, times, read_png)
	finally:
		t = time.perf_counter()
		lecroy.rm_close()
		times.close_s = time.perf_counter() - t
	if shot is not None:
		shot.timing = times
	return shot


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
	if not RIGOL_IP:
		return
	try:
		rigol_run(RIGOL_IP, RIGOL_OPERATION_TIMEOUT)  # normally already running; covers a failed read
	except _RIGOL_ERRORS as e:
		log.error("Rigol not set to RUN: %s", e)
