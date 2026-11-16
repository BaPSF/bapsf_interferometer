#!/usr/bin/env python3
"""Check certificate monitoring and launch gating through real Ubuntu consumers.

Requires an existing SSH ControlMaster connection (--control-path). All commands
reuse it; ProxyCommand=false prevents opening a replacement connection. Temporary
certificates exercise monitoring, not authentication of the established connection.
Notifications go to a local HTTP recorder. No shared keys or topics are changed.
"""
import argparse
from collections import Counter
import configparser
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import nacl.public
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from streamer.raw_output_io import IO
from streamer.ssh_key_monitor import read_certificate_validity
from streamer.timing_log import TimingLog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control-path', required=True, type=Path)
    parser.add_argument('--host', default='ubuntupc')
    parser.add_argument('--config', type=Path,
                        default=ROOT / 'streamer/conf/ubuntupc_timed_control_test.conf')
    parser.add_argument('--port', type=int, default=8597)
    parser.add_argument('--run-directory', type=Path)
    args = parser.parse_args()
    if not 0 < args.port < 65536:
        parser.error('--port must be between 1 and 65535')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    run = (args.run_directory or Path('/tmp') / f'interf-key-monitor-{stamp}').resolve()
    run.mkdir(parents=True, exist_ok=False)
    os.chmod(run, 0o700)
    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(args.config):
        parser.error(f'Cannot read {args.config}')
    section = cfg['server']
    workdir, python = section['working_directory'], section['python']
    remote_root = f'{workdir}/output/key-monitor-{stamp}'
    source, wrapper = remote_root + '/source', remote_root + '/consumer_control.py'
    gate = remote_root + '/outage'
    # Per-output SSHControlConnection may add control options or request exit.
    # Strip those so every test launch uses the same externally owned master.
    shim = run / 'ssh-existing'
    shim.write_text(f'''#!{sys.executable}
import os, sys
args = sys.argv[1:]
if '-O' in args:
    if args[args.index('-O') + 1] == 'exit':
        sys.exit(0)
filtered = []
while args:
    value = args.pop(0)
    if value == '-o':
        option = args.pop(0)
        if not option.lower().startswith('control'):
            filtered.extend([value, option])
    elif value == '-S':
        args.pop(0)
    else:
        filtered.append(value)
os.execvp('ssh', ['ssh', '-S', {str(args.control_path.resolve())!r},
    '-o', 'ControlMaster=no', '-o', 'ProxyCommand=false', '-o', 'BatchMode=yes', *filtered])
''')
    shim.chmod(0o700)

    def remote(code, *arguments, data=None):
        command = 'cd ' + shlex.quote(workdir) + ' && ' + shlex.join(
            [python, '-c', code, *map(str, arguments)])
        result = subprocess.run([str(shim), args.host, command], input=data,
                                capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr.decode() + result.stdout.decode())
        return result.stdout.decode()

    subprocess.run(['ssh', '-S', str(args.control_path), '-O', 'check', args.host], check=True)
    connection = remote("import os; print(os.environ['SSH_CONNECTION'])").strip()
    key = nacl.public.PrivateKey.generate()
    (run / 'private_key').write_bytes(bytes(key))
    (run / 'private_key').chmod(0o600)
    wrapper_code = f'''import signal, sys, time
from pathlib import Path
sys.path.insert(0, {source!r})
with Path({(remote_root + '/launches')!r}).open('a') as stream:
    stream.write(str(time.time()) + '\\n')
if Path({gate!r}).exists():
    raise SystemExit('Controlled key-monitor outage')
def interrupt(*args):
    raise SystemExit(0)
signal.signal(signal.SIGUSR1, interrupt)
from streamer.consumer import main
main()
'''
    hashes = {}
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for path in sorted((ROOT / 'streamer').glob('*.py')):
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            tar.add(path, arcname='source/streamer/' + path.name)
        for name, contents in [('consumer_control.py', wrapper_code.encode()),
                               ('public_key', bytes(key.public_key))]:
            item = tarfile.TarInfo(name)
            item.size = len(contents)
            tar.addfile(item, io.BytesIO(contents))
    remote('''import pathlib, socket, sys, tarfile
s = socket.socket(); s.bind(('0.0.0.0', int(sys.argv[2]))); s.close()
root = pathlib.Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as tar:
    tar.extractall(root, filter='data')
''', remote_root, args.port, data=archive.getvalue())
    for name in ('ca', 'identity'):
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(run / name)], check=True)

    def sign(seconds, serial):
        subprocess.run(['ssh-keygen', '-q', '-s', str(run / 'ca'), '-I', 'monitor-test',
                        '-n', 'pnorbert', '-z', str(serial), '-V', f'-1m:{seconds:+d}s',
                        str(run / 'identity.pub')], check=True)
        validity = read_certificate_validity(run / 'identity-cert.pub')
        print(json.dumps(dict(phase='certificate signed', serial=serial,
                              expires_at=validity.valid_before)), flush=True)
        return validity

    notifications, phases = [], []
    lock = threading.Lock()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            row = dict(at=time.time(), title=self.headers.get('Title'),
                       message=self.rfile.read(int(self.headers['Content-Length'])).decode())
            with lock:
                notifications.append(row)
                with (run / 'notifications.jsonl').open('a') as stream:
                    stream.write(json.dumps(row) + '\n')
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for name in ('connection_file', 'stdout_log', 'pid_file', 'timing_log'):
        section.pop(name, None)
    section.update(host=args.host, ssh_command=str(shim), script=wrapper,
                   socket_transport='ssh', bind_host='127.0.0.1',
                   advertise_host='127.0.0.1', allow_ip_range='127.0.0.1/32',
                   output_directory=remote_root, public_key=remote_root + '/public_key',
                   local_fallback_directory=str(run / 'local'), port=f'{args.port}-{args.port}',
                   socket_timeout_seconds='2', startup_timeout_seconds='3', retry_delay_seconds='2',
                   ssh_key_certificate=str(run / 'identity-cert.pub'),
                   ssh_key_check_interval_seconds='2', ntfy_topic_info='test-info',
                   ntfy_topic_alert='test-alert', ntfy_token_env='INTERF_TEST_UNUSED_TOKEN',
                   ntfy_server_url=f'http://127.0.0.1:{server.server_port}')
    config_path = run / 'server.conf'
    with config_path.open('w') as stream:
        cfg.write(stream)
    log = TimingLog(run / 'producer.jsonl')
    output = None
    plain = None
    report = dict(passed=False, local_directory=str(run), remote_directory=remote_root,
                  source_sha256=hashes, phases=phases, ssh_connection=connection,
                  authentication='existing SSH master; certificate used for monitoring')
    warnings = [f'NERSC SSH key expires in {h} hour' + ('s' if h != 1 else '') for h in (6, 3, 2, 1)]

    def titles():
        with lock:
            return Counter(n['title'] for n in notifications)

    def events():
        return [json.loads(line) for line in (run / 'producer.jsonl').read_text().splitlines()]

    def wait(predicate, description, timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                row = dict(phase=description, at=time.time())
                phases.append(row)
                print(json.dumps(row), flush=True)
                return
            time.sleep(.1)
        raise TimeoutError(description)

    def frame(shot):
        output.write({'shot_index': np.array(shot), 'test_values': np.arange(16) + shot}, step=shot)

    def launch_count():
        return int(remote("from pathlib import Path; import sys; print(len(Path(sys.argv[1]).read_text().splitlines()))",
                          remote_root + '/launches'))

    def disconnect(config):
        remote('''import os, pathlib, signal, sys
pid = int(pathlib.Path(sys.argv[1]).read_text())
assert os.fsencode(sys.argv[2]) in pathlib.Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\\0')
os.kill(pid, signal.SIGUSR1)
''', config.pid_file, wrapper)
        time.sleep(.3)

    def assert_paused(baseline, description):
        time.sleep(5)  # More than two key-check and normal retry intervals.
        assert launch_count() == baseline, description
        phases.append(dict(phase=description, at=time.time(), launches=baseline))
        print(json.dumps(phases[-1]), flush=True)

    try:
        sign(120, 1)
        settings = SimpleNamespace(destination=str(config_path), private_key=str(run / 'private_key'),
                                   buffer_seconds=1, output_interval_seconds=.1)
        output = IO.create(settings, timing_log=log)
        process = output._key_monitor._process
        report['monitor_pid'] = process.pid
        frame(0)
        wait(lambda: output._remote_written == 1, 'initial remote frame')
        wait(lambda: all(titles()[t] == 1 for t in warnings), 'initial certificate warnings')
        remote('''import os, pathlib, signal, sys
pid = int(pathlib.Path(sys.argv[2]).read_text())
assert os.fsencode(sys.argv[3]) in pathlib.Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\\0')
pathlib.Path(sys.argv[1]).touch()
os.kill(pid, signal.SIGUSR1)
''', gate, output._config.pid_file, wrapper)
        time.sleep(.3)
        for shot in range(1, 11):
            frame(shot)
        wait(lambda: output._local_written >= 10, 'frames saved in local fallback')
        wait(lambda: sum(r['event'] == 'consumer.launch_retry' for r in events()) >= 4,
             'four failed consumer relaunches')
        assert process.poll() is None and output._key_monitor._process.pid == report['monitor_pid']
        assert all(titles()[t] == 1 for t in warnings), titles()
        remote('import pathlib, sys; pathlib.Path(sys.argv[1]).unlink()', gate)
        wait(lambda: output._remote is not None, 'consumer recovered')
        frame(11)
        wait(lambda: output._remote_written == 2, 'remote writing resumed')
        assert process.poll() is None
        replacement = sign(25, 2)
        wait(lambda: all(titles()[t] == 2 for t in warnings), 'replacement certificate warnings')
        wait(lambda: titles()['NERSC SSH key has expired'] == 1, 'real certificate expiry', timeout=35)
        time.sleep(4.2)  # Observe more than two monitor intervals after expiry.
        assert all(titles()[t] == 2 for t in warnings), titles()
        assert titles()['NERSC SSH key has expired'] == 1, titles()
        assert process.poll() is None
        assert output.dropped_steps == 0
        assert remote("import os; print(os.environ['SSH_CONNECTION'])").strip() == connection
        # Existing SSH authentication remains usable. Even so, expiry must
        # prevent any consumer launch while fallback continues writing locally.
        baseline = launch_count()
        disconnect(output._config)
        for shot in range(12, 22):
            frame(shot)
        wait(lambda: any(r['event'] == 'ssh_key.wait' for r in events()),
             'fallback recovery waiting for expired certificate')
        wait(lambda: output._local_written >= 20, 'fallback writes during certificate wait')
        assert_paused(baseline, 'zero fallback launches with expired certificate')
        sign(60, 3)
        wait(lambda: output._remote is not None, 'fallback recovery after certificate renewal')
        frame(22)
        wait(lambda: output._remote_written == 3, 'fallback remote writes after renewal')
        output.close(5)
        assert process.poll() is not None, 'Monitor survived output shutdown'
        # Repeat without local fallback, including expiry during recovery.
        section.pop('local_fallback_directory')
        plain_config = run / 'plain.conf'
        with plain_config.open('w') as stream:
            cfg.write(stream)
        settings.destination = str(plain_config)
        sign(-1, 4)
        baseline = launch_count()
        waits = sum(r['event'] == 'ssh_key.wait' for r in events())
        plain = IO.create(settings, timing_log=log)
        plain.write({'shot_index': np.array(100), 'test_values': np.arange(16)}, step=100)
        wait(lambda: sum(r['event'] == 'ssh_key.wait' for r in events()) > waits,
             'ordinary initial start waiting for expired certificate')
        assert_paused(baseline, 'zero ordinary initial launches with expired certificate')
        sign(60, 5)
        wait(lambda: plain.written_steps == 1, 'ordinary initial start after renewal')
        plain_remote = plain._output
        sign(-1, 6)
        baseline = launch_count()
        waits = sum(r['event'] == 'ssh_key.wait' for r in events())
        disconnect(plain_remote._config)
        plain.write({'shot_index': np.array(101), 'test_values': np.arange(16)}, step=101)
        wait(lambda: sum(r['event'] == 'ssh_key.wait' for r in events()) > waits,
             'ordinary reconnect waiting for expired certificate')
        assert_paused(baseline, 'zero ordinary reconnect launches with expired certificate')
        sign(60, 7)
        wait(lambda: plain.written_steps == 2, 'ordinary reconnect writes after renewal')
        sign(-1, 8)
        waits = sum(r['event'] == 'ssh_key.wait' for r in events())
        disconnect(plain_remote._config)
        plain.write({'shot_index': np.array(102), 'test_values': np.arange(16)}, step=102)
        wait(lambda: sum(r['event'] == 'ssh_key.wait' for r in events()) > waits,
             'ordinary reconnect waiting before shutdown')
        plain.close(.1)
        wait(lambda: not plain._worker.is_alive(), 'ordinary shutdown interrupts certificate wait')
        assert remote("import os; print(os.environ['SSH_CONNECTION'])").strip() == connection
        report.update(passed=True, notification_counts=dict(titles()),
                      failed_launches=sum(r['event'] == 'consumer.launch_retry' for r in events()),
                      local_written=output._local_written, remote_written=output._remote_written,
                      dropped=output.dropped_steps, replacement_expires_at=replacement.valid_before,
                      monitor_stopped=True, expired_launch_gating=True,
                      ordinary_written=plain.written_steps)
    finally:
        if plain is not None:
            plain.close(.1)
        if output is not None:
            output.close(1)
            remote('''import os, pathlib, signal, sys
p = pathlib.Path(sys.argv[1])
if p.exists():
    pid = int(p.read_text()); cmd = pathlib.Path(f'/proc/{pid}/cmdline')
    if cmd.exists() and os.fsencode(sys.argv[2]) in cmd.read_bytes().split(b'\\0'):
        os.kill(pid, signal.SIGUSR1)
''', output._config.pid_file, wrapper)
        server.shutdown()
        server.server_close()
        log.close()
        (run / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(f'Report: {run / "report.json"}', flush=True)


if __name__ == '__main__':
    main()
