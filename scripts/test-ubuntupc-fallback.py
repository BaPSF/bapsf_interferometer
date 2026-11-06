#!/usr/bin/env python3
"""Exercise real SSH/TCP streaming, two outages, and local BP5 recovery.

Run from the repository root with its Python environment. All outputs are retained
in a unique local run directory and a separate remote output directory. Only this
test's consumer receives the controlled outage signal. Use --notifications to
retain the configured ntfy topics and verify notification publication.
"""
import argparse
import configparser
from collections import Counter
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from interf_sim.synthetic import make_raw_shot
from streamer import RawOutput
from streamer.arguments import RawOutputSettings
from streamer.payload import shot_variables

# Also executed on the consumer host, where the repository provides iter_steps.
VERIFY_CODE = '''
import hashlib, json, sys
from pathlib import Path
from interf_raw_reader import iter_steps

def digest(data):
    h = hashlib.sha256()
    for name, value in sorted(data.items()):
        h.update(name.encode())
        h.update(value.dtype.str.encode())
        h.update(str(value.shape).encode())
        h.update(value.tobytes())
    return h.hexdigest()

rows, files = [], []
for path in sorted(Path(sys.argv[1]).glob("*/*.bp")):
    count = 0
    for data in iter_steps(path):
        rows.append([int(data["shot_index"]), digest(data), str(path)])
        count += 1
    files.append({"path": str(path), "frames": count})
print(json.dumps({"rows": rows, "files": files}))
'''


