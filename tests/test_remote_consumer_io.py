import json
import io
import socket
import unittest
from types import SimpleNamespace
from unittest import mock

import nacl.public

from streamer.connection_security import encrypt_connection_info
from streamer.remote_consumer_io import RestartingSingleSocketIO, SSHConsumerLauncher, SSHStreamSocket
from streamer.single_socket_io import SingleSocketIO
from streamer.ntfy_notifications import NtfyNotifier, MAX_NOTIFICATION_BYTES
from streamer.retry_backoff import ConsumerLaunchBackoff


class LaunchDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            host="pnorbert@dtn.nersc",
            max_relaunch_attempts=0,
            retry_delay_seconds=0,
        )
        self.timing_log = mock.Mock()
        self.info = mock.Mock()
        self.alert = mock.Mock()

    def make_output(self):
        output = RestartingSingleSocketIO(
            SimpleNamespace(),
            private_key=None,
            config=self.config,
            launcher=mock.Mock(),
            timing_log=self.timing_log,
            notifier=self.info,
            alert_notifier=self.alert,
        )
        self.addCleanup(output.close)
        return output

    def test_invalid_certificate_blocks_before_even_a_single_attempt(self):
        from streamer.ssh_key_monitor import CertificateValidity
        self.config.ssh_key_certificate = "cert.pub"
        for validity in (CertificateValidity(0, 99), CertificateValidity(101, 200),
                         ValueError("missing or unreadable certificate")):
            with self.subTest(validity=validity), \
                    mock.patch("streamer.remote_consumer_io.time.time", return_value=100), \
                    mock.patch("streamer.remote_consumer_io.read_certificate_validity",
                               side_effect=[validity, CertificateValidity(0, 200)]), \
                    mock.patch.object(RestartingSingleSocketIO, "_new_output",
                                      return_value=mock.Mock()) as launch:
                def wait(seconds):
                    launch.assert_not_called()
                    return False
                output = RestartingSingleSocketIO(
                    SimpleNamespace(), None, config=self.config, launcher=mock.Mock(),
                    single_attempt=True, notifications=False, certificate_wait=wait)
                output.close()
                launch.assert_called_once()

    def test_certificate_expiring_between_retries_blocks_next_launch(self):
        from streamer.ssh_key_monitor import CertificateValidity
        self.config.ssh_key_certificate = "cert.pub"
        with mock.patch("streamer.remote_consumer_io.time.time", return_value=100), \
                mock.patch("streamer.remote_consumer_io.read_certificate_validity",
                           side_effect=[CertificateValidity(0, 200), CertificateValidity(0, 99),
                                        CertificateValidity(0, 300)]), \
                mock.patch.object(RestartingSingleSocketIO, "_new_output",
                                  side_effect=[OSError("outage"), mock.Mock()]) as launch:
            def wait(seconds):
                self.assertEqual(launch.call_count, 1)
                return False
            output = RestartingSingleSocketIO(
                SimpleNamespace(), None, config=self.config, launcher=mock.Mock(),
                notifications=False, certificate_wait=wait)
            output.close()
            self.assertEqual(launch.call_count, 2)

    def test_certificate_wait_can_be_cancelled_without_launching(self):
        from streamer.ssh_key_monitor import CertificateValidity
        self.config.ssh_key_certificate = "cert.pub"
        with mock.patch("streamer.remote_consumer_io.time.time", return_value=100), \
                mock.patch("streamer.remote_consumer_io.read_certificate_validity",
                           return_value=CertificateValidity(0, 99)), \
                mock.patch.object(RestartingSingleSocketIO, "_new_output") as launch:
            with self.assertRaisesRegex(RuntimeError, "aborted"):
                RestartingSingleSocketIO(
                    SimpleNamespace(), None, config=self.config, launcher=mock.Mock(),
                    notifications=False, certificate_wait=lambda seconds: True)
            launch.assert_not_called()

    def test_initial_failures_log_each_error_but_alert_only_once_then_report_success(self):
        failures = [OSError("Permission denied (publickey)."), EOFError("Connection closed")]
        with mock.patch.object(RestartingSingleSocketIO, "_new_output", side_effect=[*failures, mock.Mock()]):
            self.make_output()

        retries = [call for call in self.timing_log.record.call_args_list
                   if call.args == ("consumer.launch_retry",)]
        self.assertEqual(len(retries), 2)
        for attempt, (call, error) in enumerate(zip(retries, failures), start=1):
            self.assertEqual(call.kwargs, dict(
                reason="launch", attempt=attempt, host=self.config.host,
                error_type=type(error).__name__, error=str(error),
            ))
        self.alert.notify.assert_called_once_with(
            "Raw data server start failed",
            f"Start attempt 1 for the remote consumer on {self.config.host} failed: "
            "OSError: Permission denied (publickey). See producer log for details.",
            priority=5, tags=("rotating_light",),
        )
        self.info.notify.assert_called_once_with(
            "Raw data server started",
            f"Remote consumer on {self.config.host} started (attempt 3).",
            priority=3, tags=("white_check_mark",),
        )

    def test_successful_initial_launch_sends_no_failure_alert(self):
        with mock.patch.object(RestartingSingleSocketIO, "_new_output", return_value=mock.Mock()):
            self.make_output()
        self.alert.notify.assert_not_called()
        self.info.notify.assert_called_once()

    def test_launch_backoff_caps_and_resets_only_on_success(self):
        self.config.retry_delay_seconds = 15
        failure = OSError("server unavailable")
        with mock.patch("streamer.retry_backoff.time.monotonic", return_value=100), \
                mock.patch("streamer.remote_consumer_io.threading.Event") as event, \
                mock.patch.object(RestartingSingleSocketIO, "_new_output",
                                  side_effect=[failure] * 10 + [mock.Mock()]):
            output = self.make_output()
            self.assertEqual([call.args[0] for call in event.return_value.wait.call_args_list],
                             [0, 15, 15, 30, 60, 120, 240, 480, 960, 960, 960])
            event.return_value.wait.reset_mock()
            with mock.patch.object(output, "_new_output", side_effect=[failure, mock.Mock()]):
                output._connect_with_retries("connection_lost")
            self.assertEqual([call.args[0] for call in event.return_value.wait.call_args_list],
                             [0, 15])

    def test_failed_launch_series_preserves_backoff(self):
        self.config.retry_delay_seconds = 15
        self.config.max_relaunch_attempts = 1
        backoff = ConsumerLaunchBackoff(15)
        with mock.patch("streamer.retry_backoff.time.monotonic", return_value=100), \
                mock.patch("streamer.remote_consumer_io.threading.Event") as event, \
                mock.patch.object(RestartingSingleSocketIO, "_new_output", side_effect=OSError("outage")):
            for _ in range(4):
                with self.assertRaises(OSError):
                    RestartingSingleSocketIO(SimpleNamespace(), None, config=self.config,
                        launcher=mock.Mock(), notifier=self.info, alert_notifier=self.alert,
                        launch_backoff=backoff)
            self.assertEqual([call.args[0] for call in event.return_value.wait.call_args_list],
                             [0, 15, 15, 30])

    def test_each_new_producer_output_alerts_again(self):
        for _ in range(3):
            with mock.patch.object(RestartingSingleSocketIO, "_new_output", side_effect=[
                OSError("Host key verification failed."), mock.Mock(),
            ]):
                self.make_output()
        self.assertEqual(self.alert.notify.call_count, 3)

    def test_mismatched_keys_log_and_alert_without_claiming_success(self):
        self.config.max_relaunch_attempts = 1
        private_key = nacl.public.PrivateKey.generate()
        other_key = nacl.public.PrivateKey.generate()
        launcher = mock.Mock()
        launcher.launch.return_value = json.loads(encrypt_connection_info(
            {"id": "singlesocket"}, other_key.public_key,
        ))
        with mock.patch("streamer.remote_consumer_io.SingleSocketIO") as socket_output:
            with self.assertRaisesRegex(ValueError, "Unable to decrypt"):
                RestartingSingleSocketIO(
                    SimpleNamespace(), private_key, config=self.config,
                    launcher=launcher, timing_log=self.timing_log,
                    notifier=self.info, alert_notifier=self.alert,
                )
        socket_output.assert_not_called()
        self.info.notify.assert_not_called()
        self.alert.notify.assert_called_once()
        self.assertIn("Unable to decrypt", self.alert.notify.call_args.args[1])
        self.timing_log.record.assert_called_once_with(
            "consumer.launch_retry", reason="launch", attempt=1, host=self.config.host,
            error_type="ValueError",
            error="Unable to decrypt connection information with the private key",
        )

    def test_restart_failure_still_alerts_once_per_series(self):
        with mock.patch.object(RestartingSingleSocketIO, "_new_output", return_value=mock.Mock()):
            output = self.make_output()
        self.info.reset_mock()
        for _ in range(2):
            with mock.patch.object(output, "_new_output", side_effect=[
                OSError("Connection refused"), OSError("Connection refused"), mock.Mock(),
            ]):
                output._connect_with_retries("connection_lost")
        self.assertEqual(self.alert.notify.call_count, 2)
        self.assertEqual(self.alert.notify.call_args.args[0], "Raw data server restart failed")
        self.assertEqual(self.info.notify.call_count, 2)
        self.assertEqual(self.info.notify.call_args.args[0], "Raw data server restarted")

    def test_last_allowed_attempt_logs_and_alerts_before_raising(self):
        self.config.max_relaunch_attempts = 1
        with mock.patch.object(RestartingSingleSocketIO, "_new_output", side_effect=OSError("SSH failed")):
            with self.assertRaisesRegex(OSError, "SSH failed"):
                self.make_output()
        self.alert.notify.assert_called_once()
        self.timing_log.record.assert_called_once_with(
            "consumer.launch_retry", reason="launch", attempt=1, host=self.config.host,
            error_type="OSError", error="SSH failed",
        )

    def test_verbose_launch_failure_is_short_in_alert_and_complete_in_log(self):
        details = 'Controlled streaming outage\n' + 'Receiving and writing step N\n' * 500
        self.config.max_relaunch_attempts = 1
        with mock.patch.object(RestartingSingleSocketIO, '_new_output', side_effect=OSError(details)):
            with self.assertRaises(OSError):
                self.make_output()
        body = self.alert.notify.call_args.args[1]
        self.assertIn('Controlled streaming outage', body)
        self.assertNotIn('Receiving and writing', body)
        self.assertLessEqual(len(body.encode()), MAX_NOTIFICATION_BYTES)
        self.assertEqual(self.timing_log.record.call_args.kwargs['error'], details)

    def test_ssh_failure_preserves_stderr_in_exception(self):
        self.config.ssh_command = ["ssh"]
        self.config.automatic_paths = False
        self.config.startup_timeout_seconds = 1
        launcher = SSHConsumerLauncher(self.config)
        with mock.patch.object(launcher, "_remote_command", return_value="remote command"), \
                mock.patch("streamer.remote_consumer_io.subprocess.run", return_value=SimpleNamespace(
                    returncode=255, stderr="Permission denied (publickey).\n", stdout="",
                )):
            with self.assertRaisesRegex(OSError, r"Permission denied \(publickey\)"):
                launcher.launch()


