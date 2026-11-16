#!/usr/bin/env python3
"""Verify recorded and legacy buffer reporting through real UbuntuPC streaming.

Retains logs, reports and an isolated remote source snapshot. Does not change the
remote checkout or use its existing consumers. Notifications are disabled.
"""
import argparse
import configparser
import io
import json
import shlex
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

import nacl.public

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from interf_sim.synthetic import make_raw_shot
from streamer import RawOutput
from streamer.arguments import settings_from_env
from streamer.analyze_remote_experiment import main as analyze, remote_snapshot, bp_shot_indexes
from streamer.remote_consumer_io import ServerConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'streamer/conf/ubuntupc.conf')
    parser.add_argument('--port', type=int, default=8597)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    run = Path('/tmp') / f'interf-buffer-report-test-{stamp}'
    run.mkdir()
    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(args.config):
        parser.error(f'Cannot read {args.config}')
    server = cfg['server']
    workdir, python, host = (server[name] for name in ('working_directory', 'python', 'host'))
    remote_root = f'{workdir}/output/buffer-report-test-{stamp}'
    ssh = shlex.split(server.get('ssh_command', 'ssh'))
    key = nacl.public.PrivateKey.generate()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for path in sorted((ROOT / 'streamer').glob('*.py')):
            tar.add(path, arcname='source/streamer/' + path.name)
        entry = tarfile.TarInfo('public_key')
        entry.size = len(bytes(key.public_key))
        tar.addfile(entry, io.BytesIO(bytes(key.public_key)))
    setup = '''import pathlib, socket, sys, tarfile
s = socket.socket(); s.bind(('0.0.0.0', int(sys.argv[2]))); s.close()
root = pathlib.Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as tar:
    tar.extractall(root, filter='data')
'''
    command = shlex.join([python, '-c', setup, remote_root, str(args.port)])
    subprocess.run([*ssh, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host, command],
                   input=archive.getvalue(), check=True, timeout=30)
    for name in ('connection_file', 'stdout_log', 'pid_file', 'timing_log',
                 'local_fallback_directory', 'compression_config', 'ssh_key_certificate'):
        server.pop(name, None)
    server.update(working_directory=remote_root + '/source', script='streamer/consumer.py',
                  output_directory=remote_root + '/output', public_key=remote_root + '/public_key',
                  socket_transport='ssh', bind_host='127.0.0.1', advertise_host='127.0.0.1',
                  allow_ip_range='127.0.0.1/32', port=f'{args.port}-{args.port}',
                  ntfy_topic_info='', ntfy_topic_alert='', socket_timeout_seconds='5',
                  startup_timeout_seconds='10', max_relaunch_attempts='1')
    config_path = run / 'server.conf'
    with config_path.open('w') as stream:
        cfg.write(stream)
    private_key = run / 'private_key'
    private_key.write_bytes(bytes(key))
    private_key.chmod(0o600)
    producer = run / 'producer.jsonl'
    # Match interf_main: 60 s fallback, overridden by the environment if set.
    settings = settings_from_env('INTERF_RAW_OUTPUT', producer, buffer_seconds=60.0,
                                 environ={'INTERF_RAW_OUTPUT': str(config_path),
                                          'INTERF_RAW_OUTPUT_PRIVATE_KEY': str(private_key)})
    output = RawOutput(settings)
    try:
        for index in range(12):
            output.write(make_raw_shot(n=4096, host_time=1_800_000_000 + index * .1,
                                       shot_number=index))
            time.sleep(.1)
    finally:
        output.close(timeout=30)
    assert (output.written_shots, output.dropped_shots, output.unsent_shots) == (12, 0, 0)
    records = [json.loads(line) for line in producer.read_text().splitlines()]
    buffer = next(row for row in records if row['event'] == 'io.buffer_config')
    assert (buffer['buffer_seconds'], buffer['output_interval_seconds'],
            buffer['buffer_capacity_steps']) == (60.0, 3.0, 20), buffer
    legacy = run / 'legacy-producer.jsonl'
    legacy.write_text(''.join(json.dumps(row) + '\n' for row in records
                              if row['event'] != 'io.buffer_config'))
    checks = []
    for name, log, extra, expected in (
        ('recorded', producer, [], 'Ring-buffer capacity: **20 shots** (60 seconds)'),
        ('legacy', legacy, [], 'Ring-buffer capacity: **unknown**'),
        ('override', legacy, ['--buffer-seconds', '60', '--interval-seconds', '3'],
         'Ring-buffer capacity: **20 shots** (60 seconds)'),
    ):
        report = run / f'{name}.md'
        assert analyze([str(config_path), str(report), '--producer-log', str(log),
                        '--skip-compression', *extra]) == 0
        text = report.read_text()
        assert expected in text, text
        assert '600 seconds' not in text
        if name == 'legacy':
            assert '% of capacity' not in text
        checks.append(dict(case=name, passed=True, report=str(report)))
        print(json.dumps(checks[-1]), flush=True)
    _, remote = remote_snapshot(ServerConfig(config_path), None, skip_compression=True)
    assert remote['payload']['sampled_steps'] == 12, remote
    assert bp_shot_indexes(remote) == list(range(12)), remote
    (run / 'remote-snapshot.json').write_text(json.dumps(remote, indent=2) + '\n')
    result = dict(passed=True, local_directory=str(run), remote_directory=remote_root,
                  written=12, dropped=0, unsent=0, buffer=buffer, checks=checks)
    (run / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
