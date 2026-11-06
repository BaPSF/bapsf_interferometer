"""Opt-in outage routing with a single owner for every queued frame."""
import shutil
import threading
import time
from datetime import datetime

from .consumer_output import RotatingConsumerOutput
from .ntfy_notifications import NtfyNotifier
from .raw_output_io import BufferedIO
from .remote_consumer_io import RestartingSingleSocketIO


class FallbackIO(BufferedIO):
    MIN_FREE_BYTES = 10_000_000_000

    def __init__(self, settings, config, private_key, timing_log=None, prepare=None):
        self._count_pending = True
        self._config = config
        self._settings = settings
        self._key = private_key
        self._remote = None
        self._connecting_socket = None
        self._task = None
        self._task_done = False
        self._task_error = None
        self._fallback = False
        self._fallback_armed = True
        self._transition_state = None
        self._estimate = None
        self._unknown_clock_logged = False
        self._local = None
        self._next_local_retry = 0
        self._local_written = 0
        self._remote_written = 0
        notifier = NtfyNotifier.from_config(config, timing_log=timing_log)
        self._transition_notifier = notifier
        super().__init__(lambda: None, getattr(settings, "buffer_seconds", 600.0),
                         getattr(settings, "output_interval_seconds", 3.0), timing_log=timing_log,
                         retry_delay_seconds=config.retry_delay_seconds, prepare=prepare)
        self._notifier = notifier
        self._recovery = threading.Thread(target=self._recover, daemon=True,
                                          name="raw-output-recovery")
        self._recovery.start()

    def _enqueue(self, pending):
        super()._enqueue(pending)
        with self._condition:
            if self._task is not None:
                self._fallback_armed = True
            if self._fallback_armed and len(self._buffer) + (self._pending is not None) >= math_ceil_90(self._capacity):
                self._fallback = True
            self._condition.notify_all()

    def _socket_available(self, output):
        with self._condition:
            self._connecting_socket = output
            stopped = self._closed
        if stopped:
            output.abort()

    def _recover(self):
        while True:
            with self._condition:
                if self._closed:
                    return
            remote = None
            try:
                remote = RestartingSingleSocketIO(
                    self._settings, self._key, config=self._config,
                    timing_log=self._timing_log, single_attempt=True,
                    socket_callback=self._socket_available, notifications=False,
                )
                estimate = remote._output.clock_estimate
                with self._condition:
                    if self._closed:
                        return
                    self._estimate = estimate
                    self._remote = remote
                    # Give a ready connection the backlog. Re-arm after it drains
                    # below the threshold, or immediately if this connection fails.
                    self._fallback_armed = (len(self._buffer) + (self._pending is not None)
                                            < math_ceil_90(self._capacity))
                    self._condition.notify_all()
                self._record("fallback.clock", offset_seconds=estimate[0],
                             round_trip_seconds=estimate[1], timezone=str(estimate[4]))
                while True:
                    with self._condition:
                        self._condition.wait_for(lambda: self._closed or self._task is not None)
                        if self._closed:
                            return
                        task = self._task
                    error = None
                    try:
                        remote.write_data(task[0])
                    except Exception as exc:
                        error = exc
                    with self._condition:
                        self._task_error = error
                        self._task_done = True
                        if error is not None:
                            self._remote = None
                        self._condition.notify_all()
                        self._condition.wait_for(lambda: self._closed or self._task is not task)
                        if self._closed:
                            return
                        retired = self._remote is not remote
                    if error is not None:
                        raise error
                    if retired:
                        break
            except Exception as exc:
                with self._condition:
                    self._fallback_armed = True
                    if len(self._buffer) + (self._pending is not None) >= math_ceil_90(self._capacity):
                        self._fallback = True
                self._record_error("remote", exc)
            finally:
                with self._condition:
                    self._remote = None
                    self._connecting_socket = None
                    self._condition.notify_all()
                if remote is not None:
                    self._quietly("remote.abort", remote.abort)
                    self._quietly("remote.close", remote.close)
            with self._condition:
                self._condition.wait_for(lambda: self._closed,
                                         timeout=max(.01, self._retry_delay_seconds))

    def _transition(self, state, message, **details):
        if self._transition_state == state:
            return
        self._transition_state = state
        self._record("fallback.transition", state=state, message=message,
                     directory=str(self._config.local_fallback_directory),
                     local_written=self._local_written, remote_written=self._remote_written,
                     dropped=self._dropped_steps, **details)
        if self._transition_notifier is not None:
            self._transition_notifier.notify(message, message)

    def _timestamp(self):
        with self._condition:
            estimate = self._estimate
        if estimate is None:
            if not self._unknown_clock_logged:
                self._unknown_clock_logged = True
                self._record("fallback.clock_unknown", message="Using producer local time and timezone; server clock and timezone unknown")
                if self._transition_notifier is not None:
                    self._transition_notifier.notify("Server clock skew unknown", "Using producer local time and timezone for local saving")
            return datetime.now().astimezone()
        return datetime.fromtimestamp(estimate[2] + time.monotonic() - estimate[3], estimate[4])

    def _close_local(self):
        local, self._local = self._local, None
        if local is not None:
            self._quietly("local.close", local.close)

    def _write_local(self, data, step):
        directory = self._config.local_fallback_directory
        directory.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(directory).free
        self._record("fallback.disk", directory=str(directory), free_bytes=free,
                     minimum_bytes=self.MIN_FREE_BYTES)
        if free < self.MIN_FREE_BYTES:
            raise OSError(f"Local free space {free} below {self.MIN_FREE_BYTES} bytes")
        if self._local is None:
            self._local = RotatingConsumerOutput(directory, "BP5",
                interval_seconds=self._config.file_interval_seconds,
                timestamp_factory=self._timestamp)
        self._local.write_data(data)
        self._local_written += 1
        self._record("fallback.write", step=step, directory=str(directory),
                     path=str(self._local.current_path),
                     local_written=self._local_written)

    def _write_pending(self):
        try:
            while True:
                with self._condition:
                    if self._closed or (self._closing and self._pending is None and not self._buffer):
                        return
                    remote = self._remote
                    fallback = self._fallback
                    if remote is None and not fallback:
                        self._condition.wait(.02)
                        continue
                if remote is not None:
                    self._close_local()
                    if self._transition_state is not None:
                        self._transition("remote", "Remote streaming resumed")
                    self._fallback = False
                elif time.monotonic() < self._next_local_retry:
                    with self._condition:
                        self._condition.wait(.02)
                    continue
                # Never wait in _next_pending: recovery may become ready while idle.
                with self._condition:
                    if self._pending is None and not self._buffer:
                        self._condition.wait(.02)
                        continue
                pending = self._next_pending()
                if pending is None:
                    continue
                data, step = pending
                success = False
                if remote is not None:
                    with self._condition:
                        if self._remote is not remote:
                            continue
                        self._task, self._task_done = pending, False
                        self._condition.notify_all()
                    cancelled = False
                    while True:
                        with self._condition:
                            if self._task_done or self._closed:
                                break
                            self._condition.wait(.02)
                            if self._task_done:
                                continue
                            if self._fallback_armed and len(self._buffer) + 1 >= math_ceil_90(self._capacity):
                                self._fallback = True
                            cancel = (self._fallback or self._closed) and not cancelled
                            if cancel:
                                self._remote = None
                        if cancel:
                            # SSH/socket shutdown can wait. Do it without the queue lock.
                            cancelled = True
                            remote.abort()
                    with self._condition:
                        if self._closed:
                            return
                        success = self._task_error is None
                        self._task = None
                        self._condition.notify_all()
                    if success:
                        self._remote_written += 1
                else:
                    try:
                        self._write_local(data, step)
                        success = True
                        message = ("Local saving resumed" if self._transition_state == "paused"
                                   else "Saving raw data locally")
                        self._transition("local", message)
                    except Exception as exc:
                        self._record_error("local", exc, step=step)
                        self._close_local()
                        self._transition("paused", "Local saving paused; buffering in memory", reason=str(exc))
                        self._next_local_retry = time.monotonic() + max(.01, self._retry_delay_seconds)
                if success:
                    with self._condition:
                        self._pending = None
                        self._written_steps += 1
                        if len(self._buffer) < math_ceil_90(self._capacity):
                            self._fallback_armed = True
                    self._error = None
        finally:
            self._close_local()
            with self._condition:
                self._closed = True
                self._condition.notify_all()

    def close(self, timeout=None):
        super().close(timeout)
        with self._condition:
            remote, connecting = self._remote, self._connecting_socket
            self._condition.notify_all()
        if remote is not None:
            remote.abort()
        if connecting is not None:
            connecting.abort()
        self._worker.join(.2)
        self._recovery.join(.2)
        if self._transition_notifier is not None:
            self._transition_notifier.close()


def math_ceil_90(capacity):
    return (9 * capacity + 9) // 10
