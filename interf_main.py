# coding: utf-8
"""Acquisition loop: acquire_shot() until stopped, one log line per shot. Linux only."""
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path

from lab_scopes.lecroy import LeCroyWavedesc, wavedesc_trigger_timestamp

from diag_ioc.outage import LOG_FORMAT, Outage
from interf_raw import AcqState, acquire_shot, release_scopes

LOG_DIR = os.environ.get("INTERF_LOG_DIR", str(Path.home() / "data" / "log"))

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
	logging.basicConfig(level=logging.INFO, format=LOG_FORMAT,
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


# {id(output): (output, Outage)} for outputs in an outage. Keyed by id() so any object can be an
# output (a plain @dataclass is unhashable); the strong reference keeps the id from being reused
# by another object while its entry exists.
_outages = {}


def _write_outputs(shot, outputs):
	for output in outputs:
		try:
			output.write(shot)
		except Exception as e:
			# Isolated so one broken output cannot stop the others, the shot log line, or the loop.
			entry = _outages.get(id(output))
			if entry is None:
				entry = _outages[id(output)] = (output, Outage(log, f"output {output!r}"))
			entry[1].failed(e, "acquisition continues without it", exc_info=True)
		else:
			if (entry := _outages.pop(id(output), None)) is not None:
				entry[1].ended()


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
	"""Acquire until stopped; each shot goes to every `outputs` item's write(shot). The caller closes them."""
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
	ioc_link = os.environ.get("INTERF_IOC_LINK")  # unix:/path or tcp:host:port of the diag_ioc listener
	if ioc_link:
		from diag_ioc.link import IocLink  # no EPICS library: diag_ioc's __init__ keeps it that way
		with IocLink(ioc_link) as link:
			main(outputs=[link])
	else:
		main()