def digest(data):
    h = hashlib.sha256()
    for name, value in sorted(data.items()):
        h.update(name.encode())
        h.update(value.dtype.str.encode())
        h.update(str(value.shape).encode())
        h.update(value.tobytes())
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'streamer/conf/ubuntupc.conf')
    parser.add_argument('--run-directory', type=Path)
    parser.add_argument('--cycles', type=int, default=2)
    parser.add_argument('--port', type=int, default=8501,
                        help='dedicated consumer port (must be unused)')
    parser.add_argument('--notifications', action='store_true',
                        help='publish to the configured ntfy topics and check delivery logs')
    parser.add_argument('--disk-pressure', action='store_true',
                        help='temporarily raise this test instance disk limit, fill the buffer and verify intentional drops')
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error('--cycles must be positive')
    if not 0 < args.port < 65536:
        parser.error('--port must be between 1 and 65535')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    run = (args.run_directory or Path('/tmp') / f'interf-fallback-test-{stamp}').resolve()
    run.mkdir(parents=True, exist_ok=False)
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(args.config)
    section = cfg['server']
    if args.notifications and not section.get('ntfy_topic_info', '').strip():
        parser.error('--notifications requires ntfy_topic_info in the server configuration')
    host, workdir, python = (section[k] for k in ('host', 'working_directory', 'python'))
    ssh = shlex.split(section.get('ssh_command', 'ssh'))
    remote_root = f'{workdir}/output/fallback-test-{stamp}'
    remote_data = remote_root + '/data'
    wrapper = remote_root + '/consumer_control.py'
    gate = remote_root + '/outage'

    def remote(code, *arguments, timeout=30):
        command = f'cd {shlex.quote(workdir)} && ' + shlex.join([python, '-c', code, *map(str, arguments)])
        result = subprocess.run([*ssh, host, command], capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f'SSH failed ({result.returncode}): {result.stderr}\n{result.stdout}')
        return result.stdout

    wrapper_code = f'''import signal, sys
from pathlib import Path
sys.path.insert(0, {workdir!r})
if Path({gate!r}).exists():
    raise SystemExit("Controlled streaming outage")
def interrupt(*args):
    raise SystemExit(0)
signal.signal(signal.SIGUSR1, interrupt)
from streamer.consumer import main
main()
'''
    remote('''import pathlib, socket, sys
root = pathlib.Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
(root / 'consumer_control.py').write_text(sys.argv[2])
s = socket.socket(); s.bind(('0.0.0.0', int(sys.argv[3]))); s.close()
''', remote_root, wrapper_code, str(args.port))
    # Use only the dedicated test port, after checking it is unused.
    port = str(args.port)
    for key in ('connection_file', 'stdout_log', 'pid_file', 'timing_log'):
        section.pop(key, None)
    section.update(script=wrapper, output_directory=remote_root,
                   local_fallback_directory=str(run / 'local'), port=f'{port}-{port}',
                   socket_timeout_seconds='3', startup_timeout_seconds='5',
                   retry_delay_seconds='.3', file_interval_seconds='2')
    if not args.notifications:
        section.update(ntfy_topic_info='', ntfy_topic_alert='')
    config_path = run / 'server.conf'
    with config_path.open('w') as stream:
        cfg.write(stream)
    timing_path = run / 'producer.jsonl'
    output = RawOutput(RawOutputSettings(destination=str(config_path), timing_log=str(timing_path),
                       private_key=str(ROOT / 'keys/testkey'), buffer_seconds=2,
                       output_interval_seconds=.05))
    io = output._output
    expected, acquisition_errors, phases = {}, [], []
    stop = threading.Event()
    acquisition_enabled = threading.Event()
    acquisition_enabled.set()
    acquisition_lock = threading.Lock()
    original_disk_limit = io.MIN_FREE_BYTES
    pressure_checks = []

    def produce_one():
        index = len(expected)
        shot = make_raw_shot(n=4096, host_time=1_800_000_000 + index * .05, shot_number=index)
        expected[index] = digest(shot_variables(shot, index))
        output.write(shot)

    def acquire():
        next_frame = time.monotonic()
        try:
            while not stop.is_set():
                if not acquisition_enabled.wait(.1):
                    continue
                with acquisition_lock:
                    if stop.is_set():
                        return
                    if not acquisition_enabled.is_set():
                        continue
                    produce_one()
                next_frame = max(next_frame, time.monotonic()) + .05
                stop.wait(max(0, next_frame - time.monotonic()))
        except Exception as exc:
            acquisition_errors.append(repr(exc))
            stop.set()

    def pause_acquisition():
        acquisition_enabled.clear()
        with acquisition_lock:
            pass  # Wait for a frame already being generated to finish.

    thread = threading.Thread(target=acquire, name='test-acquisition')
    thread.start()

    def wait(predicate, description, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if acquisition_errors:
                raise RuntimeError(str(acquisition_errors))
            if predicate():
                row = dict(phase=description, generated=len(expected), written=io.written_steps,
                           local_written=io._local_written, remote_written=io._remote_written,
                           dropped=io.dropped_steps, at=datetime.now(timezone.utc).isoformat())
                phases.append(row)
                print(json.dumps(row), flush=True)
                return
            time.sleep(.025)
        raise TimeoutError(description + ': ' + str(io.error))

    def outage():
        return remote('''import os, pathlib, signal, sys
pathlib.Path(sys.argv[1]).touch()
pid = int(pathlib.Path(sys.argv[2]).read_text())
argv = pathlib.Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\\0')
assert os.fsencode(sys.argv[3]) in argv, 'Refusing to signal unrelated process'
os.kill(pid, signal.SIGUSR1)
print(pid)
''', gate, io._config.pid_file, wrapper)

    def pressure_snapshot(phase):
        with io._condition:
            ids = ([io._pending[1]] if io._pending is not None else []) + [p[1] for p in io._buffer]
            row = dict(phase=phase, occupancy=len(ids), capacity=io.capacity,
                       buffered_ids=ids, dropped=io.dropped_steps, local_written=io._local_written,
                       minimum_free_bytes=io.MIN_FREE_BYTES)
        pressure_checks.append(row)
        io._record('test.disk_pressure', **row)
        print(json.dumps(row), flush=True)
        return row

    def disk_pressure(cycle):
        pause_acquisition()
        wait(lambda: io._pending is None and not io._buffer, f'cycle {cycle}: local queue drained')
        dropped_before = io.dropped_steps
        # Instance-only override: exercise the real disk checker without filling the disk.
        io.MIN_FREE_BYTES = shutil.disk_usage(run / 'local').total + 1
        io._record('test.disk_limit', minimum_free_bytes=io.MIN_FREE_BYTES, original_bytes=original_disk_limit)
        with acquisition_lock:
            produce_one()
        wait(lambda: io._transition_state == 'paused' and io._local is None,
             f'cycle {cycle}: local saving paused')
        paused = pressure_snapshot('buffering below capacity')
        assert paused['occupancy'] < io.capacity and paused['dropped'] == dropped_before
        with acquisition_lock:
            for _ in range(io.capacity - paused['occupancy']):
                produce_one()
        full = pressure_snapshot('buffer full before overflow')
        assert full['occupancy'] == io.capacity and full['dropped'] == dropped_before
        with acquisition_lock:
            for _ in range(12):
                produce_one()
        overflow = pressure_snapshot('buffer full after 12 additional frames')
        assert overflow['occupancy'] == io.capacity and overflow['dropped'] == dropped_before + 12
        before_resume = io._local_written
        io.MIN_FREE_BYTES = original_disk_limit
        io._record('test.disk_limit', minimum_free_bytes=io.MIN_FREE_BYTES, restored=True)
        wait(lambda: io._transition_state == 'local' and io._local_written == before_resume + io.capacity,
             f'cycle {cycle}: local saving resumed and retained buffer drained')
        resumed = pressure_snapshot('disk limit restored')
        assert resumed['occupancy'] == 0 and resumed['dropped'] == dropped_before + 12
        acquisition_enabled.set()
        wait(lambda: io._local_written >= before_resume + io.capacity + 20,
             f'cycle {cycle}: acquisition continues with local saving')

    scenario_errors = []
    try:
        wait(lambda: io._remote_written >= 20, 'initial streaming')
        for cycle in range(args.cycles):
            local_before = io._local_written
            print(f'Cycle {cycle+1}: interrupting test consumer PID {outage().strip()}', flush=True)
            wait(lambda: io._transition_state == 'local' and io._local_written >= local_before + 30,
                 f'cycle {cycle+1}: saving locally')
            if args.disk_pressure:
                disk_pressure(cycle + 1)
            remote_before = io._remote_written
            remote('import pathlib, sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)', gate)
            wait(lambda: io._transition_state == 'remote' and io._remote_written >= remote_before + 20,
                 f'cycle {cycle+1}: streaming resumed')
    except Exception as exc:
        scenario_errors.append(repr(exc))
    finally:
        io.MIN_FREE_BYTES = original_disk_limit
        io._record("test.disk_limit", minimum_free_bytes=io.MIN_FREE_BYTES, restored=True)
        stop.set()
        acquisition_enabled.set()
        thread.join(5)
        output.close(10)
        remote('''import os, pathlib, signal, sys
pathlib.Path(sys.argv[1]).unlink(missing_ok=True)
pid = int(pathlib.Path(sys.argv[2]).read_text())
cmdline = pathlib.Path(f'/proc/{pid}/cmdline')
if cmdline.exists() and os.fsencode(sys.argv[3]) in cmdline.read_bytes().split(b'\\0'):
    os.kill(pid, signal.SIGUSR1)
''', gate, io._config.pid_file, wrapper)

    # Wait for the consumer to close BP metadata after the producer disconnects.
    remote('''import pathlib, sys, time
pidfile = pathlib.Path(sys.argv[1])
pid = int(pidfile.read_text())
for _ in range(100):
    cmdline = pathlib.Path(f'/proc/{pid}/cmdline')
    if not cmdline.exists() or not cmdline.read_bytes():
        break
    time.sleep(.1)
else:
    raise RuntimeError('Test consumer did not exit')
''', io._config.pid_file)
    remote_result = json.loads(remote(VERIFY_CODE, remote_data))
    local_result = subprocess.run([sys.executable, '-c', VERIFY_CODE, str(run / 'local')],
                                  cwd=ROOT, capture_output=True, text=True, timeout=30, check=True)
    local_result = json.loads(local_result.stdout)
    (run / 'expected.json').write_text(json.dumps(expected, indent=2))
    (run / 'remote-files.json').write_text(json.dumps(remote_result, indent=2))
    (run / 'local-files.json').write_text(json.dumps(local_result, indent=2))
    observed, corrupt = {}, []
    for row in remote_result['rows'] + local_result['rows']:
        index, checksum, path = row
        observed.setdefault(index, []).append(path)
        if expected.get(index) != checksum:
            corrupt.append(row)
    records = [json.loads(line) for line in timing_path.read_text().splitlines()]
    transitions = [r for r in records if r['event'] == 'fallback.transition']
    missing, unexpected = sorted(expected.keys() - observed.keys()), sorted(observed.keys() - expected.keys())
    notifications = [r for r in records if r['event'].startswith('notification.')]
    sent_titles = Counter(r['title'] for r in notifications if r['event'] == 'notification.sent')
    expected_titles = Counter(r['message'] for r in transitions)
    notification_errors = [r for r in notifications if r['event'] == 'notification.failed']
    if args.disk_pressure:
        for title in ('Local saving paused; buffering in memory', 'Local saving resumed'):
            if expected_titles[title] != args.cycles:
                scenario_errors.append('Missing disk transition: ' + title)
    drops = [r for r in records if r['event'] == 'io.buffer_drop']
    dropped_ids = sorted(r['dropped_step'] for r in drops)
    retained_ids = [i for check in pressure_checks if check['phase'] == 'buffer full after 12 additional frames'
                    for i in check['buffered_ids']]
    retained_missing = sorted(set(retained_ids) - observed.keys())
    retained_written = [r['step'] for r in records if r['event'] == 'fallback.write' and r['step'] in set(retained_ids)]
    retained_fifo = retained_written == retained_ids
    losses_match = (missing == dropped_ids and len(dropped_ids) == output.dropped_shots
                    and retained_fifo and not retained_missing and (args.disk_pressure or not dropped_ids))
    if args.disk_pressure and output.dropped_shots != 12 * args.cycles:
        losses_match = False
    notifications_passed = not args.notifications or (
        not notification_errors and all(sent_titles[title] == count for title, count in expected_titles.items())
        and expected_titles['Saving raw data locally'] == args.cycles
        and expected_titles['Remote streaming resumed'] == args.cycles
    )
    report = dict(passed=notifications_passed and losses_match and not (unexpected or corrupt or acquisition_errors or scenario_errors or output.unsent_shots),
                  run_directory=str(run), remote_directory=remote_root, source_config=str(args.config),
                  disk_pressure_enabled=args.disk_pressure, pressure_checks=pressure_checks,
                  original_disk_limit=original_disk_limit, restored_disk_limit=io.MIN_FREE_BYTES,
                  dropped_ids=dropped_ids, losses_match=losses_match, retained_missing=retained_missing,
                  retained_fifo=retained_fifo,
                  notifications_enabled=args.notifications, notifications_passed=notifications_passed,
                  notification_sent_titles=dict(sent_titles), notification_errors=notification_errors,
                  notifications=notifications, cycles=args.cycles, generated=len(expected), unique_stored=len(observed),
                  remote_frames=len(remote_result['rows']), local_frames=len(local_result['rows']),
                  duplicate_copies=sum(len(paths)-1 for paths in observed.values()),
                  dropped=output.dropped_shots, unsent=output.unsent_shots,
                  written=output.written_shots, missing=missing, unexpected=unexpected,
                  corrupt=corrupt, acquisition_errors=acquisition_errors, scenario_errors=scenario_errors, phases=phases, transitions=transitions)
    (run / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
