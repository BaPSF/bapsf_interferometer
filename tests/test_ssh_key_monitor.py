import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from streamer.raw_output_io import IO
from streamer.remote_consumer_io import RestartingSingleSocketIO
from streamer.ssh_key_monitor import CertificateValidity, SSHKeyExpiryChecker


class KeyMonitorLifecycleTests(unittest.TestCase):
    def test_buffer_keeps_monitor_across_output_open_failures(self):
        config = SimpleNamespace(local_fallback_directory=None)
        settings = SimpleNamespace(destination='server.conf', retry_delay_seconds=0)
        written = threading.Event()
        destination = mock.Mock()
        destination.write_data.side_effect = lambda data: written.set()
        opener = mock.Mock(side_effect=[OSError('outage'), EOFError('closed'), destination])
        with mock.patch('streamer.remote_consumer_io.ServerConfig', return_value=config), \
                mock.patch.object(IO, '_output_opener', return_value=opener), \
                mock.patch('streamer.ssh_key_monitor.SSHKeyMonitorProcess') as monitor:
            output = IO.create(settings)
            try:
                output.write({'shot_index': 1})
                self.assertTrue(written.wait(3))
                self.assertEqual(opener.call_count, 3)
                monitor.assert_called_once_with(config)
                monitor.return_value.close.assert_not_called()
            finally:
                output.close(1)
            output.close(1)
            monitor.return_value.close.assert_called_once()

    def test_failed_ssh_launch_does_not_create_a_key_monitor(self):
        config = SimpleNamespace(
            host='test-server', retry_delay_seconds=0,
            ssh_key_certificate='cert.pub', ntfy_topic_alert='test-topic',
        )
        with mock.patch('streamer.ssh_key_monitor.SSHKeyMonitorProcess') as monitor, \
                mock.patch.object(RestartingSingleSocketIO, '_new_output',
                                  side_effect=OSError('outage')):
            for _ in range(3):
                with self.assertRaisesRegex(OSError, 'outage'):
                    RestartingSingleSocketIO(
                        SimpleNamespace(), None, config=config, launcher=mock.Mock(),
                        single_attempt=True, notifications=False,
                    )
            monitor.assert_not_called()

    def test_repeated_checks_warn_once_and_replacement_resets_schedule(self):
        notifier = mock.Mock()
        reader = mock.Mock(return_value=CertificateValidity(0, 24 * 3600))
        checker = SSHKeyExpiryChecker('cert.pub', notifier, reader=reader)
        for attempt in range(6):
            checker.check(now=18 * 3600 + attempt * 1025)
        notifier.notify.assert_called_once()
        self.assertEqual(notifier.notify.call_args.args[0],
                         'NERSC SSH key expires in 6 hours')
        reader.return_value = CertificateValidity(3600, 25 * 3600)
        checker.check(now=20 * 3600)
        self.assertEqual(notifier.notify.call_count, 2)
