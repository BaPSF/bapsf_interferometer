import json
import math
import os
import threading
import time
from collections import deque
from abc import ABC, abstractmethod
from pathlib import Path
from types import SimpleNamespace

from .connection_security import decrypt_connection_info, load_private_key
from .data_operations import load_data_operation


class IO(ABC):
    """Common producer output interface."""

    notifier = None

    @classmethod
    def create(cls, settings, timing_log=None, prepare=None):
        """Check ``settings`` locally and return a :class:`BufferedIO`.

        Nothing here waits on the network or opens the destination: launching,
        connecting and opening run on the writer thread, which retries them.
        ``prepare(data, step)``, when given, runs there too and converts each
        queued object to the variables written.
        """
        return BufferedIO(
            cls._output_opener(settings, timing_log),
            buffer_seconds=getattr(settings, "buffer_seconds", 600.0),
            output_interval_seconds=getattr(
                settings, "output_interval_seconds", 3.0
            ),
            timing_log=timing_log,
            retry_delay_seconds=getattr(settings, "retry_delay_seconds", 3.0),
            prepare=prepare,
        )

    @classmethod
    def _output_opener(cls, settings, timing_log):
        """Return a function that opens a new output for ``settings``.

        The mode follows the destination, never the key, and is fixed here
        rather than at each open: a consumer removes its connection file when it
        exits, and a retry must wait for the next one rather than write ADIOS at
        that path.

        - an existing directory, or a path ending in "/": direct ADIOS, local
          files (timestamped, or ``unused_path`` for an existing BP output);
        - a ``.conf``: the SSH-launched consumer (private key required);
        - any other existing file: a connection file from a consumer started by
          hand (private key required);
        - a path that does not exist: one direct ADIOS output.
        """
        from .remote_consumer_io import ServerConfig

        destination = Path(settings.destination)
        if settings.destination.endswith(("/", os.sep)) or destination.is_dir():
            engine = cls._require_adios(settings)
            if _is_adios_output(destination):
                return lambda: _open_adios(destination, engine)
            from .consumer_output import RotatingConsumerOutput

            interval = getattr(settings, "file_interval_seconds", 3600.0)
            return lambda: RotatingConsumerOutput(
                destination, engine, interval_seconds=interval
            )

        if (
            destination.suffix.lower() == ".conf"
            or ServerConfig.is_server_config(settings.destination)
        ):
            from .remote_consumer_io import RestartingSingleSocketIO

            private_key = cls._load_required_private_key(settings)
            config = ServerConfig(settings.destination)
            return lambda: RestartingSingleSocketIO(
                settings, private_key, config=config, timing_log=timing_log
            )

        if destination.exists():
            private_key = cls._load_required_private_key(settings)
            if cls._read_connection_info(settings.destination, private_key) is None:
                raise ValueError(
                    f"{settings.destination} exists but holds no connection "
                    "information; a direct ADIOS destination must not exist yet "
                    "or be a directory"
                )
            operation = load_data_operation(
                getattr(settings, "compression_config", None)
            )
            return lambda: cls._open_connection_file(
                settings, private_key, operation
            )

        engine = cls._require_adios(settings)
        return lambda: _open_adios(destination, engine)

    @staticmethod
    def _require_adios(settings):
        from .adios_io import ADIOS2_AVAILABLE

        if not ADIOS2_AVAILABLE:
            raise RuntimeError("adios2 is not available")
        return getattr(settings, "engine", "BP5")

    @classmethod
    def _open_connection_file(cls, settings, private_key, operation):
        # Re-read at every open: a restarted consumer writes a new port and ID.
        connection_info = cls._read_connection_info(
            settings.destination, private_key
        )
        if connection_info is None:
            raise FileNotFoundError(
                f"No connection information in {settings.destination}"
            )
        io_id = connection_info["id"]
        if io_id == "singlesocket":
            from .single_socket_io import SingleSocketIO

            return SingleSocketIO(
                settings, connection_info, private_key, operation=operation
            )
        if io_id == "adios":
            return _open_adios(
                Path(connection_info.get("output", settings.destination)),
                connection_info.get("engine", getattr(settings, "engine", "BP5")),
            )
        raise ValueError(f"Unsupported IO id in {settings.destination}: {io_id}")

    @staticmethod
    def _load_required_private_key(settings):
        private_key_path = getattr(settings, "private_key", None)
        if not private_key_path:
            raise ValueError("A private key is required for socket output")
        return load_private_key(private_key_path)

    @staticmethod
    def _read_connection_info(destination, private_key):
        path = Path(destination)
        if not path.is_file():
            return None

        try:
            with path.open(encoding="utf-8") as stream:
                connection_info = json.load(stream)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

        if not isinstance(connection_info, dict):
            return None
        if "format" not in connection_info:
            if "id" in connection_info:
                raise ValueError("Socket connection information must be encrypted")
            return None
        if private_key is None:
            raise ValueError("A private key is required for socket output")
        return decrypt_connection_info(connection_info, private_key)

    def write(self, data, step=None):
        del step
        self.write_data(data)

    @abstractmethod
    def write_data(self, data):
        pass

    @abstractmethod
    def close(self):
        pass

    def abort(self):
        """Drop a blocked connection at once, from any thread; close() still follows."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _is_adios_output(path):
    # BP4/BP5 outputs are directories; one must not be taken for a directory
    # of timestamped outputs.
    return path.suffix.lower() == ".bp" or (path / "md.idx").exists()


def unused_path(path):
    """``path``, or the first ``<stem>-1<suffix>``, ``-2``, ... that does not exist."""
    candidate = path
    sequence = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{sequence}{path.suffix}")
        sequence += 1
    return candidate


def _open_adios(path, engine):
    # ADIOS mode "w" erases an existing output, so never open one that exists.
    from .adios_io import AdiosIO

    return AdiosIO(
        SimpleNamespace(destination=str(unused_path(path)), engine=engine)
    )


class BufferedIO(IO):
    """Queue output steps and write them from a dedicated worker thread.

    The worker opens the output and never stops on an error: it records
    ``io.error``, discards that output, waits ``retry_delay_seconds``, opens a
    new one and retries the same step. write() therefore never waits on the
    destination and raises only after close().
    """

    HIGH_BUFFER_PERCENT = 80
    HIGH_BUFFER_NOTIFICATION_INTERVAL_SECONDS = 3600

    def __init__(
        self,
        open_output,
        buffer_seconds,
        output_interval_seconds,
        timing_log=None,
        clock=time.monotonic,
        retry_delay_seconds=3.0,
        prepare=None,
    ):
        if not math.isfinite(buffer_seconds) or buffer_seconds <= 0:
            raise ValueError("Buffer duration must be greater than zero")
        if not math.isfinite(output_interval_seconds) or output_interval_seconds <= 0:
            raise ValueError("Output interval must be greater than zero")
        if not math.isfinite(retry_delay_seconds) or retry_delay_seconds < 0:
            raise ValueError("Retry delay must not be negative")

        self._open_output = open_output
        self._output = None
        self._prepare = prepare
        self._retry_delay_seconds = retry_delay_seconds
        self._timing_log = timing_log
        self._notifier = None  # the open output's, for buffer-pressure notifications
        self._clock = clock
        self._capacity = max(1, math.ceil(buffer_seconds / output_interval_seconds))
        self._notification_thresholds = tuple(
            (percent, max(1, math.ceil(self._capacity * percent / 100)))
            for percent in (20, 40, 60)
        )
        self._high_notification_depth = max(
            1, math.ceil(self._capacity * self.HIGH_BUFFER_PERCENT / 100)
        )
        self._notified_thresholds = set()
        self._last_high_notification_at = None
        self._dropped_since_high_notification = 0
        self._buffer = deque()
        self._pending = None  # (data, step) taken from the buffer, not yet written
        self._condition = threading.Condition()
        self._closing = False  # close() called: no new steps
        self._closed = False  # close() done waiting: the worker stops at its next check
        self._dropped_steps = 0
        self._written_steps = 0
        self._unsent_steps = 0
        self._error = None
        self._next_drop_index = 1 if self._capacity > 1 else 0
        # A daemon: a destination that never answers cannot keep the process alive.
        self._worker = threading.Thread(
            target=self._write_pending,
            name="raw-output-writer",
            daemon=True,
        )
        self._worker.start()

    @property
    def capacity(self):
        return self._capacity

    @property
    def dropped_steps(self):
        with self._condition:
            return self._dropped_steps

    @property
    def written_steps(self):
        with self._condition:
            return self._written_steps

    @property
    def unsent_steps(self):
        """Steps queued or in flight when close() gave up; 0 before close()."""
        with self._condition:
            return self._unsent_steps

    @property
    def error(self):
        """The last error text while the worker is failing; None once a step is written again.

        Read without the lock (one reference read), so a caller on a hot path
        never waits for the worker.
        """
        return self._error

    def write(self, data, step=None):
        # With prepare, the worker converts the object later, so the caller must
        # not change it (RawShot arrays are fresh for each acquisition). Without,
        # a shallow mapping copy keeps the queued variable set stable.
        self._enqueue((data if self._prepare is not None else dict(data), step))

    def write_data(self, data):
        self.write(data)

    def _enqueue(self, pending):
        dropped = None
        low_notifications = []
        high_notification = None
        with self._condition:
            self._raise_if_unavailable()
            if len(self._buffer) >= self._capacity:
                dropped = self._drop_one_pending_step()
                self._dropped_since_high_notification += 1
            self._buffer.append(pending)
            depth = len(self._buffer)
            for percent, threshold_depth in self._notification_thresholds:
                if (
                    depth >= threshold_depth
                    and percent not in self._notified_thresholds
                ):
                    self._notified_thresholds.add(percent)
                    low_notifications.append(percent)
            now = self._clock()
            if depth >= self._high_notification_depth and (
                self._last_high_notification_at is None
                or now - self._last_high_notification_at
                >= self.HIGH_BUFFER_NOTIFICATION_INTERVAL_SECONDS
            ):
                high_notification = self._dropped_since_high_notification
                self._dropped_since_high_notification = 0
                self._last_high_notification_at = now
            notifier = self._notifier
            self._condition.notify()

        if notifier is not None:
            for percent in low_notifications:
                priority = 4 if percent >= 60 else 3
                notifier.notify(
                    f"Output ring buffer {percent}% full",
                    f"The output ring buffer contains {depth} of "
                    f"{self._capacity} queued snapshots.",
                    priority=priority,
                    tags=("warning",),
                )
            if high_notification is not None:
                loss_message = (
                    f" {high_notification} "
                    f"snapshot{' was' if high_notification == 1 else 's were'} "
                    "discarded since the previous high-buffer notification."
                    if high_notification
                    else ""
                )
                notifier.notify(
                    "Output ring buffer at least 80% full",
                    f"The output ring buffer contains {depth} of "
                    f"{self._capacity} queued snapshots.{loss_message}",
                    priority=5,
                    tags=("warning",),
                )

        if dropped is not None:
            self._record(
                "io.buffer_drop",
                dropped_step=dropped[1],
                incoming_step=pending[1],
                buffer_capacity_steps=self._capacity,
            )

    def _drop_one_pending_step(self):
        drop_index = min(self._next_drop_index, len(self._buffer) - 1)
        dropped = self._buffer[drop_index]
        del self._buffer[drop_index]
        self._dropped_steps += 1

        self._next_drop_index = drop_index + 1
        if self._next_drop_index >= self._capacity:
            self._next_drop_index = 0
        return dropped

    def _raise_if_unavailable(self):
        if self._closing:
            raise RuntimeError("Cannot write to closed output")

    def _record(self, event, **fields):
        # Best effort: a failing timing log must not stop the writer or the caller.
        if self._timing_log is not None:
            self._timing_log.try_record(event, **fields)

    def _record_error(self, stage, exc, **fields):
        self._error = f"{stage}: {type(exc).__name__}: {exc}"
        self._record(
            "io.error",
            stage=stage,
            error_type=type(exc).__name__,
            error=str(exc),
            **fields,
        )

    def _write_pending(self):
        failures = 0
        while True:
            with self._condition:
                if self._closed or (
                    self._closing and self._pending is None and not self._buffer
                ):
                    break
                output = self._output
            if output is None:
                try:
                    output = self._open_output()
                except Exception as exc:
                    failures += 1
                    self._record_error("open", exc, attempt=failures)
                    self._wait_to_retry()
                    continue
                with self._condition:
                    self._output = output
                    self._notifier = output.notifier
                self._record("io.open", attempt=failures + 1)
            pending = self._next_pending()
            if pending is None:
                continue
            data, step = pending
            try:
                output.write_data(data)
            except Exception as exc:
                failures += 1
                self._record_error("write", exc, step=step, attempt=failures)
                self._discard(output)
                self._wait_to_retry()
                continue
            failures = 0
            self._error = None
            with self._condition:
                self._pending = None
                self._written_steps += 1

        with self._condition:
            output, self._output = self._output, None
        if output is not None:
            self._quietly("close", output.close)

    def _quietly(self, stage, end):
        # Ending an output that already failed must not stop the worker or close().
        try:
            end()
        except Exception as exc:
            self._record_error(stage, exc)

    def _next_pending(self):
        """The step to write: the one that failed, else the oldest queued.

        None when closing with nothing queued, or when the step could not be
        prepared (it is dropped).
        """
        with self._condition:
            if self._pending is not None:
                return self._pending
            self._condition.wait_for(lambda: self._buffer or self._closing)
            if not self._buffer:
                return None
            self._pending = self._buffer.popleft()
            if self._capacity > 1 and self._next_drop_index == 0:
                self._next_drop_index = 1
            elif self._next_drop_index > 0:
                self._next_drop_index -= 1
            if self._prepare is None:
                return self._pending
            data, step = self._pending
        try:
            prepared = (self._prepare(data, step), step)
        except Exception as exc:
            # The same object would fail again: drop it rather than retry.
            self._record_error("prepare", exc, step=step)
            prepared = None
        with self._condition:
            self._pending = prepared
            if prepared is None:
                self._dropped_steps += 1
        return prepared

    def _wait_to_retry(self):
        with self._condition:
            self._condition.wait_for(
                lambda: self._closed, timeout=self._retry_delay_seconds
            )

    def _discard(self, output):
        with self._condition:
            if self._output is output:
                self._output = None
        self._quietly("abort", output.abort)
        self._quietly("close", output.close)

    def close(self, timeout=None):
        """Stop accepting steps and let the worker drain for at most ``timeout`` s.

        ``timeout=None`` waits until every queued step is written. Steps still
        queued or in flight after the timeout are counted in ``unsent_steps``,
        and the output is aborted (a socket stops at once), so the worker stops
        waiting on it.
        """
        with self._condition:
            if self._closing:
                return
            self._closing = True
            self._condition.notify_all()

        self._worker.join(timeout)

        with self._condition:
            drained = not self._worker.is_alive()
            self._closed = True
            self._condition.notify_all()
            self._unsent_steps = len(self._buffer) + (self._pending is not None)
            output = self._output
            counts = {
                "written_steps": self._written_steps,
                "dropped_steps": self._dropped_steps,
                "unsent_steps": self._unsent_steps,
            }
        if not drained and output is not None:
            self._quietly("abort", output.abort)
        self._record("io.close", drained=drained, **counts)
