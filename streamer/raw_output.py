"""Lifecycle and timing wrapper for acquired raw-shot output."""

import time

from .payload import shot_variables
from .raw_output_io import IO
from .timing_log import TimingLog, timestamp


class RawOutput:
	"""Queue one complete raw acquisition at a time for a background writer (raw_output_io.BufferedIO)."""

	def __init__(self, settings):
		self._timing_log = TimingLog(settings.timing_log)
		try:
			self._output = IO.create(
				settings, timing_log=self._timing_log, prepare=shot_variables
			)
		except BaseException:
			self._timing_log.close()
			raise
		self._shot_index = 0
		self._closed = False

	@property
	def dropped_shots(self):
		return self._output.dropped_steps

	@property
	def written_shots(self):
		return self._output.written_steps

	@property
	def unsent_shots(self):
		return self._output.unsent_steps

	@property
	def error(self):
		return self._output.error

	def write(self, shot):
		if self._closed:
			raise RuntimeError("Cannot write to closed raw output")
		shot_index = self._shot_index
		called_at = timestamp()
		start = time.perf_counter()
		# Only queues: shot_variables runs on the writer thread (prepare).
		self._output.write(shot, step=shot_index)
		self._timing_log.try_record(  # a full log disk must not make write() raise
			"raw_output.write",
			shot=shot_index,
			called_at=called_at,
			duration_seconds=time.perf_counter() - start,
			status="ok",
		)
		self._shot_index += 1

	def close(self, timeout=None):
		"""Write what is queued, for at most ``timeout`` s (None: until done); see unsent_shots."""
		if self._closed:
			return
		self._closed = True
		try:
			self._output.close(timeout)
		finally:
			self._timing_log.close()

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		self.close()
