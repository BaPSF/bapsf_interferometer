import os
import unittest
from datetime import datetime, timezone
from unittest import mock
from pathlib import Path

from streamer.local_timezone import timezone_description, timezone_from_description


class LocalTimezoneTests(unittest.TestCase):
    def test_override_preserves_dst_rules(self):
        with mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
            zone = timezone_from_description(timezone_description())
        self.assertEqual(datetime(2026, 1, 1, tzinfo=zone).strftime("%z"), "-0800")
        self.assertEqual(datetime(2026, 7, 1, tzinfo=zone).strftime("%z"), "-0700")

    @unittest.skipUnless(Path("/etc/localtime").is_file(), "System has no TZif file")
    def test_system_tzif_round_trip(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            description = timezone_description()
        self.assertIn("tzif", description)
        zone = timezone_from_description(description)
        self.assertIsNotNone(datetime.now(zone).utcoffset())

    def test_empty_tz_uses_utc(self):
        with mock.patch.dict(os.environ, {"TZ": ""}), \
                mock.patch('streamer.local_timezone.datetime') as clock:
            clock.now.return_value.astimezone.return_value = datetime.now(timezone.utc)
            zone = timezone_from_description(timezone_description())
        self.assertEqual(datetime.now(zone).strftime("%z"), "+0000")
