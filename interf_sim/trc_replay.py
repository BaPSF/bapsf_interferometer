"""Scope fakes that replay .trc files through the unmodified interf_raw and interf_main.

The fakes replace the lab_scopes classes interf_raw calls (`interf_raw.LeCroyScope`,
`interf_raw.RigolDHO800`), not the SCPI wire. FakeLeCroyScope subclasses LeCroyScope and overrides
only the methods interf_raw calls that would reach the scope, so decoding and validation are the
driver's own; any other call that reaches the transport raises SimFault.

What a run cannot show:
- No Rigol: every Rigol connection fails, so every shot has missing["rigol"].
- Transfers cost only the file read, so RawShot.critical_path_s is shorter than on the scopes.
- Trigger times come from the recorded WAVEDESCs, so the dt that interf_main logs follows the
  recording, not `shot_period`.
- The patches are per process: a Rigol moved into a worker process would bypass them.
"""
import math
import os
import re
import signal
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from lab_scopes.errors import ScopeConnectionError
from lab_scopes.io.lecroy_files import TRACE_DATA_OFFSET, TRC_BLOCK_PREFIX_BYTES
from lab_scopes.lecroy import LeCroyNoDataError, LeCroyScope, LeCroyWavedesc, wavedesc_trigger_timestamp

import interf_main
import interf_raw

# TRC_DIR = Path("D:/data/raw data")  # recorded shots on this PC; on Linux edit this line or pass --trc-dir
TRC_DIR = Path("/home/adios/shared/Software/LAPD/data")

# LeCroy auto-save name <channel>-<title>-shot<counter>.trc. The counter wraps (the recording in
# TRC_DIR runs 68015..99998, then 0..57507), so shots are ordered by trigger time, never by counter.
_TRC_NAME = re.compile(r"(C[1-8])-interf-shot(\d+)\.trc", re.IGNORECASE)
# LeCroyScope.acquire_bytes/parse_wavedesc hardcode a 15-byte :WAVEFORM? preamble; a .trc prefix is shorter.
_PREAMBLE_PAD = b"\0" * (15 - TRC_BLOCK_PREFIX_BYTES)
_PROGRESS_EVERY = 5000  # header reads between progress lines


class SimFault(BaseException):
	"""The replay cannot stay faithful: a driver call the fake does not emulate, or a capture read twice.

	BaseException so interf_raw's `except Exception` scope-error handlers cannot log it as a scope
	failure and retry forever.
	"""


def trc_shots(directory=TRC_DIR):
	"""[(counter, {ch: Path})] for every shot with a file for any interf_raw.LECROY_CHANNELS, in trigger-time order.

	A channel without a file is served as LeCroyNoDataError, as the scope does for a trace without data.
	"""
	shots = {}
	with os.scandir(directory) as entries:
		for entry in entries:
			m = _TRC_NAME.fullmatch(entry.name)
			if m and (ch := m[1].upper()) in interf_raw.LECROY_CHANNELS:
				shots.setdefault(int(m[2]), {})[ch] = Path(entry.path)
	if not shots:
		raise FileNotFoundError(f"no files matching {_TRC_NAME.pattern} for {interf_raw.LECROY_CHANNELS} in {directory}")
	print(f"trc_shots: reading the trigger times of {len(shots)} shots in {directory}", file=sys.stderr, flush=True)
	keyed = []
	for i, (counter, files) in enumerate(shots.items(), 1):
		path = files[min(files)]
		with open(path, "rb", buffering=0) as f:  # unbuffered: a buffered read() fetches 128 KiB, not 357 bytes
			t = wavedesc_trigger_timestamp(LeCroyWavedesc(_wavedesc(f.read(TRACE_DATA_OFFSET), path)).wd)
		if t is None:
			raise ValueError(f"{path}: WAVEDESC has no trigger time, so the shot cannot be ordered")
		keyed.append((t, counter, files))
		if i % _PROGRESS_EVERY == 0:
			print(f"trc_shots: {i}/{len(shots)}", file=sys.stderr, flush=True)
	keyed.sort(key=lambda k: k[:2])
	return [(counter, files) for _, counter, files in keyed]


def repeat_trc_shots(shots, limit):
	"""Exactly `limit` synthetic shots made by cycling `shots` with consecutive counters.

	The first synthetic counter is the first source counter. Each synthetic shot gets its own channel
	dictionary, but its paths still refer to the source .trc files. The WAVEDESC trigger timestamps in
	the files are unchanged.
	"""
	if not shots:
		raise ValueError("cannot repeat an empty shot list")
	if limit < 0:
		raise ValueError("repeat limit cannot be negative")
	first_counter = shots[0][0]
	return [
		(first_counter + i, dict(shots[i % len(shots)][1]))
		for i in range(limit)
	]


