#!/usr/bin/env python3
"""Measure real consumer restart waits through 60s over SSH, then check reset.

Runs for about three minutes. Retains evidence locally and on ubuntupc. Uses an
isolated source snapshot, ephemeral public key, and dedicated consumer port;
only this test's consumer is signalled. No clocks or retry code are mocked.
"""
import argparse
import configparser
import hashlib
import io
import json
import shlex
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import nacl.public
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streamer.remote_consumer_io import RestartingSingleSocketIO, SSHConsumerLauncher, ServerConfig
from streamer.timing_log import TimingLog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'streamer/conf/ubuntupc.conf')
    parser.add_argument('--port', type=int, default=8598)
    parser.add_argument('--run-directory', type=Path)
    args = parser.parse_args()
    if not 0 < args.port < 65536:
        parser.error('--port must be between 1 and 65535')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    run = (args.run_directory or Path('/tmp') / f'interf-backoff-test-{stamp}').resolve()
    run.mkdir(parents=True, exist_ok=False)
    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(args.config):
        parser.error(f'Cannot read {args.config}')
    section = cfg['server']
    host, workdir, python = (section[k] for k in ('host', 'working_directory', 'python'))
    ssh = shlex.split(section.get('ssh_command', 'ssh'))
    remote_root = f'{workdir}/output/backoff-test-{stamp}'
    source = remote_root + '/source'
    wrapper = remote_root + '/consumer_control.py'
    gate = remote_root + '/failures_remaining'
    key = nacl.public.PrivateKey.generate()

    def remote(code, *arguments, data=None):
        command = 'cd ' + shlex.quote(workdir) + ' && ' + shlex.join(
            [python, '-c', code, *map(str, arguments)])
        result = subprocess.run([*ssh, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host, command],
                                input=data, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr.decode() + result.stdout.decode())
        return result.stdout.decode()

    wrapper_code = f'''import signal, sys
from pathlib import Path
sys.path.insert(0, {source!r})
gate = Path({gate!r})
remaining = int(gate.read_text()) if gate.exists() else 0
if remaining:
    gate.write_text(str(remaining - 1))
    raise SystemExit("Controlled backoff test outage")
def interrupt(*args):
    raise SystemExit(0)
signal.signal(signal.SIGUSR1, interrupt)
from streamer.consumer import main
main()
'''
    archive = io.BytesIO()
    hashes = {}
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for path in sorted((ROOT / 'streamer').glob('*.py')):
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            tar.add(path, arcname='source/streamer/' + path.name)
        for name, contents in [('consumer_control.py', wrapper_code.encode()),
                               ('public_key', bytes(key.public_key))]:
            item = tarfile.TarInfo(name)
            item.size = len(contents)
            item.mode = 0o644
            tar.addfile(item, io.BytesIO(contents))
    remote('''import pathlib, socket, sys, tarfile
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', int(sys.argv[2]))); s.close()
root = pathlib.Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as tar:
    tar.extractall(root, filter='data')
''', remote_root, args.port, data=archive.getvalue())
    for name in ('connection_file', 'stdout_log', 'pid_file', 'timing_log',
                 'local_fallback_directory', 'compression_config', 'ssh_key_certificate'):
        section.pop(name, None)
    section.update(script=wrapper, output_directory=remote_root,
                   public_key=remote_root + '/public_key', socket_transport='ssh',
                   bind_host='127.0.0.1', advertise_host='127.0.0.1', allow_ip_range='127.0.0.1/32',
                   port=f'{args.port}-{args.port}', retry_delay_seconds='15',
                   max_relaunch_attempts='0', startup_timeout_seconds='5', socket_timeout_seconds='5',
                   ntfy_topic_info='', ntfy_topic_alert='')
    config_path = run / 'server.conf'
    with config_path.open('w') as stream:
        cfg.write(stream)
    config = ServerConfig(config_path)
    attempts, checks = [], []
    log = TimingLog(run / 'producer.jsonl')

    class ObservedLauncher(SSHConsumerLauncher):
        def launch(self):
            row = dict(start=time.monotonic(), at=datetime.now(timezone.utc).isoformat())
            attempts.append(row)
            try:
                result = super().launch()
                row['status'] = 'connected'
                return result
            except Exception as exc:
                row.update(status='failed', error=str(exc))
                raise
            finally:
                row['end'] = time.monotonic()
                log.record('test.launch', **row)
                print(json.dumps(row), flush=True)

    def outage(failures):
        remote('''import os, pathlib, signal, sys
pid = int(pathlib.Path(sys.argv[2]).read_text())
argv = pathlib.Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\\0')
assert os.fsencode(sys.argv[3]) in argv, 'Refusing to signal unrelated process'
pathlib.Path(sys.argv[1]).write_text(sys.argv[4])
os.kill(pid, signal.SIGUSR1)
''', gate, config.pid_file, wrapper, failures)
        # Wait for EOF before the next real write triggers reconnection.
        time.sleep(.5)

    def check_waits(start, expected):
        rows = attempts[start:]
        assert [r['status'] for r in rows] == ['failed'] * len(expected) + ['connected'], rows
        for previous, following, target in zip(rows, rows[1:], expected):
            measured = following['start'] - previous['end']
            check = dict(expected_seconds=target, measured_seconds=measured,
                         passed=target - .05 <= measured <= target + 3)
            checks.append(check)
            print(json.dumps(check), flush=True)
            assert check['passed'], check

    output = None
    report = dict(host=host, local_directory=str(run), remote_directory=remote_root,
                  source_sha256=hashes, attempts=attempts, checks=checks, passed=False)
    try:
        output = RestartingSingleSocketIO(SimpleNamespace(), key, config=config,
            launcher=ObservedLauncher(config), timing_log=log, notifications=False)
        output.write_data({'shot_index': np.array(0), 'test_values': np.arange(16)})
        for shot, expected in [(1, [15, 15, 30, 60]), (2, [15, 15])]:
            outage(len(expected))
            start = len(attempts)
            output.write_data({'shot_index': np.array(shot), 'test_values': np.arange(16) + shot})
            check_waits(start, expected)
        output.close()
        output = None
        # Verify the acknowledged frames are also present in the consumer log.
        rows = json.loads(remote('''import json, pathlib, sys, time
time.sleep(.5)
print(json.dumps([json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]))
''', config.timing_log))
        (run / 'consumer.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        shots = [row['shot'] for row in rows if row['event'] == 'io.write' and row['status'] == 'ok']
        assert shots == [0, 1, 2], shots
        report.update(passed=True, consumer_written_shots=shots)
    finally:
        if output is not None:
            output.close()
        # Retain all data; terminate only a surviving consumer from this test.
        remote('''import os, pathlib, signal, sys
p = pathlib.Path(sys.argv[1])
if p.exists():
    pid = int(p.read_text())
    cmd = pathlib.Path(f'/proc/{pid}/cmdline')
    if cmd.exists() and os.fsencode(sys.argv[2]) in cmd.read_bytes().split(b'\\0'):
        os.kill(pid, signal.SIGUSR1)
''', config.pid_file, wrapper)
        log.close()
        (run / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(f'Report: {run / "report.json"}', flush=True)


if __name__ == '__main__':
    main()
