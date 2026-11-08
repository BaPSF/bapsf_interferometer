import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import nacl.public
import numpy as np

from streamer.fallback_io import FallbackIO
from streamer.single_socket_io import SingleSocketIO
from streamer import socket_protocol as protocol
from streamer.remote_consumer_io import ServerConfig


def wait_until(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Timed out waiting for worker")


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = SimpleNamespace(host="test-server", local_fallback_directory=Path(self.tmp.name),
                                      retry_delay_seconds=.02, file_interval_seconds=3600)
        self.settings = SimpleNamespace(buffer_seconds=10, output_interval_seconds=1)
        self.ready = threading.Event()
        self.remote_frames = []
        self.local_frames = []
        self.log = mock.Mock()
        self.notifier = mock.Mock()
        self.disk = mock.patch('streamer.fallback_io.shutil.disk_usage',
                               return_value=SimpleNamespace(free=10_000_000_000)).start()
        self.addCleanup(mock.patch.stopall)
        self.local = mock.Mock()
        self.local.write_data.side_effect = lambda d: self.local_frames.append(int(d['shot_index']))
        mock.patch('streamer.fallback_io.RotatingConsumerOutput', return_value=self.local).start()
        mock.patch('streamer.fallback_io.NtfyNotifier.from_config', return_value=self.notifier).start()
        self.factory = mock.patch('streamer.fallback_io.RestartingSingleSocketIO', side_effect=self.connect).start()

    def connect(self, *args, **kwargs):
        if not self.ready.is_set():
            raise OSError('outage')
        remote = mock.Mock()
        remote._output.clock_estimate = (5, .1, time.time()+5, time.monotonic(), ZoneInfo("America/Los_Angeles"))
        remote.write_data.side_effect = lambda d: self.remote_frames.append(int(d['shot_index']))
        return remote

    def output(self):
        output = FallbackIO(self.settings, self.config, None, timing_log=self.log)
        self.addCleanup(output.close, .1)
        return output

    def enqueue(self, output, start, end):
        for i in range(start, end):
            output.write({'shot_index': np.array(i)}, step=i)

    def test_successful_start_notifies_without_any_frames_or_fallback(self):
        self.ready.set()
        output = self.output()
        wait_until(lambda: any(c.args[0] == 'Raw data server started'
                              for c in self.notifier.notify.call_args_list))
        self.assertEqual(output._local_written, 0)
        self.assertEqual(output._remote_written, 0)
        self.assertEqual([c.args[0] for c in self.notifier.notify.call_args_list],
                         ['Raw data server started'])

    def test_failed_launch_alerts_once_until_success_and_rearms_after_disconnect(self):
        output = self.output()
        wait_until(lambda: self.factory.call_count >= 3)
        titles = lambda: [c.args[0] for c in self.notifier.notify.call_args_list]
        self.assertEqual(titles().count('Raw data server start failed'), 1)
        self.ready.set()
        wait_until(lambda: 'Raw data server started' in titles())
        self.ready.clear()
        output._remote.write_data.side_effect = OSError('connection lost')
        self.enqueue(output, 0, 1)
        wait_until(lambda: 'Raw data server restart failed' in titles())
        time.sleep(.08)
        self.assertEqual(titles().count('Raw data server disconnected'), 1)
        self.assertEqual(titles().count('Raw data server restart failed'), 1)
        self.ready.set()
        wait_until(lambda: 'Raw data server restarted' in titles())
        self.assertEqual(titles().count('Raw data server restarted'), 1)

    def test_boundary_fifo_and_remote_recovery_transitions(self):
        output = self.output()
        self.enqueue(output, 0, 8)
        time.sleep(.06)
        self.assertEqual(self.local_frames, [])
        self.enqueue(output, 8, 9)
        wait_until(lambda: output.written_steps == 9)
        self.assertEqual(self.local_frames, list(range(9)))
        self.ready.set()
        wait_until(lambda: output._remote is not None)
        self.enqueue(output, 9, 11)
        wait_until(lambda: output.written_steps == 11)
        self.assertEqual(self.remote_frames, [9, 10])
        messages = [c.args[0] for c in self.notifier.notify.call_args_list]
        self.assertEqual(messages.count('Saving raw data locally'), 1)
        self.assertEqual(messages.count('Remote streaming resumed'), 1)

    def test_verbose_launch_error_keeps_logs_out_of_notification(self):
        details = 'Controlled streaming outage\n' + 'Receiving and writing step N\n' * 500
        self.factory.side_effect = OSError(details)
        self.output()
        wait_until(lambda: self.notifier.notify.called)
        body = self.notifier.notify.call_args.args[1]
        self.assertIn('Controlled streaming outage', body)
        self.assertNotIn('Receiving and writing', body)
        self.assertLessEqual(len(body.encode()), 512)
        self.log.try_record.assert_any_call('io.error', stage='remote', error_type='OSError', error=details)

    def test_low_space_preserves_frames_and_overflow_is_bounded(self):
        self.disk.return_value.free = 9_999_999_999
        output = self.output()
        self.enqueue(output, 0, 9)
        wait_until(lambda: output._transition_state == 'paused')
        self.assertEqual(output.dropped_steps, 0)
        self.assertEqual(output.written_steps, 0)
        self.enqueue(output, 9, 10)
        self.assertEqual(output.dropped_steps, 0)
        self.enqueue(output, 10, 20)
        with output._condition:
            self.assertEqual(len(output._buffer) + (output._pending is not None), 10)
        self.assertEqual(output.dropped_steps, 10)
        self.disk.return_value.free = 10_000_000_000
        wait_until(lambda: output.written_steps == 10)
        self.assertEqual(self.local_frames[0], 0)
        self.assertIn('Local saving resumed', [c.args[0] for c in self.notifier.notify.call_args_list])

    def test_disk_inspection_and_write_errors_retain_pending(self):
        self.disk.side_effect = OSError('inspection failed')
        output = self.output()
        self.enqueue(output, 0, 9)
        wait_until(lambda: output._transition_state == 'paused')
        self.assertEqual(output.dropped_steps, 0)
        self.disk.side_effect = None
        self.local.write_data.side_effect = OSError('ADIOS failed')
        time.sleep(.06)
        self.assertEqual(output.written_steps, 0)
        self.local.write_data.side_effect = lambda d: self.local_frames.append(int(d['shot_index']))
        wait_until(lambda: output.written_steps == 9)
        self.assertEqual(self.local_frames, list(range(9)))

    def test_blocked_remote_write_releases_ownership_before_local_write(self):
        started, cancelled, release = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        remote = mock.Mock()
        remote._output.clock_estimate = (0, 0, time.time(), time.monotonic(), ZoneInfo("America/Los_Angeles"))
        def write(data):
            started.set()
            cancelled.wait(3)
            release.wait(3)
            raise OSError('cancelled')
        remote.write_data.side_effect = write
        def abort():
            cancelled.set()
            release.wait(3)
        remote.abort.side_effect = abort
        self.factory.side_effect = [remote, OSError('outage')]
        output = self.output()
        self.enqueue(output, 0, 1)
        self.assertTrue(started.wait(2))
        self.enqueue(output, 1, 9)
        self.assertTrue(cancelled.wait(2))
        self.assertEqual(self.local_frames, [])
        start = time.monotonic()
        self.enqueue(output, 9, 10)
        self.assertLess(time.monotonic() - start, .1)
        release.set()
        wait_until(lambda: output.written_steps == 10)
        self.assertEqual(self.local_frames, list(range(10)))

    def test_shutdown_during_blocked_startup(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def blocked(*args, **kwargs):
            release.wait(3)
            raise OSError('outage')
        self.factory.side_effect = blocked
        output = self.output()
        self.enqueue(output, 0, 9)
        wait_until(lambda: output.written_steps == 9)
        output.close(.05)
        self.assertFalse(output._worker.is_alive())
        release.set()
        output._recovery.join(1)
        self.assertFalse(output._recovery.is_alive())

    def test_clock_anchor_ignores_wall_clock_jumps_and_refreshes(self):
        output = self.output()
        output._estimate = (10, .2, 1000, 100, ZoneInfo("America/Los_Angeles"))
        with mock.patch('streamer.fallback_io.time.monotonic', return_value=102):
            self.assertEqual(output._timestamp().timestamp(), 1002)
        output._estimate = (-10, .2, 2000, 105, ZoneInfo("America/Los_Angeles"))
        with mock.patch('streamer.fallback_io.time.monotonic', return_value=107):
            self.assertEqual(output._timestamp().timestamp(), 2002)

    def test_no_server_uses_producer_local_timezone(self):
        output = self.output()
        local = datetime(2026, 11, 5, 23, 30, tzinfo=ZoneInfo("Asia/Tokyo"))
        with mock.patch('streamer.fallback_io.datetime') as clock:
            clock.now.return_value.astimezone.return_value = local
            self.assertEqual(output._timestamp(), local)
        self.assertTrue(output._unknown_clock_logged)

    def test_last_server_timezone_survives_outage_and_dst_change(self):
        output = self.output()
        zone = ZoneInfo("America/Los_Angeles")
        before = datetime(2026, 11, 1, 8, 59, 59, tzinfo=timezone.utc).timestamp()
        output._estimate = (0, .1, before, 100, zone)
        with mock.patch('streamer.fallback_io.time.monotonic', return_value=100):
            self.assertEqual(output._timestamp().strftime("%Y%m%d/%H%M%S%z"), "20261101/015959-0700")
        with mock.patch('streamer.fallback_io.time.monotonic', return_value=102):
            self.assertEqual(output._timestamp().strftime("%Y%m%d/%H%M%S%z"), "20261101/010001-0800")

    def test_slow_local_write_bounds_memory_and_remote_waits_for_it(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def write(data):
            started.set()
            release.wait(3)
            self.local_frames.append(int(data['shot_index']))
        self.local.write_data.side_effect = write
        output = self.output()
        self.enqueue(output, 0, 9)
        self.assertTrue(started.wait(2))
        self.ready.set()
        wait_until(lambda: output._remote is not None)
        start = time.monotonic()
        self.enqueue(output, 9, 20)
        self.assertLess(time.monotonic() - start, .2)
        self.assertEqual(output.dropped_steps, 10)
        self.assertEqual(self.remote_frames, [])
        self.local.close.assert_not_called()
        release.set()
        wait_until(lambda: output.written_steps == 10)
        self.assertEqual(len(self.local_frames), 1)
        self.assertEqual(len(self.remote_frames), 9)
        self.local.close.assert_called_once()

    def test_repeated_outages_notify_each_transition(self):
        output = self.output()
        self.enqueue(output, 0, 9)
        wait_until(lambda: output.written_steps == 9)
        self.ready.set()
        wait_until(lambda: output._remote is not None)
        self.enqueue(output, 9, 10)
        wait_until(lambda: output.written_steps == 10)
        self.ready.clear()
        output._remote.write_data.side_effect = OSError('second outage')
        self.enqueue(output, 10, 11)
        wait_until(lambda: output._remote is None)
        self.enqueue(output, 11, 19)
        wait_until(lambda: output.written_steps == 19)
        self.ready.set()
        wait_until(lambda: output._transition_state == 'remote')
        messages = [c.args[0] for c in self.notifier.notify.call_args_list]
        self.assertEqual(messages.count('Saving raw data locally'), 2)
        self.assertEqual(messages.count('Remote streaming resumed'), 2)

    def test_capacity_one_preserves_owned_frame_and_accounts_incoming_loss(self):
        self.settings.buffer_seconds = 1
        self.disk.return_value.free = 0
        output = self.output()
        self.enqueue(output, 0, 1)
        wait_until(lambda: output._pending is not None)
        self.enqueue(output, 1, 2)
        self.assertEqual(output.dropped_steps, 1)
        self.assertEqual(int(output._pending[0]['shot_index']), 0)
        self.disk.return_value.free = 10_000_000_001
        wait_until(lambda: output.written_steps == 1)



class ProtocolTests(unittest.TestCase):
    def test_authenticated_readiness_without_a_frame(self):
        key = nacl.public.PrivateKey.generate()
        left, right = socket.socketpair()
        errors = []
        def consumer():
            try:
                protocol.verify_producer_private_key(right, key.public_key)
                names, identity = protocol.receive_hello(right)
                self.assertEqual(names, ())
                protocol.send_ready(right, identity, time.time())
                self.assertIsNone(protocol.receive_message(right))
            except Exception as exc:
                errors.append(exc)
            finally:
                right.close()
        thread = threading.Thread(target=consumer)
        thread.start()
        output = SingleSocketIO(SimpleNamespace(), dict(host='localhost', port=1234,
            consumer_id='test', protocol_version=protocol.PROTOCOL_VERSION), key,
            connector=lambda *a, **k: left)
        self.assertLess(abs(output.clock_estimate[0]), .1)
        output.close()
        thread.join(2)
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive())

    def test_offset_excludes_processing_time_for_both_skew_signs(self):
        for skew in (-50, 50):
            # outbound .2s, server processing 3s, inbound .2s
            header = dict(type='ready', protocol_version=protocol.PROTOCOL_VERSION,
                          consumer_id='test', server_receive=100+skew+.2,
                          server_send=103+skew+.2, server_timezone={"key": "America/Los_Angeles"})
            with mock.patch.object(protocol, '_receive_header', return_value=header), \
                    mock.patch.object(protocol.time, 'monotonic', return_value=13.4):
                offset, delay, _, _, zone = protocol.receive_ready(None, 'test', 100, 10)
            self.assertAlmostEqual(offset, skew)
            self.assertAlmostEqual(delay, .4)
            self.assertEqual(zone.key, "America/Los_Angeles")

    def test_explicit_version_rejection(self):
        with mock.patch.object(protocol, '_receive_header', return_value={'type':'hello', 'protocol_version':4}):
            with self.assertRaisesRegex(ValueError, 'protocol version'):
                protocol.receive_hello(None)
        with self.assertRaisesRegex(ValueError, 'protocol version'):
            SingleSocketIO(SimpleNamespace(), {'protocol_version':4}, None)

    def test_readiness_requires_output_directory_access(self):
        from streamer import consumer
        connection = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'file'
            path.write_text('not a directory')
            with mock.patch.object(consumer, 'accept_from_allowed_network', return_value=(connection, ('localhost', 1))), \
                    mock.patch.object(consumer, 'enable_keepalive'), \
                    mock.patch.object(consumer, 'verify_producer_private_key'), \
                    mock.patch.object(consumer, 'receive_hello', return_value=((), 'test')), \
                    mock.patch.object(consumer, 'send_ready') as ready:
                with self.assertRaises(OSError):
                    consumer.accept_connection(None, 'test', None, (), path)
                ready.assert_not_called()
                connection.close.assert_called_once()

    def test_config_resolves_fallback_against_config_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'server.conf'
            path.write_text('[server]\nhost=x\nworking_directory=x\npublic_key=x\nallow_ip_range=127.0.0.1/32\nlocal_fallback_directory=../fallback\n')
            self.assertEqual(ServerConfig(path).local_fallback_directory, (Path(tmp)/'../fallback').resolve())


class SocketOutageIntegrationTests(unittest.TestCase):
    def test_interrupted_consumer_falls_back_to_real_bp5_then_recovers(self):
        import adios2
        from streamer.consumer_output import RotatingConsumerOutput
        key = nacl.public.PrivateKey.generate()
        recover = threading.Event()
        interrupted = threading.Event()
        remote_frames, threads, errors = [], [], []
        attempts = [0]

        class Remote:
            def __init__(self, output):
                self._output = output
            def write_data(self, data):
                self._output.write_data(data)
            def abort(self):
                self._output.abort()
            def close(self):
                self._output.close()

        def connect(*args, **kwargs):
            attempts[0] += 1
            first = attempts[0] == 1
            if not first and not recover.is_set():
                raise OSError('consumer unavailable')
            left, right = socket.socketpair()
            def consume():
                try:
                    protocol.verify_producer_private_key(right, key.public_key)
                    _, identity = protocol.receive_hello(right)
                    protocol.send_ready(right, identity, time.time())
                    while True:
                        data = protocol.receive_message(right)
                        if data is None:
                            return
                        if first:
                            interrupted.set()
                            return  # close before ack: the pending frame must be retained
                        remote_frames.append(int(data['shot_index']))
                        protocol.send_ack(right)
                except (EOFError, OSError):
                    pass
                except Exception as exc:
                    errors.append(exc)
                finally:
                    right.close()
            thread = threading.Thread(target=consume, daemon=True)
            threads.append(thread)
            thread.start()
            return Remote(SingleSocketIO(SimpleNamespace(socket_callback=kwargs.get('socket_callback')),
                dict(host='localhost', port=1234, consumer_id='integration',
                     protocol_version=protocol.PROTOCOL_VERSION), key,
                connector=lambda *a, **k: left))

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch('streamer.fallback_io.RestartingSingleSocketIO', side_effect=connect), \
                mock.patch.object(protocol, 'timezone_description', return_value={'key': 'America/Los_Angeles'}), \
                mock.patch('streamer.fallback_io.shutil.disk_usage', return_value=SimpleNamespace(free=10_000_000_001)):
            config = SimpleNamespace(host="test-server", local_fallback_directory=Path(tmp),
                                     retry_delay_seconds=.02, file_interval_seconds=3600)
            output = FallbackIO(SimpleNamespace(buffer_seconds=10, output_interval_seconds=1), config, key)
            try:
                wait_until(lambda: output._remote is not None)
                payload = np.arange(32, dtype=np.int16)
                output.write({'shot_index': np.array(0), 'samples': payload}, step=0)
                self.assertTrue(interrupted.wait(2))
                wait_until(lambda: output._remote is None)
                for i in range(1, 9):
                    output.write({'shot_index': np.array(i), 'samples': payload+i}, step=i)
                wait_until(lambda: output.written_steps == 9)
                recover.set()
                wait_until(lambda: output._remote is not None)
                wait_until(lambda: output._local is None)
                files = list(Path(tmp).glob('*/*.bp'))
                self.assertEqual(len(files), 1)
                self.assertTrue(files[0].name.endswith(datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%z') + '.bp'))
                ids = []
                with adios2.Stream(str(files[0]), 'r') as reader:
                    for _ in reader.steps():
                        identity = int(reader.read('shot_index'))
                        ids.append(identity)
                        np.testing.assert_array_equal(reader.read('samples'), payload+identity)
                self.assertEqual(ids, list(range(9)))
                for i in range(9, 12):
                    output.write({'shot_index': np.array(i), 'samples': payload+i}, step=i)
                wait_until(lambda: output.written_steps == 12)
                self.assertEqual(remote_frames, [9, 10, 11])
                self.assertEqual(output.dropped_steps, 0)
            finally:
                output.close(.5)
            for thread in threads:
                thread.join(1)
            self.assertEqual(errors, [])
