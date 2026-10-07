"""The contract between the diag_ioc host and a diagnostic module, and the per-module shot pipeline."""
import logging
import threading
import time
from collections import deque

from diag_ioc.outage import Outage

log = logging.getLogger(__name__)


class DiagnosticModule:
	"""Base class of a diag_ioc module; the host constructs it as factory(name, options).

	Call order: create_records(builder) after SetDeviceName(prefix) and before iocInit; start(host) after
	iocInit and before the link accepts shots; then analyze() and publish() for one shot at a time on the
	pipeline's worker thread; stop() at exit. Record set() is thread-safe, so publish() may run off the
	dispatcher thread. If analyze() or publish() raises, nothing more of that shot is published (records keep
	the previous shot and its alarms) and STAT:ERROR goes MAJOR, so analyze() should turn bad or missing input
	into published missing data (empty + INVALID) and raise only on bugs.
	"""

	def __init__(self, name, options):
		self.name = name
		self.options = dict(options)

	def create_records(self, builder):
		raise NotImplementedError

	def analyze(self, variables):
		"""{name: array} from the link -> result for publish(). Pure (no records, no I/O) and picklable."""
		raise NotImplementedError

	def publish(self, result):
		raise NotImplementedError

	def start(self, host):
		"""`host` is the module's diag_ioc.host.ModuleHost; a module with its own data source calls host.pipeline.submit()."""

	def stop(self):
		pass


class ShotPipeline:
	"""Latest-wins mailbox plus one worker thread that runs module.analyze, then module.publish.

	submit() never blocks: a full mailbox drops its oldest message (`dropped`). After every shot the worker
	calls on_done(analysis_s, error), error "" for a good shot; a failing shot is also logged (throttled).
	"""

	def __init__(self, module, depth=2, on_done=None):
		self.module = module
		self.dropped = 0
		self.last_received = None  # time.time() of the latest submit(); None before the first
		self._depth = depth
		self._on_done = on_done
		self._queue = deque()
		self._cond = threading.Condition()
		self._closing = False
		self._outage = Outage(log, f"module {module.name}")
		self._thread = threading.Thread(target=self._run, name=f"pipeline-{module.name}", daemon=True)
		self._thread.start()

	def submit(self, variables):
		with self._cond:
			if self._closing:
				return  # shutting down: a message still arriving from the link is discarded
			if len(self._queue) == self._depth:
				self._queue.popleft()
				self.dropped += 1
			self._queue.append(variables)
			self.last_received = time.time()
			self._cond.notify()

	def close(self, timeout=5.0):
		"""Stop the worker after its current shot (queued ones are discarded); waits at most `timeout` s."""
		with self._cond:
			self._closing = True
			self._cond.notify()
		self._thread.join(timeout)
		if self._thread.is_alive():
			log.warning("module %s: worker still busy after %.0f s; abandoning it (daemon thread)", self.module.name, timeout)

	def _run(self):
		while True:
			with self._cond:
				while not self._queue and not self._closing:
					self._cond.wait()
				if self._closing:
					return
				variables = self._queue.popleft()
			t0 = time.perf_counter()
			error = ""
			try:
				self.module.publish(self.module.analyze(variables))
			except Exception as e:
				error = f"{type(e).__name__}: {e}"
				self._outage.failed(e, exc_info=True)
			else:
				self._outage.ended()
			if self._on_done is not None:
				try:
					self._on_done(time.perf_counter() - t0, error)
				except Exception:
					log.exception("module %s: status update failed", self.module.name)