def _wavedesc(content, path):
	if len(content) < TRACE_DATA_OFFSET or content[:2] != b"#9" \
			or content[TRC_BLOCK_PREFIX_BYTES:TRC_BLOCK_PREFIX_BYTES + 8] != b"WAVEDESC":
		raise ValueError(f"{path}: not a LeCroy .trc file")
	return content[TRC_BLOCK_PREFIX_BYTES:TRACE_DATA_OFFSET]


class ReplayLeCroy:
	"""Simulated LeCroy; like the real scope, its state outlives each connection.

	The machine triggers every `shot_period` s (0: at every arm). A SINGLE-armed scope captures the
	first trigger at or after its arm, so an iteration slower than the period skips a trigger, as on
	the machine. Each capture loads the next entry of `shots`, so no recorded shot is skipped.
	"""

	def __init__(self, shots, shot_period=0.0):
		self.shots = list(shots)  # [(counter, {ch: Path})], see trc_shots
		self.shot_period = shot_period
		self.on_exhausted = None  # called once when armed with no shot left
		self.captured = []  # counters served, in capture order
		self.mode = "NORM"  # TRIG_MODE? reply
		self.sweeps = 0
		self.record = {}  # {ch: Path} of the last capture; kept until the next, like scope memory
		self.read = set()  # channels of `record` already acquired: same-shot rule 3 allows one read
		self._armed_at = 0.0
		self._clock0 = time.monotonic()
		self._trigger_at = None

	@property
	def exhausted(self):
		return len(self.captured) == len(self.shots)

	def set_mode(self, mode):
		self.mode = mode
		if mode == "SINGLE":
			self._armed_at = time.monotonic()
			if self.shot_period > 0:
				n = math.ceil((self._armed_at - self._clock0) / self.shot_period)
				self._trigger_at = self._clock0 + n * self.shot_period
			else:
				self._trigger_at = None

	def time_until_trigger(self):
		"""Seconds until the armed trigger; None when no timed trigger is pending."""
		if self.mode != "SINGLE" or self.exhausted or self._trigger_at is None:
			return None
		return max(0.0, self._trigger_at - time.monotonic())

	def update(self):
		"""Capture a machine trigger that has occurred since the SINGLE arm."""
		if self.mode != "SINGLE":
			return
		if self.exhausted:
			notify, self.on_exhausted = self.on_exhausted, None
			if notify is not None:
				notify()
			return
		if self._trigger_at is not None and time.monotonic() < self._trigger_at:
			return
		counter, self.record = self.shots[len(self.captured)]
		self.captured.append(counter)
		self.read = set()
		self.mode = "STOP"
		self.sweeps += 1


class FakeLeCroyScope(LeCroyScope):
	"""LeCroyScope connected to the ReplayLeCroy at `ipv4_addr` instead of a scope."""

	network = {}  # {ip: ReplayLeCroy}; simulated_scopes() patches it

	def __init__(self, ipv4_addr, verbose=True, timeout=5.0, port=1861, transport=None, discover_traces="channels"):
		# LeCroyScope.__init__ is skipped (it probes the scope); its class-level defaults cover the rest.
		lecroy = self.network.get(ipv4_addr)
		if lecroy is None:
			raise ScopeConnectionError(f"cannot connect to {ipv4_addr}:{port}: no simulated LeCroy there")
		if isinstance(discover_traces, str) and discover_traces != "channels":
			raise SimFault(f"LeCroyScope(discover_traces={discover_traces!r})")
		self.verbose = verbose
		self.rm_status = True
		self.valid_trace_names = self.channel_names if discover_traces == "channels" else tuple(discover_traces)
		self.scope = _Transport(lecroy)

	def arm_master_single(self, channel=None):
		channel = self._resolve_ref_channel(channel)
		lecroy = self.scope.live()
		lecroy.sweeps = 0  # CLEAR_SWEEPS
		lecroy.set_mode("SINGLE")
		return channel

	def sweeps_per_acq(self, channel):
		self.validate_channel(channel)
		return self.scope.live().sweeps

	def wait_for_stop_then_complete(self, channel, timeout=100, poll=0.02):
		self.validate_channel(channel)
		t_end = time.monotonic() + timeout
		while time.monotonic() < t_end:
			lecroy = self.scope.live()
			if lecroy.mode == "STOP" and lecroy.sweeps >= 1:
				return True
			left = t_end - time.monotonic()
			if left <= 0:
				break
			until_trigger = lecroy.time_until_trigger()
			# Wait for the absolute cadence deadline, not one full shot period after the
			# preceding iteration. The timeout still bounds stop-request latency.
			time.sleep(min(left, until_trigger) if until_trigger is not None else min(left, poll))
		return False

	def acquire(self, trace, seg=0, raw=False):
		trace = self.validate_trace(trace)
		lecroy = self.scope.live()
		if trace in lecroy.read:
			raise SimFault(f"{trace} of shot {lecroy.captured[-1]} read twice (same-shot rule 3)")
		result = super().acquire(trace, seg, raw)
		lecroy.read.add(trace)
		return result

	def acquire_bytes(self, trace, seg=0):
		trace = self.validate_trace(trace)
		if seg != 0:
			raise SimFault(f"acquire(seg={seg}): sequence mode")
		path = self.scope.live().record.get(trace)
		if path is None:
			raise LeCroyNoDataError(f"{trace}: no .trc file for this shot")
		content = path.read_bytes()
		return _PREAMBLE_PAD + content, _wavedesc(content, path)

	def set_trigger_mode(self, trigger_mode):
		lecroy = self.scope.live()
		previous = lecroy.mode
		if trigger_mode in ("AUTO", "NORM", "SINGLE", "STOP"):
			lecroy.set_mode(trigger_mode)
		return previous


