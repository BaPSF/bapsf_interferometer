# -*- coding: utf-8 -*-
"""Shot time and shot identity (docs/ARCHITECTURE.md D14, D15).

shot_time is the LeCroy WAVEDESC trigger time (the scope clock is NTP-synced), or host_time when a shot has none.
A shot is (shot_date, shot_number): its LA-local date as YYYYMMDD, and a number from 0 each date, persisted (and
recovered from the acquisition log) so a restart the same day continues.
"""
import datetime
import json
import logging
import os
import re
import threading
from pathlib import Path
from zoneinfo import ZoneInfo  # Windows needs the tzdata package, which lab_scopes installs there

from lab_scopes.lecroy import LeCroyWavedesc, wavedesc_trigger_timestamp

from diag_ioc.outage import Outage

log = logging.getLogger(__name__)

# The scopes' clock zone and the zone of shot_date; a constant, not a setting (D15), and never the host's zone.
SHOT_ZONE = ZoneInfo("America/Los_Angeles")
# s; host_time - shot_time outside [0, this] is logged as a clock problem; 0 disables. ShotIdentifier's default.
TRIG_LAG_MAX_S = float(os.environ.get("INTERF_TRIG_LAG_MAX_S", "5"))


def local_time(t):
	"""Aware datetime of epoch `t` in SHOT_ZONE."""
	return datetime.datetime.fromtimestamp(t, SHOT_ZONE)


def clock(t):
	"""HH:MM:SS.fff of epoch `t` in SHOT_ZONE, so a trigger time reads as the scope displays it."""
	return local_time(t).strftime("%H:%M:%S.%f")[:-3]


def shot_date(shot_time):
	"""YYYYMMDD of `shot_time` (epoch s) in SHOT_ZONE, as an int."""
	return int(local_time(shot_time).strftime("%Y%m%d"))


def shot_id(date, number):
	"""The shot ID as logged and shown: "<YYYYMMDD>-<number>"."""
	return f"{date}-{number}"


def trigger_time(wd, host_time):
	"""(shot_time, time_source): the trigger time of `wd` (a LeCroyWavedesc(...).wd) and "trigger", or
	(host_time, "host") when `wd` is None or has no trigger time (tt_year 0). Never raises for a bad WAVEDESC."""
	t = None if wd is None else wavedesc_trigger_timestamp(wd, tz=SHOT_ZONE.key)
	if t is None:
		return host_time, "host"
	# When DST ends the wall clock repeats an hour; wavedesc_trigger_timestamp returns the first occurrence, and the
	# second is `repeat` s later (0 outside that hour). The fields cannot tell them apart, so take the one nearer host_time.
	first = local_time(t)
	repeat = (first.utcoffset() - first.replace(fold=1).utcoffset()).total_seconds()
	if repeat and abs(t + repeat - host_time) < abs(t - host_time):
		t += repeat
	return t, "trigger"


class ShotIdentifier:
	"""identify(shot) sets a RawShot's shot_time, time_source, shot_date and shot_number; one per acquisition run.

	Never raises: a WAVEDESC that does not unpack falls back to host_time, and only a failing counter leaves
	shot_date/shot_number None. Failures, and host_time - shot_time outside [0, trig_lag_max_s] (0 disables), are
	logged when they start, every 5 min, and on recovery.
	"""

	def __init__(self, counter, trig_lag_max_s=TRIG_LAG_MAX_S):
		self.counter = counter
		self.trig_lag_max_s = trig_lag_max_s
		self._lag = Outage(log, "trigger time check")
		self._failures = Outage(log, "shot identity")

	def identify(self, shot):
		ok = True
		try:
			# Every channel of one capture shares a trigger; the first WAVEDESC read stands for all.
			wd = LeCroyWavedesc(next(iter(shot.lecroy.values()))[1]).wd if shot.lecroy else None
			shot.shot_time, shot.time_source = trigger_time(wd, shot.host_time)
		except Exception as e:  # a WAVEDESC that does not unpack
			shot.shot_time, shot.time_source = shot.host_time, "host"
			self._failures.failed(e, "shot time falls back to host_time", exc_info=True)
			ok = False
		if shot.time_source == "trigger":
			self._check_lag(shot.host_time - shot.shot_time)
		try:
			shot.shot_date, shot.shot_number = self.counter.next(shot.shot_time)
		except Exception as e:
			self._failures.failed(e, exc_info=True)
			ok = False
		if ok:
			self._failures.ended()

	def _check_lag(self, lag):
		if self.trig_lag_max_s <= 0:
			return
		if 0 <= lag <= self.trig_lag_max_s:
			self._lag.ended()
		else:
			# Detection lags the trigger by scope processing plus polling; outside the bound one clock is wrong.
			self._lag.failed(f"host_time - shot_time = {lag:.3f} s, outside 0..{self.trig_lag_max_s:g} s "
			                 "(INTERF_TRIG_LAG_MAX_S); check the scope's NTP sync")


