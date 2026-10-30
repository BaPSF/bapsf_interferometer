# coding: utf-8
"""Acquisition loop: acquire_shot() until stopped, one log line per shot. Linux only."""
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path

from diag_ioc.outage import LOG_FORMAT, Outage, ms_text
from interf_raw import AcqState, acquire_shot, release_scopes
from interf_shot import ShotCounter, ShotIdentifier, clock, shot_id

LOG_DIR = os.environ.get("INTERF_LOG_DIR", str(Path.home() / "data" / "log"))
ACQUIRE_LOG = "interf_acquire.log"  # in LOG_DIR
# State, not a log: kept out of LOG_DIR, whose old files get rotated and cleaned up.
SHOT_STATE = os.environ.get("INTERF_SHOT_STATE", str(Path.home() / "data" / "state" / "shot_counter.json"))
# PNG's CA PVs (D16)
PNG_RUN_PV = os.environ.get("INTERF_PNG_RUN_PV", "")
PNG_SHOT_PV = os.environ.get("INTERF_PNG_SHOT_PV", "")
# Raw output (D9, D17): INTERF_RAW_OUTPUT is the destination, INTERF_RAW_OUTPUT_* its settings
# (streamer.arguments.settings_from_env).
RAW_OUTPUT_ENV = "INTERF_RAW_OUTPUT"
RAW_OUTPUT_TIMING_LOG = "raw_output_%Y%m%dT%H%M%S.jsonl"  # strftime at start, in LOG_DIR: one file per start
# Default queue: 20 shots at 3 s, about 240 MB at ~12 MB a shot (D5); streamer's 600 s default would hold ~2.4 GB.
RAW_OUTPUT_BUFFER_S = 60.0
# Raw output's drain at stop; with the shot in flight, the scope release and the link's close it stays under the
# unit's TimeoutStopSec=30.
RAW_OUTPUT_CLOSE_TIMEOUT_S = 15.0

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
	if logging.getLogger().handlers:
		return  # configured already: by __main__ before the outputs start, or by interf_sim's run_main
	os.makedirs(LOG_DIR, exist_ok=True)
	# Midnight rotation: ~29k shots/day, each a shot and a timing line (plus a link line with INTERF_IOC_LINK).
	file_handler = logging.handlers.TimedRotatingFileHandler(
		os.path.join(LOG_DIR, ACQUIRE_LOG), when="midnight", backupCount=30)
	logging.basicConfig(level=logging.INFO, format=LOG_FORMAT,
	                    handlers=[logging.StreamHandler(sys.stdout), file_handler])


def _acquire_logs():
	"""The current acquisition log and its newest rotated copy: rotation is at host midnight, so right after it the
	last shot is only in the copy."""
	# TimedRotatingFileHandler's when="midnight" suffix; a stray .gz or .tmp would otherwise sort last.
	rotated = sorted(Path(LOG_DIR).glob(ACQUIRE_LOG + ".????-??-??"))
	return [Path(LOG_DIR) / ACQUIRE_LOG, *rotated[-1:]]


def _shot_line(shot, prev_trig):
	if shot.time_source == "trigger":
		dt = f"{shot.shot_time - prev_trig:.3f} s" if prev_trig is not None else "-"
		trig_txt = f"trig {clock(shot.shot_time)} dt {dt}"
	else:
		trig_txt = "trig ?"
	parts = [f"shot {shot_id(shot.shot_date, shot.shot_number)}", f"host {clock(shot.host_time)}", trig_txt]
	for scope, data in (("LeCroy", shot.lecroy), ("Rigol", shot.rigol)):
		if data:
			parts.append(scope + " " + " ".join(f"{ch}:{samples.size}" for ch, (samples, _) in data.items()))
	parts.append(f"path {shot.critical_path_s:.2f} s")
	if shot.missing:
		parts.append("missing " + "; ".join(f"{k}: {v}" for k, v in shot.missing.items()))
	parts.append(_png_text(shot.png, shot.host_time))
	return " | ".join(parts)


def _png_text(png, host_time):
	"""The shot line's PNG part; lag is host_time minus the shot number's CA timestamp, and about 3 s means PNG's
	previous shot, not this one (D16)."""
	if png is None or png["shot"] is None:
		return "png -"
	run = "?" if png["run"] is None else png["run"]
	return f"png {run}-{png['shot']} lag {host_time - png['shot_time']:.3f}"  # shot_time arrives with shot


def _timing_line(shot, identify_s, outputs_s):
	"""Step times in ms. "timing", not "shot", after the logger name: ShotCounter resumes from shot lines only."""
	t = shot.timing
	prepare = ms_text(t.prepare_s)
	if t.prepare_s is not None:
		prepare += " armed" if t.prepare_armed else " not armed"
	parts = [f"timing {shot_id(shot.shot_date, shot.shot_number)} (ms)", f"open {ms_text(t.open_s)}", f"prepare {prepare}",
	         f"wait {ms_text(t.wait_s)}", f"onoff {ms_text(t.onoff_s)}",
	         " ".join(f"{ch} {ms_text(s)}" for ch, s in t.channels_s.items()),
	         f"rigol {ms_text(t.rigol_s)}" + (f" {t.rigol}" if t.rigol else ""), f"rearm {ms_text(t.rearm_s)}",
	         f"crit {ms_text(shot.critical_path_s)}", f"close {ms_text(t.close_s)}", f"identify {ms_text(identify_s)}",
	         f"outputs {ms_text(outputs_s)}", f"cycle {ms_text(t.cycle_s)}"]
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


