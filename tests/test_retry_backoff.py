import unittest
from unittest import mock

from streamer.retry_backoff import ConsumerLaunchBackoff


class ConsumerLaunchBackoffTests(unittest.TestCase):
    def test_elapsed_time_does_not_reset_failure_history(self):
        with mock.patch("streamer.retry_backoff.time.monotonic", return_value=100) as clock:
            backoff = ConsumerLaunchBackoff()
            for _ in range(2):
                backoff.failed()
                self.assertEqual(backoff.remaining(), 15)
            clock.return_value = 10000
            self.assertEqual(backoff.remaining(), 0)
            backoff.failed()
            self.assertEqual(backoff.remaining(), 30)
            backoff.reset()
            self.assertEqual(backoff.remaining(), 0)
            backoff.failed()
            self.assertEqual(backoff.remaining(), 15)

    def test_attempt_duration_reduces_remaining_wait(self):
        with mock.patch("streamer.retry_backoff.time.monotonic", return_value=100) as clock:
            backoff = ConsumerLaunchBackoff()
            backoff.failed()
            clock.return_value = 105
            self.assertEqual(backoff.remaining(), 10)