# interf_main's shot line (LOG_FORMAT, then interf_main._shot_line); a "shot None-None" line has no identity.
_LOGGED_SHOT = re.compile(r" interf_main: shot (\d{8})-(\d+) \|")


class ShotCounter:
	"""next(shot_time) -> (shot_date, shot_number); the number restarts at 0 whenever the date changes.

	Resumes after the later of the state file and the last shot line in `log_paths` (D14). next() only counts; a
	daemon thread writes the state, latest wins, so call close() to write the last one. state_path None persists nothing.
	"""

	def __init__(self, state_path, log_paths=()):
		self.state_path = None if state_path is None else Path(state_path)
		self._date, self._number = self._resume(log_paths)
		if self.state_path is None:
			return
		self._save_outage = Outage(log, f"shot counter state {self.state_path}")
		self._wake = threading.Condition()
		self._pending = None  # (date, number) not yet written; next() overwrites it
		self._closed = False
		self._writer = threading.Thread(target=self._write_loop, name="shot-counter-writer", daemon=True)
		self._writer.start()

	def _resume(self, log_paths):
		"""(date, number) to continue after; (None, -1) starts at 0."""
		stored, error = self._load() if self.state_path else (None, None)
		logged = max(filter(None, map(_last_logged_shot, log_paths)), default=None)
		if error is not None:
			log.warning("shot counter state %s unreadable (%s: %s); %s", self.state_path, type(error).__name__, error,
			            f"resuming after {shot_id(*logged)} from the acquisition log" if logged else
			            "no shot in the acquisition log either, so numbers already used today may repeat")
		# YYYYMMDD ints order as dates, so the tuple max is the later date and, on one date, the larger number.
		return max(filter(None, (stored, logged)), default=(None, -1))

	def _load(self):
		"""(state or None, the exception that made an existing file unreadable or None)."""
		try:
			state = json.loads(self.state_path.read_text())
			date, number = state["shot_date"], state["shot_number"]
			if type(date) is not int or type(number) is not int or number < 0:
				raise ValueError(f"bad values {state!r}")
			return (date, number), None
		except FileNotFoundError:
			return None, None
		except Exception as e:  # OSError, JSON, missing keys, bad values
			return None, e

	def next(self, shot_time):
		date = shot_date(shot_time)
		self._number = self._number + 1 if date == self._date else 0
		self._date = date
		if self.state_path is not None:
			with self._wake:  # held by the writer only to swap _pending, never during a write
				if self._closed:
					raise RuntimeError("ShotCounter is closed")
				self._pending = (date, self._number)
				self._wake.notify()
		return date, self._number

	def close(self, timeout=5.0):
		"""Write the last state and stop the writer; on a disk slower than `timeout` s, log and return."""
		if self.state_path is None or self._closed:
			return
		with self._wake:
			self._closed = True
			self._wake.notify()
		self._writer.join(timeout)
		if self._writer.is_alive():
			log.warning("shot counter state %s: last write still running after %g s", self.state_path, timeout)

	def _write_loop(self):
		while True:
			with self._wake:
				while self._pending is None and not self._closed:
					self._wake.wait()
				state, self._pending = self._pending, None
			if state is None:  # closed, and the last state written
				return
			self._save(*state)

	def _save(self, date, number):
		tmp = self.state_path.with_name(self.state_path.name + ".tmp")
		try:
			self.state_path.parent.mkdir(parents=True, exist_ok=True)
			with open(tmp, "w") as f:
				json.dump({"shot_date": date, "shot_number": number}, f)
				f.flush()
				os.fsync(f.fileno())  # else a power cut after the rename can leave an empty file
			# Atomic: a crash leaves the old state or the new, never half of one. The rename itself is not synced,
			# so a power cut can lose the last update; the acquisition log then supplies it at the next start.
			os.replace(tmp, self.state_path)
		except Exception as e:  # not only OSError: an escaping error would end the writer, and with it every later write
			self._save_outage.failed(e, "counting continues in memory", exc_info=not isinstance(e, OSError))
		else:
			self._save_outage.ended()


def _last_logged_shot(path):
	"""(date, number) of the last shot line in the log at `path`, or None (no such line, or no readable file)."""
	last = None
	try:
		with open(path, encoding="utf-8", errors="replace") as f:
			for line in f:  # one pass at start; a day's log is ~29k lines
				if m := _LOGGED_SHOT.search(line):
					last = m
	except FileNotFoundError:
		return None
	except OSError as e:
		log.warning("acquisition log %s unreadable for the shot counter (%s: %s)", path, type(e).__name__, e)
		return None
	return (int(last[1]), int(last[2])) if last else None
