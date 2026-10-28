# -*- coding: utf-8 -*-
"""PNG's run and shot numbers, kept current by CA monitors for acquisition to copy at each capture (D16)."""
import functools
import logging
import threading
import time

from diag_ioc.outage import Outage

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 5.0  # start-up wait for both first values; after it acquisition starts without them
_FLUSH_POLL_S = 0.05


class PngMonitor:
	"""CA monitors of PNG's run and shot PVs; latest() copies their last values and never waits on CA."""

	def __init__(self, run_pv, shot_pv, connect_timeout=CONNECT_TIMEOUT_S):
		import epics  # here only, so interf_main without PNG never loads an EPICS library (D16)
		self._ca = epics.ca
		self._names = {"run": run_pv, "shot": shot_pv}
		self._lock = threading.Lock()
		self._latest = dict.fromkeys(("run", "run_time", "shot", "shot_time"))  # <key>_time: CA timestamp, epoch s
		# Silent at the first connection (ended() is a no-op while healthy); each disconnect and reconnect logs once.
		self._links = {name: Outage(log, f"PNG {name}") for name in self._names.values()}
		self._flush_failures = Outage(log, "PNG CA flush")
		self._pvs = [epics.PV(name, auto_monitor=True, form="time", callback=functools.partial(self._on_value, key),
		                      connection_callback=self._on_connection)
		             for key, name in self._names.items()]  # held: a PV no longer referenced stops monitoring
		# pyepics reports a PV connected before it subscribes, so the last monitor created can silently never start
		# (ARCHITECTURE D13): flush until both have delivered. One that misses this goes out with a later flush().
		deadline = time.monotonic() + connect_timeout
		while silent := self._silent():
			if time.monotonic() >= deadline:
				log.warning("PNG not connected: no value from %s within %g s; shots carry none until it arrives",
				            ", ".join(silent), connect_timeout)
				return
			self._ca.flush_io()
			time.sleep(_FLUSH_POLL_S)
		latest = self.latest()
		log.info("PNG monitors ready: run %s, shot %s", latest["run"], latest["shot"])

	def _silent(self):
		with self._lock:
			return [name for key, name in self._names.items() if self._latest[key + "_time"] is None]

	def _on_value(self, key, value=None, timestamp=None, **kw):
		# On a CA thread: stores only, no CA call (handoff §3).
		with self._lock:
			self._latest[key], self._latest[key + "_time"] = value, timestamp

	def _on_connection(self, pvname=None, conn=None, **kw):
		# On a CA thread: logs only.
		if conn:
			self._links[pvname].ended()
		else:
			self._links[pvname].failed("disconnected", "shots keep its last value until it reconnects")

	def latest(self):
		"""A new {"run", "run_time", "shot", "shot_time"}: each PV's last value and CA timestamp (epoch s), None before its
		first. A value may be unchanged since earlier shots; whether it is this shot's is the reader's match (D16)."""
		with self._lock:
			return dict(self._latest)

	def flush(self):
		"""Send queued CA requests (a subscription that missed the start); never raises."""
		try:
			self._ca.flush_io()
		except Exception as e:
			self._flush_failures.failed(e, "PNG values may stop updating")
		else:
			self._flush_failures.ended()