class _Transport:
	"""LeCroyScope.scope: answers only TRIG_MODE? (read by interf_raw directly) and close()."""

	def __init__(self, lecroy):
		self._lecroy = lecroy
		self.closed = False

	def live(self):
		if self.closed:
			raise ScopeConnectionError("simulated LeCroy connection is closed")
		self._lecroy.update()
		return self._lecroy

	def query(self, cmd):
		if cmd != "TRIG_MODE?":
			raise SimFault(f"LeCroy query {cmd!r}")
		return self.live().mode

	def close(self):
		self.closed = True

	def __getattr__(self, name):
		if name.startswith("__"):  # copy/pickle/inspect probes expect AttributeError
			raise AttributeError(name)
		raise SimFault(f"LeCroy transport .{name}: override the driver method that sent it in FakeLeCroyScope")


class FakeRigolDHO800:
	"""Stands in for lab_scopes RigolDHO800 with no Rigol on the network: every connection fails."""

	def __init__(self, ip, port=5555, timeout=5.0, verbose=True):
		raise ScopeConnectionError(f"cannot connect to scope at {ip}:{port}: no simulated Rigol")


@contextmanager
def simulated_scopes(lecroy):
	"""Put `lecroy` at interf_raw.LECROY_IP and swap interf_raw's scope classes for the fakes."""
	# patch.object raises if interf_raw no longer imports a name, so a rename cannot leave a real
	# driver reaching the network.
	with mock.patch.object(FakeLeCroyScope, "network", {interf_raw.LECROY_IP: lecroy}), \
			mock.patch.object(interf_raw, "LeCroyScope", FakeLeCroyScope), \
			mock.patch.object(interf_raw, "RigolDHO800", FakeRigolDHO800):
		yield


def iter_shots(lecroy):
	"""RawShots from the unmodified interf_raw.acquire_shot() until `lecroy` has served every shot.

	Raises RuntimeError when successive calls capture nothing (a LeCroy error loop) rather than
	retrying forever.
	"""
	# A capture needs ceil(shot_period / TRIGGER_TIMEOUT) calls; one more allows a pause on the boundary.
	max_misses = math.ceil(lecroy.shot_period / interf_raw.TRIGGER_TIMEOUT) + 1
	with simulated_scopes(lecroy):
		state = interf_raw.AcqState()
		misses = 0
		try:
			while not lecroy.exhausted:
				shot = interf_raw.acquire_shot(state, lambda: False)
				if shot is not None:
					misses = 0
					yield shot
				else:
					misses += 1
					if misses > max_misses:
						raise RuntimeError(f"no capture in {misses} acquire_shot() calls; see the interf_raw warnings")
		finally:
			interf_raw.release_scopes()


def run_main(lecroy, log_dir):
	"""interf_main.main() unmodified on the fakes; returns after `lecroy` has served every shot.

	Exhaustion sends SIGINT, as an operator's Ctrl-C, so main's stop and release path runs. The stop
	flag and signal handlers main changes are restored, so it can run again in the same process.
	"""
	lecroy.on_exhausted = lambda: signal.raise_signal(signal.SIGINT)
	handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
	try:
		with simulated_scopes(lecroy), mock.patch.object(interf_main, "LOG_DIR", str(log_dir)), \
				mock.patch.object(interf_main, "_stop", False):
			interf_main.main()
	finally:
		for s, handler in handlers.items():
			signal.signal(s, handler)
