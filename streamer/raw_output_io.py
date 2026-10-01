import json
import math
import threading
import time
from collections import deque
from abc import ABC, abstractmethod
from pathlib import Path

from .connection_security import decrypt_connection_info, load_private_key
from .data_operations import load_data_operation


class IO(ABC):
    """Common producer output interface."""

    @classmethod
    def create(cls, settings, timing_log=None):
        from .remote_consumer_io import ServerConfig

        if (
            Path(settings.destination).suffix.lower() == ".conf"
            or ServerConfig.is_server_config(settings.destination)
        ):
            from .remote_consumer_io import RestartingSingleSocketIO

            private_key = cls._load_required_private_key(settings)
            output = RestartingSingleSocketIO(
                settings, private_key, timing_log=timing_log
            )
            return BufferedIO(
                output,
                buffer_seconds=getattr(settings, "buffer_seconds", 600.0),
                output_interval_seconds=getattr(
                    settings, "output_interval_seconds", 3.0
                ),
                timing_log=timing_log,
                notifier=output.notifier,
            )

        connection_info = cls._read_connection_info(
            settings.destination, getattr(settings, "private_key", None)
        )
        io_id = connection_info.get("id", "adios") if connection_info else "adios"

        if io_id == "singlesocket":
            from .single_socket_io import SingleSocketIO

            private_key = cls._load_required_private_key(settings)
            operation = load_data_operation(
                getattr(settings, "compression_config", None)
            )
            output = SingleSocketIO(
                settings, connection_info, private_key, operation=operation
            )
        elif io_id == "adios":
            from .adios_io import AdiosIO

            output = AdiosIO(settings, connection_info)
        else:
            raise ValueError(f"Unsupported IO id in {settings.destination}: {io_id}")

        return BufferedIO(
            output,
            buffer_seconds=getattr(settings, "buffer_seconds", 600.0),
            output_interval_seconds=getattr(
                settings, "output_interval_seconds", 3.0
            ),
            timing_log=timing_log,
        )

    @staticmethod
    def _load_required_private_key(settings):
        private_key_path = getattr(settings, "private_key", None)
        if not private_key_path:
            raise ValueError("A private key is required for socket output")
        return load_private_key(private_key_path)

    @staticmethod
    def _read_connection_info(destination, private_key_path):
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
        if not private_key_path:
            raise ValueError("A private key is required for socket output")
        private_key = load_private_key(private_key_path)
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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class BufferedIO(IO):
    """Queue output steps and write them from a dedicated worker thread."""

    HIGH_BUFFER_PERCENT = 80
    HIGH_BUFFER_NOTIFICATION_INTERVAL_SECONDS = 3600

    def __init__(
        self,
        output,
        buffer_seconds,
        output_interval_seconds,
        timing_log=None,
        notifier=None,
        clock=time.monotonic,
    ):
        if not math.isfinite(buffer_seconds) or buffer_seconds <= 0:
            raise ValueError("Buffer duration must be greater than zero")
        if not math.isfinite(output_interval_seconds) or output_interval_seconds <= 0:
            raise ValueError("Output interval must be greater than zero")

        self._output = output
        self._timing_log = timing_log
        self._notifier = notifier
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
        self._condition = threading.Condition()
        self._closing = False
        self._closed = False
        self._error = None
        self._dropped_steps = 0
        self._next_drop_index = 1 if self._capacity > 1 else 0
        self._worker = threading.Thread(
            target=self._write_pending,
            name="raw-output-writer",
        )
        self._worker.start()

    @property
    def capacity(self):
        return self._capacity

    @property
    def dropped_steps(self):
        with self._condition:
            return self._dropped_steps

    def write(self, data, step=None):
        # RawShot owns fresh arrays for each acquisition, so a shallow mapping copy
        # is sufficient to keep the queued variable set stable.
        self._enqueue((dict(data), step))

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
            self._condition.notify()

        if self._notifier is not None:
            for percent in low_notifications:
                priority = 4 if percent >= 60 else 3
                self._notifier.notify(
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
                self._notifier.notify(
                    "Output ring buffer at least 80% full",
                    f"The output ring buffer contains {depth} of "
                    f"{self._capacity} queued snapshots.{loss_message}",
                    priority=5,
                    tags=("warning",),
                )

        if dropped is not None and self._timing_log is not None:
            self._timing_log.record(
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
        if self._error is not None:
            raise RuntimeError("Background output writer failed") from self._error
        if self._closing or self._closed:
            raise RuntimeError("Cannot write to closed output")

    def _write_pending(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._buffer or self._closing)
                    if not self._buffer:
                        return
                    data, _step = self._buffer.popleft()
                    if self._capacity > 1 and self._next_drop_index == 0:
                        self._next_drop_index = 1
                    elif self._next_drop_index > 0:
                        self._next_drop_index -= 1
                self._output.write_data(data)
        except BaseException as exc:
            with self._condition:
                self._error = exc
                self._dropped_steps += len(self._buffer)
                self._buffer.clear()
                self._closing = True
                self._condition.notify_all()

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closing = True
            self._condition.notify_all()

        self._worker.join()

        close_error = None
        try:
            self._output.close()
        except BaseException as exc:
            close_error = exc

        with self._condition:
            self._closed = True
            writer_error = self._error

        if writer_error is not None:
            raise RuntimeError("Background output writer failed") from writer_error
        if close_error is not None:
            raise close_error