def _handle_shot(shot, prev_trig, identifier, outputs=()):
	"""Assign the shot identity, write the shot to every output, then log it and its step times; return the trigger
	time for the next dt."""
	t0 = time.perf_counter()
	identifier.identify(shot)  # never raises
	t1 = time.perf_counter()
	_write_outputs(shot, outputs)  # an IocLink only queues here; its sender logs encode and send times
	t2 = time.perf_counter()
	log.log(logging.WARNING if shot.missing else logging.INFO, _shot_line(shot, prev_trig))
	log.info(_timing_line(shot, t1 - t0, t2 - t1))
	# None after a shot without a trigger time, so the next dt prints "-" rather than spanning
	# two captures and reading as a skipped shot.
	return shot.shot_time if shot.time_source == "trigger" else None


def _start_png():
	"""A PngMonitor of PNG_RUN_PV and PNG_SHOT_PV, or None."""
	if bool(PNG_RUN_PV) != bool(PNG_SHOT_PV):
		log.warning("PNG off: INTERF_PNG_RUN_PV and INTERF_PNG_SHOT_PV must both be set")
	if not (PNG_RUN_PV and PNG_SHOT_PV):
		return None
	try:
		from interf_png import PngMonitor
		return PngMonitor(PNG_RUN_PV, PNG_SHOT_PV)  # waits at most interf_png.CONNECT_TIMEOUT_S
	except Exception:
		log.exception("PNG monitor not started; acquisition continues without PNG numbers")
		return None


def _start_raw_output():
	"""streamer's RawOutput for RAW_OUTPUT_ENV, or None (unset, or its local checks failed: no raw output, D17)."""
	if not os.environ.get(RAW_OUTPUT_ENV):
		return None
	try:
		# Only here: the live link never imports streamer (D6).
		from streamer import RawOutput
		from streamer.arguments import settings_from_env
		settings = settings_from_env(RAW_OUTPUT_ENV, Path(LOG_DIR) / time.strftime(RAW_OUTPUT_TIMING_LOG),
		                             buffer_seconds=RAW_OUTPUT_BUFFER_S)
		output = RawOutput(settings)  # only local checks; the destination is opened on its writer thread
	except Exception:
		log.exception("raw output not started; acquisition continues without it")
		return None
	log.info("raw output started: %s, timing log %s", settings.destination, settings.timing_log)
	return output


def _report_raw_output(raw_outputs, outage):
	"""Log raw output failing and recovering from its `error` status: a reference read, no lock and no I/O (D17)."""
	for output in raw_outputs:
		error = output.error  # read once: the writer thread may clear it at any time
		if error is not None:
			outage.failed(error, "shots wait in its queue, and a full queue drops some")
			return
	outage.ended()


def main(outputs=(), identifier=None):
	"""Acquire until stopped; each shot goes to every `outputs` item's write(shot). The caller closes them.

	`identifier` (an interf_shot.ShotIdentifier) defaults to one counting in SHOT_STATE with TRIG_LAG_MAX_S,
	resuming from the acquisition logs; main closes that counter, never a caller's.
	"""
	if not hasattr(signal, "setitimer"):
		sys.exit("interf_main: Linux only (the Rigol deadline needs signal.setitimer)")
	_setup_logging()
	png = _start_png()
	read_png = png.latest if png is not None else None
	_outages.clear()  # an output reused from an earlier run in this process starts healthy
	# streamer.RawOutput fails on its own writer thread, never in write(), and reports it as `error` (D17). Checked on
	# the class, so a mock output does not count.
	raw_outputs = [o for o in outputs if hasattr(type(o), "error")]
	raw_outage = Outage(log, "raw output")
	signal.signal(signal.SIGINT, _request_stop)
	signal.signal(signal.SIGTERM, _request_stop)  # systemd stop

	state = AcqState()
	counter = None
	if identifier is None:
		counter = ShotCounter(SHOT_STATE, log_paths=_acquire_logs())
		identifier = ShotIdentifier(counter)
	prev_trig = None
	log.info("acquisition started")
	try:
		while not stop_requested():
			try:
				shot = acquire_shot(state, stop_requested, read_png=read_png)
				if shot is not None:
					prev_trig = _handle_shot(shot, prev_trig, identifier, outputs)
					_report_raw_output(raw_outputs, raw_outage)
					if png is not None:
						png.flush()
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
	finally:
		if counter is not None:
			counter.close()


if __name__ == "__main__":
	_setup_logging()  # before the outputs, so their start-up messages reach the acquisition log
	ioc_link = os.environ.get("INTERF_IOC_LINK")  # unix:/path or tcp:host:port of the diag_ioc listener
	link = None
	if ioc_link:
		import interf_payload
		# diag_ioc loads no EPICS library (diag_ioc/__init__.py); interf_main's only one is pyepics, for PNG (D16).
		from diag_ioc.link import IocLink
		link = IocLink(ioc_link, encode=interf_payload.encode)
	raw_output = _start_raw_output()
	try:
		main(outputs=[o for o in (link, raw_output) if o is not None])
	finally:
		# The link first: it is local and quick, while raw output may wait on a network destination.
		if link is not None:
			link.close()
		if raw_output is not None:
			raw_output.close(timeout=RAW_OUTPUT_CLOSE_TIMEOUT_S)
			log.log(logging.WARNING if raw_output.dropped_shots or raw_output.unsent_shots else logging.INFO,
			        "raw output closed: written %d, dropped %d, unsent %d", raw_output.written_shots,
			        raw_output.dropped_shots, raw_output.unsent_shots)
