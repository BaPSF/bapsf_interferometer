"""Lifecycle and timing wrapper for acquired raw-shot output."""

import time

from .payload import shot_variables
from .raw_output_io import IO
from .timing_log import TimingLog, timestamp


class RawOutput:
	"""Convert, queue, and write one complete raw acquisition at a time."""

	def __init__(self, settings):
		self._timing_log = TimingLog(settings.timing_log)
		try:
			self._output = IO.create(settings, timing_log=self._timing_log)
		except BaseException:
			self._timing_log.close()
			raise
		self._shot_index = 0
		self._closed = False

	@property
	def dropped_shots(self):
		return self._output.dropped_steps

	def write(self, shot):
		if self._closed:
			raise RuntimeError("Cannot write to closed raw output")
		shot_index = self._shot_index
		called_at = timestamp()
		start = time.perf_counter()
		try:
			self._output.write(shot_variables(shot, shot_index), step=shot_index)
		except Exception as exc:
			self._timing_log.record(
				"raw_output.write",
				shot=shot_index,
				called_at=called_at,
				duration_seconds=time.perf_counter() - start,
				status="error",
				error_type=type(exc).__name__,
			)
			raise
		self._timing_log.record(
			"raw_output.write",
			shot=shot_index,
			called_at=called_at,
			duration_seconds=time.perf_counter() - start,
			status="ok",
		)
		self._shot_index += 1

	def close(self):
		if self._closed:
			return
		self._closed = True
		try:
			self._output.close()
		finally:
			self._timing_log.close()

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		self.close()
