"""Throttled logging of a persisting failure, shared by interf_main's outputs and the IOC link."""
import time

OUTAGE_LOG_INTERVAL_S = 300.0


class Outage:
	"""Logs a failure once when it starts, every OUTAGE_LOG_INTERVAL_S while it lasts, and once when it ends.

	One owner thread (or the owner's lock); `count` is the failed() calls of the current outage.
	"""

	def __init__(self, logger, what):
		self.what = what
		self.since = None  # time.monotonic() at the first failure; None while healthy
		self.count = 0
		self._logger = logger
		self._logged = 0.0

	def failed(self, error, note="", exc_info=False):
		"""Record one failure; `error` is an exception or text, `note` is appended after it."""
		detail = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
		if note:
			detail += f"; {note}"
		now = time.monotonic()
		self.count += 1
		if self.since is None:
			self.since = self._logged = now
			self._logger.warning("%s failed (%s)", self.what, detail, exc_info=exc_info)
		elif now - self._logged >= OUTAGE_LOG_INTERVAL_S:
			self._logged = now
			self._logger.warning("%s still failing after %.0f s, %d failures (%s)",
			                     self.what, now - self.since, self.count, detail)

	def ended(self, note=""):
		"""Log the recovery if an outage is ongoing; `note` is appended to the message."""
		if self.since is not None:
			self._logger.info("%s recovered after %.0f s, %d failures%s",
			                  self.what, time.monotonic() - self.since, self.count, f"; {note}" if note else "")
			self.since, self.count = None, 0
