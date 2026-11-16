"""Consumer launch backoff, retained until a connection succeeds."""
import time


class ConsumerLaunchBackoff:
    MAX_DELAY_SECONDS = 960.0

    def __init__(self, initial_delay_seconds=15.0):
        self.initial_delay_seconds = min(initial_delay_seconds, self.MAX_DELAY_SECONDS)
        self.reset()

    def reset(self):
        self._delay = self.initial_delay_seconds
        self._failures = 0
        self._retry_at = 0.0

    def failed(self):
        self._failures += 1
        if self._failures > 2:
            self._delay = min(self._delay * 2, self.MAX_DELAY_SECONDS)
        self._retry_at = time.monotonic() + self._delay

    def remaining(self):
        return max(0.0, self._retry_at - time.monotonic())
