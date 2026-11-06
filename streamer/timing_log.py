import json
import threading
from datetime import datetime
from pathlib import Path

from .output_permissions import output_directory, share_output


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="microseconds")


class TimingLog:
    """Append line-delimited JSON timing records and flush each record."""

    def __init__(self, path):
        output_directory(Path(path).parent)
        self._stream = open(path, "a", encoding="utf-8", buffering=1)
        share_output(path)
        self._lock = threading.Lock()

    def record(self, event, **fields):
        record = {
            "logged_at": timestamp(),
            "event": event,
            **fields,
        }
        with self._lock:
            json.dump(record, self._stream, separators=(",", ":"))
            self._stream.write("\n")

    def try_record(self, event, **fields):
        """record(), ignoring a closed log or a write error: for threads that must not stop on logging."""
        try:
            self.record(event, **fields)
        except Exception:
            pass

    def close(self):
        with self._lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