class SSHAbortTests(unittest.TestCase):
    def test_disconnected_ssh_socket_can_be_aborted_before_relaunch(self):
        connection, peer = socket.socketpair()
        self.addCleanup(peer.close)
        self.addCleanup(connection.close)
        process = mock.Mock()
        errors = io.BytesIO()
        tunnel = SSHStreamSocket(connection, process, errors)
        output = SingleSocketIO.__new__(SingleSocketIO)
        output._socket = tunnel
        peer.close()
        output.abort()
        output.abort()
        self.assertIsNone(output._socket)
        self.assertEqual(connection.fileno(), -1)
        self.assertTrue(errors.closed)
        process.wait.assert_called_once_with(timeout=.2)


class NotificationSizeTests(unittest.TestCase):
    def test_long_unicode_message_has_a_byte_limit_and_keeps_valid_utf8(self):
        opener, log = mock.MagicMock(), mock.Mock()
        notifier = NtfyNotifier('test-topic', opener=opener, timing_log=log)
        message = 'Streaming failure: ' + '⚠️ logs\n' * 2000
        notifier.notify('Failure', message)
        notifier.close()
        body = opener.call_args.args[0].data
        self.assertLessEqual(len(body), MAX_NOTIFICATION_BYTES)
        self.assertTrue(body.decode().endswith('See producer log for details.'))
        log.record.assert_any_call('notification.truncated', title='Failure',
                                   original_bytes=len(message.encode()))

    def test_short_message_is_unchanged(self):
        opener = mock.MagicMock()
        notifier = NtfyNotifier('test-topic', opener=opener)
        notifier.notify('Started', 'Server started.')
        notifier.close()
        self.assertEqual(opener.call_args.args[0].data, b'Server started.')


if __name__ == "__main__":
    unittest.main()
