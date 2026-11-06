import json
import subprocess
import sys
import tempfile
import unittest
import os
import stat
from unittest import mock
from pathlib import Path

from streamer.remote_consumer_io import ServerConfig, SSHConsumerLauncher
from streamer.output_permissions import output_directory, share_output
from streamer.consumer_output import RotatingConsumerOutput
from streamer.timing_log import TimingLog
from streamer.pickle_io import PickleIO
from types import SimpleNamespace
import numpy as np


class OutputLayoutTests(unittest.TestCase):
    def config(self, root):
        path = root / "server.conf"
        path.write_text(
            "[consumer]\n"
            "host = example\n"
            f"working_directory = {root}\n"
            f"python = {sys.executable}\n"
            "script = fake_consumer.py\n"
            "output_directory = output with spaces\n"
            "public_key = test.pub\n"
            "allow_ip_range = 127.0.0.1/32\n"
            "startup_timeout_seconds = 2\n"
        )
        return ServerConfig(path)

    def test_new_producer_sessions_have_distinct_log_sets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.config(root)
            second = self.config(root)
            self.assertNotEqual(first.timing_log, second.timing_log)
            self.assertEqual(first.data_directory, "output with spaces/data")
            for path in (first.connection_file, first.stdout_log, first.pid_file, first.timing_log):
                self.assertEqual(Path(path).parent, Path("output with spaces/logs"))
                self.assertTrue(Path(path).name.startswith("consumer-"))
            self.assertEqual(
                SSHConsumerLauncher(first)._consumer_arguments()[-2:],
                [first.connection_file, first.data_directory],
            )

    def test_launch_uses_server_timezone_once_for_session_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            launcher = SSHConsumerLauncher(config)
            stamp = "20261105T233000.123456-0800"
            with mock.patch.object(launcher, "_run_remote", side_effect=[stamp, '{"ready": true}', '{"ready": true}']) as run:
                self.assertEqual(launcher.launch(), {"ready": True})
                self.assertIn(stamp, config.timing_log)
                self.assertEqual(launcher.launch(), {"ready": True})
                self.assertEqual(run.call_count, 3)
                self.assertIn(stamp, run.call_args_list[1].args[0])
                self.assertIn(stamp, run.call_args_list[2].args[0])

    def test_launch_creates_directories_and_preserves_previous_session_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fake_consumer.py").write_text(
                "import json, sys\n"
                "from pathlib import Path\n"
                "log = Path(sys.argv[sys.argv.index('--timing-log') + 1])\n"
                "with log.open('a') as stream: stream.write('timing\\n')\n"
                "print('consumer started', flush=True)\n"
                "Path(sys.argv[-2]).write_text(json.dumps({'ready': True}))\n"
            )
            first = self.config(root)
            for config in (first, first, self.config(root)):
                completed = subprocess.run(
                    ["sh", "-c", SSHConsumerLauncher(config)._remote_command()],
                    capture_output=True, text=True, timeout=5, check=True,
                )
                self.assertEqual(json.loads(completed.stdout), {"ready": True})
            self.assertEqual((root / first.timing_log).read_text(), "timing\ntiming\n")
            self.assertEqual((root / first.stdout_log).read_text(), "consumer started\n" * 2)
            self.assertEqual(len(list((root / first.log_directory).glob("*.jsonl"))), 2)
            for config in (first, config):
                for name in (config.stdout_log, config.pid_file, config.log_directory):
                    self.assertEqual(stat.S_IMODE((root / name).stat().st_mode), 0o775)

    def test_outputs_override_restrictive_umask_without_changing_existing_parents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent_mode = stat.S_IMODE(root.stat().st_mode)
            old_mask = os.umask(0o077)
            try:
                base = output_directory(root / 'shared' / 'nested')
                with mock.patch('streamer.consumer_output.ADIOS2_AVAILABLE', False):
                    output = RotatingConsumerOutput(base / 'pickle', 'BP5')
                    output.write_data({'a': np.arange(3)})
                    output.close()
                output = RotatingConsumerOutput(base / 'adios', 'BP5')
                output.write_data({'a': np.arange(3)})
                for path in (base / 'adios').rglob('*'):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o777, 0o775)
                output.close()
                log = TimingLog(base / 'logs' / 'timing.jsonl')
                log.record('test')
                log.close()
                for path in [root / 'shared', *(root / 'shared').rglob('*')]:
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o777, 0o775, str(path))
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), parent_mode)
            finally:
                os.umask(old_mask)


if __name__ == "__main__":
    unittest.main()
