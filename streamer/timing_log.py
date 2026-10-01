import json
import threading
from datetime import datetime


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="microseconds")


class TimingLog:
    """Append line-delimited JSON timing records and flush each record."""

    def __init__(self, path):
        self._stream = open(path, "a", encoding="utf-8", buffering=1)
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

    def close(self):
        with self._lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
