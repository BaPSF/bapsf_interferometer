# coding: utf-8
"""Acquisition loop: acquire_shot() until stopped, one log line per shot. Linux only."""
import logging.handlers
import os
import signal
import sys
import time
import weakref
from pathlib import Path

from lab_scopes.lecroy import LeCroyWavedesc, wavedesc_trigger_timestamp

from interf_raw import AcqState, acquire_shot, release_scopes

LOG_DIR = os.environ.get("INTERF_LOG_DIR", str(Path.home() / "data" / "log"))
_OUTPUT_WARN_INTERVAL_S = 300.0

log = logging.getLogger("interf_main")
_stop = False


def _request_stop(signum, frame):
	# Only sets a flag: a handler that returns lets PEP 475 resume the interrupted socket call, so an
	# in-flight transfer completes. The next Ctrl-C raises KeyboardInterrupt (forced exit).
	global _stop
	_stop = True
	signal.signal(signal.SIGINT, signal.default_int_handler)


def stop_requested():
	return _stop


def _setup_logging():
	os.makedirs(LOG_DIR, exist_ok=True)
	# Midnight rotation: ~29k lines/day, unattended for months.
	file_handler = logging.handlers.TimedRotatingFileHandler(
		os.path.join(LOG_DIR, "interf_acquire.log"), when="midnight", backupCount=30)
	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
	                    handlers=[logging.StreamHandler(sys.stdout), file_handler])


def _trigger_time(wavedesc):
	"""LeCroy trigger time from its WAVEDESC; None if unset.

	Read on the scope's own, unsynchronized clock: only differences between shots are meaningful,
	never a comparison with host time.
	"""
	return wavedesc_trigger_timestamp(LeCroyWavedesc(wavedesc).wd)


def _clock(t):
	# gmtime undoes the timegm inside wavedesc_trigger_timestamp, so this prints the scope's own
	# clock reading.
	ms = round(t * 1000)
	return time.strftime("%H:%M:%S", time.gmtime(ms // 1000)) + f".{ms % 1000:03d}"


def _shot_line(shot, trig, prev_trig):
	host = time.strftime("%H:%M:%S", time.localtime(shot.host_time))
	if trig is None:
		trig_txt = "trig ?"
	else:
		dt = f"{trig - prev_trig:.3f} s" if prev_trig is not None else "-"
		trig_txt = f"trig {_clock(trig)} dt {dt}"
	parts = [f"shot host {host}", trig_txt]
	for scope, data in (("LeCroy", shot.lecroy), ("Rigol", shot.rigol)):
		if data:
			parts.append(scope + " " + " ".join(f"{ch}:{samples.size}" for ch, (samples, _) in data.items()))
	parts.append(f"path {shot.critical_path_s:.2f} s")
	if shot.missing:
		parts.append("missing " + "; ".join(f"{k}: {v}" for k, v in shot.missing.items()))
	return " | ".join(parts)


class _OutputOutage:
	"""One output's run of failed writes: traceback at the start, a count every _OUTPUT_WARN_INTERVAL_S, info at the end."""

	def __init__(self):
		self.count = 0
		self.since = self.logged = time.monotonic()

	def failed(self, output):
		self.count += 1
		now = time.monotonic()
		if self.count == 1:
			log.exception("output %r failed; acquisition continues without it", output)
		elif now - self.logged >= _OUTPUT_WARN_INTERVAL_S:
			self.logged = now
			log.warning("output %r still failing: %d shots in %.0f s", output, self.count, now - self.since)

	def ended(self, output):
		log.info("output %r recovered after %d failed shots in %.0f s",
		         output, self.count, time.monotonic() - self.since)


# Holds only outputs in an outage. Weak keys: an output dropped mid-outage must not hand its
# state to a later object at the same id().
_outages = weakref.WeakKeyDictionary()


def _write_outputs(shot, outputs):
	for output in outputs:
		try:
			output.write(shot)
		except Exception:
			# Isolated so one broken output cannot stop the others, the shot log line, or the loop.
			_outages.setdefault(output, _OutputOutage()).failed(output)
		else:
			if (outage := _outages.pop(output, None)) is not None:
				outage.ended(output)


def _handle_shot(shot, prev_trig, outputs=()):
	"""Write the shot to every output, then log it; return the trigger time used by the next shot."""
	# Every channel of one capture shares a trigger; the first WAVEDESC stands for all.
	trig = _trigger_time(next(iter(shot.lecroy.values()))[1]) if shot.lecroy else None
	_write_outputs(shot, outputs)
	log.log(logging.WARNING if shot.missing else logging.INFO, _shot_line(shot, trig, prev_trig))
	# None after a shot without a trigger time, so the next dt prints "-" rather than spanning
	# two captures and reading as a skipped shot.
	return trig


def main(outputs=()):
	"""Acquire until stopped; each shot goes to every `outputs` item's write(shot). The caller closes them.

	Outputs must be hashable and weak-referenceable (outage tracking keys on them).
	"""
	if not hasattr(signal, "setitimer"):
		sys.exit("interf_main: Linux only (the Rigol deadline needs signal.setitimer)")
	_setup_logging()
	_outages.clear()  # an output reused from an earlier run in this process starts healthy
	signal.signal(signal.SIGINT, _request_stop)
	signal.signal(signal.SIGTERM, _request_stop)  # systemd stop

	state = AcqState()
	prev_trig = None
	log.info("acquisition started")
	try:
		while not stop_requested():
			try:
				shot = acquire_shot(state, stop_requested)
				if shot is not None:
					prev_trig = _handle_shot(shot, prev_trig, outputs)
			except Exception:
				# Scope errors are handled inside acquire_shot, so this is a bug. Log it and keep the
				# loop, so an exit still restores the scopes.
				log.exception("shot iteration failed")
				time.sleep(1.0)  # bound the rate of a persistent failure
		log.info("stop requested; setting LeCroy NORM and Rigol RUN")
		release_scopes()
		log.info("acquisition stopped")
	except KeyboardInterrupt:
		log.warning("forced exit: scope trigger modes not restored")


if __name__ == "__main__":
	main()
