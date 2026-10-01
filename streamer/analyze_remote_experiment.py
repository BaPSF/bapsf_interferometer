#!/usr/bin/env python3
"""Analyze an active remote raw-interferometer run and write Markdown."""

import argparse
import base64
import json
import math
import os
import re
import shlex
import statistics
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "streamer"

from .data_operations import load_data_operation
from .remote_consumer_io import SSHControlConnection, ServerConfig


ROOT = Path(__file__).resolve().parent.parent


REMOTE_SNAPSHOT_PROGRAM = r'''
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

output_directory = Path(sys.argv[1])
pid_file = Path(sys.argv[2])
stdout_log = Path(sys.argv[3])
operation_description = json.loads(sys.argv[4]) if sys.argv[4] else None

result = {
    "captured_at": __import__("datetime").datetime.now().astimezone().isoformat(),
    "hostname": socket.gethostname(),
}

try:
    completed = subprocess.run(
        ["du", "-sk", "--", str(output_directory)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    result["disk_kib"] = int(completed.stdout.split()[0])
except Exception as exc:
    result["disk_error"] = f"{type(exc).__name__}: {exc}"

try:
    entries = sorted(
        path for path in output_directory.iterdir()
        if path.suffix.lower() in (".bp", ".h5", ".pkl")
    )
    result["segments"] = [path.name for path in entries]
except Exception as exc:
    entries = []
    result["segments_error"] = f"{type(exc).__name__}: {exc}"

try:
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    result["consumer_pid"] = pid
    completed = subprocess.run(
        [
            "ps", "-p", str(pid), "-o",
            "pid=,etime=,%cpu=,%mem=,rss=,stat=,args=",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    result["consumer_process"] = completed.stdout.strip() or None
except Exception as exc:
    result["consumer_process_error"] = f"{type(exc).__name__}: {exc}"

try:
    lines = stdout_log.read_text(encoding="utf-8", errors="replace").splitlines()
    result["consumer_stdout_last"] = lines[-1] if lines else None
except Exception as exc:
    result["consumer_stdout_error"] = f"{type(exc).__name__}: {exc}"

if entries:
    try:
        import adios2
        from streamer.data_operations import data_operation_from_description

        operation = (
            data_operation_from_description(operation_description)
            if operation_description else None
        )
        samples = []
        total_raw = 0
        total_wire = 0
        total_steps = 0
        for path in entries:
            if path.suffix.lower() != ".bp":
                continue
            reader = adios2.FileReader(str(path))
            try:
                variables = reader.available_variables()
                arrays = [
                    name for name, description in variables.items()
                    if description.get("SingleValue", "false").lower() == "false"
                ]
                if not arrays:
                    continue
                steps = min(
                    int(variables[name]["AvailableStepsCount"]) for name in arrays
                )
                if steps <= 0:
                    continue
                sample_step = steps // 2
                raw_bytes = 0
                wire_bytes = 0
                for name in variables:
                    value = reader.read(name, step_selection=[sample_step, 1])
                    raw_bytes += value.nbytes
                    if operation is not None:
                        encoded = operation.encode(value)
                        wire_bytes += len(encoded.payload)
                shot = None
                if "shot_index" in variables:
                    value = reader.read(
                        "shot_index", step_selection=[sample_step, 1]
                    )
                    shot = int(value.reshape(-1)[0])
                samples.append(
                    {
                        "segment": path.name,
                        "steps": steps,
                        "sample_shot": shot,
                        "raw_bytes": raw_bytes,
                        "wire_bytes": wire_bytes if operation else None,
                        "ratio": raw_bytes / wire_bytes if wire_bytes else None,
                    }
                )
                total_raw += raw_bytes * steps
                total_wire += wire_bytes * steps
                total_steps += steps
            finally:
                reader.close()
        result["payload"] = {
            "samples": samples,
            "sampled_steps": total_steps,
            "estimated_raw_bytes": total_raw,
        }
        if operation is not None:
            result["compression"] = {
                "samples": samples,
                "sampled_steps": total_steps,
                "estimated_raw_bytes": total_raw,
                "estimated_wire_bytes": total_wire,
                "ratio": total_raw / total_wire if total_wire else None,
            }
    except Exception as exc:
        result["payload_error"] = f"{type(exc).__name__}: {exc}"

print(json.dumps(result, separators=(",", ":")))
'''


def parse_time(value):
    return datetime.fromisoformat(value)


def completion_time(record):
    return parse_time(record["called_at"]) + timedelta(
        seconds=record["duration_seconds"]
    )


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return math.nan
    return ordered[round((len(ordered) - 1) * fraction)]


def distribution(values):
    values = list(values)
    if not values:
        return None
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values),
    }


def read_json_lines_bytes(contents, description):
    records = []
    lines = contents.splitlines(keepends=True)
    for index, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            records.append(json.loads(raw_line))
        except json.JSONDecodeError as exc:
            is_incomplete_tail = index == len(lines) and not raw_line.endswith(b"\n")
            if is_incomplete_tail:
                break
            raise ValueError(
                f"Invalid JSON in {description} at line {index}: {exc}"
            ) from exc
    return records


def read_json_lines(path):
    try:
        return read_json_lines_bytes(path.read_bytes(), str(path))
    except OSError as exc:
        raise ValueError(f"Unable to read producer timing log {path}: {exc}") from exc


def replace_consumer_name(value, replacement):
    name = Path(value).name
    if ".consumer" not in name:
        raise ValueError(
            f"Cannot derive the producer {replacement} from {name!r}; "
            f"pass --producer-{replacement} explicitly"
        )
    return name.replace(".consumer", ".producer", 1)


def derive_local_paths(config, producer_log=None, producer_stdout=None):
    log_directory = ROOT / "data" / "logs"
    if producer_log is None:
        if not config.timing_log:
            raise ValueError(
                "The server configuration has no timing_log; "
                "pass --producer-log explicitly"
            )
        producer_log = log_directory / replace_consumer_name(
            config.timing_log, "log"
        )
    if producer_stdout is None:
        producer_stdout = log_directory / replace_consumer_name(
            config.stdout_log, "stdout"
        )
    return Path(producer_log), Path(producer_stdout)


def parse_producer_stdout(path, require_mock_header=True):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"Unable to read producer stdout log {path}: {exc}") from exc

    patterns = {
        "shape": r"^Array size\s*:\s*(\d+)\s*x\s*(\d+)\s*$",
        "total_steps": r"^Number of output steps\s*:\s*(\d+)\s*$",
        "interval_seconds": r"^Output interval\s*:\s*([0-9.eE+-]+)\s+seconds\s*$",
        "buffer_seconds": r"^Output buffer\s*:\s*([0-9.eE+-]+)\s+seconds\s*$",
        "engine": r"^Using engine\s*:\s*(\S+)\s*$",
    }
    result = {}
    for line in text.splitlines():
        for name, pattern in patterns.items():
            if name in result:
                continue
            match = re.match(pattern, line)
            if not match:
                continue
            if name == "shape":
                result["nx"], result["ny"] = map(int, match.groups())
            elif name == "total_steps":
                result[name] = int(match.group(1))
            elif name in ("interval_seconds", "buffer_seconds"):
                result[name] = float(match.group(1))
            else:
                result[name] = match.group(1)
    required = ("nx", "ny", "total_steps", "interval_seconds", "buffer_seconds")
    missing = [name for name in required if name not in result]
    if require_mock_header and missing:
        raise ValueError(
            f"Producer stdout log {path} is missing: {', '.join(missing)}"
        )
    return result


def experiment_settings(
    producer,
    stdout_settings=None,
    *,
    total_shots=None,
    interval_seconds=None,
    buffer_seconds=None,
):
    """Combine optional CLI/header settings with values observable in a run."""
    settings = dict(stdout_settings or {})
    successful = [
        record for record in producer
        if record.get("event") in ("raw_output.write", "io.write")
        and record.get("status") == "ok"
    ]
    if interval_seconds is None and "interval_seconds" not in settings:
        intervals = [
            (
                parse_time(successful[index]["called_at"])
                - parse_time(successful[index - 1]["called_at"])
            ).total_seconds()
            for index in range(1, len(successful))
        ]
        if not intervals:
            raise ValueError(
                "At least two producer writes or --interval-seconds are required"
            )
        settings["interval_seconds"] = statistics.median(intervals)
    elif interval_seconds is not None:
        settings["interval_seconds"] = interval_seconds

    if total_shots is not None:
        settings["total_steps"] = total_shots
    else:
        settings.setdefault("total_steps", None)
    settings["buffer_seconds"] = (
        buffer_seconds
        if buffer_seconds is not None
        else settings.get("buffer_seconds", 600.0)
    )
    return settings


def run_ssh(connection, config, remote_command, *, binary=False, timeout=120):
    completed = subprocess.run(
        [*connection.ssh_command, config.host, remote_command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise OSError(
            f"SSH command on {config.host} failed with status "
            f"{completed.returncode}: {stderr.strip()}"
        )
    return completed.stdout


class BorrowedSSHControlConnection:
    """Use an existing SSH master without taking ownership of its lifetime."""

    def __init__(self, config, control_path):
        executable, *arguments = config.ssh_command
        self.ssh_command = [
            executable,
            "-S",
            str(control_path),
            "-o",
            "ControlMaster=no",
            *arguments,
        ]

    def close(self):
        pass


def remote_snapshot(
    config,
    operation_description,
    skip_compression=False,
    control_path=None,
):
    if not config.timing_log:
        raise ValueError("The server configuration must define timing_log")

    connection = (
        BorrowedSSHControlConnection(config, control_path)
        if control_path else SSHControlConnection(config)
    )
    try:
        quote = shlex.quote
        workdir = quote(config.working_directory)
        timing_log = quote(config.timing_log)
        consumer_bytes = run_ssh(
            connection,
            config,
            f"cd -- {workdir} && cat -- {timing_log}",
            binary=True,
        )

        encoded_program = base64.b64encode(
            REMOTE_SNAPSHOT_PROGRAM.encode("utf-8")
        ).decode("ascii")
        bootstrap = (
            "import base64;exec(base64.b64decode(" + repr(encoded_program) + "))"
        )
        description = ""
        if operation_description and not skip_compression:
            description = json.dumps(operation_description, separators=(",", ":"))
        arguments = (
            config.output_directory,
            config.pid_file,
            config.stdout_log,
            description,
        )
        command = " ".join(
            [
                "cd", "--", workdir, "&&", quote(config.python), "-c",
                quote(bootstrap),
                *(quote(argument) for argument in arguments),
            ]
        )
        metadata_text = run_ssh(
            connection, config, command, timeout=300
        )
        try:
            metadata = json.loads(metadata_text)
        except json.JSONDecodeError as exc:
            raise OSError(
                f"Remote analysis on {config.host} returned invalid JSON"
            ) from exc
        return consumer_bytes, metadata
    finally:
        connection.close()


def is_contiguous(records):
    if not records:
        return False
    steps = [record["step"] for record in records]
    return (
        len(steps) == len(set(steps))
        and max(steps) - min(steps) + 1 == len(steps)
    )


def latency_clusters(latency_by_step, threshold=3.0):
    slow_steps = sorted(
        step for step, latency in latency_by_step.items() if latency > threshold
    )
    groups = []
    for step in slow_steps:
        if not groups or step != groups[-1][-1] + 1:
            groups.append([step])
        else:
            groups[-1].append(step)
    return groups


def summarize(producer, consumer, settings, bytes_per_step=None):
    producer_writes = []
    for record in producer:
        if (
            record.get("event") in ("raw_output.write", "io.write")
            and record.get("status") == "ok"
        ):
            normalized = dict(record)
            if "step" not in normalized and "shot" in normalized:
                normalized["step"] = normalized["shot"]
            producer_writes.append(normalized)
    consumer_writes = []
    for record in consumer:
        if record.get("event") == "io.write" and record.get("status") == "ok":
            normalized = dict(record)
            if "shot" in normalized:
                normalized["step"] = normalized["shot"]
            consumer_writes.append(normalized)
    receives = [
        record for record in consumer
        if record.get("event") == "socket.receive_all"
    ]
    if not producer_writes or not consumer_writes:
        raise ValueError(
            "Both producer and consumer logs need successful output-write events"
        )

    producer_by_step = {record["step"]: record for record in producer_writes}
    consumer_by_step = {record["step"]: record for record in consumer_writes}
    matched = sorted(producer_by_step.keys() & consumer_by_step.keys())
    if not matched:
        raise ValueError("Producer and consumer logs have no matching shot indexes")
    latency_by_step = {
        step: (
            completion_time(consumer_by_step[step])
            - parse_time(producer_by_step[step]["called_at"])
        ).total_seconds()
        for step in matched
    }

    cadence = [
        (
            parse_time(producer_writes[index]["called_at"])
            - parse_time(producer_writes[index - 1]["called_at"])
        ).total_seconds()
        for index in range(1, len(producer_writes))
    ]
    first_step = producer_writes[0]["step"]
    first_time = parse_time(producer_writes[0]["called_at"])
    drift = [
        (
            parse_time(record["called_at"]) - first_time
        ).total_seconds()
        - (record["step"] - first_step) * settings["interval_seconds"]
        for record in producer_writes
    ]

    timeline = [
        (parse_time(record["called_at"]), 1) for record in producer_writes
    ] + [
        (completion_time(record), -1) for record in consumer_writes
    ]
    outstanding = 0
    maximum_outstanding = 0
    maximum_at = None
    for timestamp, change in sorted(timeline):
        outstanding += change
        if outstanding > maximum_outstanding:
            maximum_outstanding = outstanding
            maximum_at = timestamp

    if bytes_per_step is None:
        if "nx" in settings and "ny" in settings:
            bytes_per_step = 6 * settings["nx"] * settings["ny"] * 8 + 8
        else:
            bytes_per_step = math.nan
    elapsed = (
        completion_time(consumer_writes[-1])
        - parse_time(producer_writes[0]["called_at"])
    ).total_seconds()
    consumer_write_seconds = sum(
        record["duration_seconds"] for record in consumer_writes
    )
    latest_produced = producer_writes[-1]["step"]
    latest_consumed = consumer_writes[-1]["step"]
    total_steps = settings.get("total_steps")
    target_last_step = total_steps - 1 if total_steps is not None else None

    return {
        "producer_writes": producer_writes,
        "consumer_writes": consumer_writes,
        "receives": receives,
        "latency_by_step": latency_by_step,
        "latency": distribution(latency_by_step.values()),
        "cadence": distribution(cadence),
        "producer_enqueue": distribution(
            record["duration_seconds"] for record in producer_writes
        ),
        "consumer_write": distribution(
            record["duration_seconds"] for record in consumer_writes
        ),
        "latest_produced": latest_produced,
        "latest_consumed": latest_consumed,
        "produced_steps": len(producer_writes),
        "consumed_steps": len(consumer_writes),
        "producer_contiguous": is_contiguous(producer_writes),
        "consumer_contiguous": is_contiguous(consumer_writes),
        "producer_bad_writes": sum(
            record.get("event") in ("raw_output.write", "io.write")
            and record.get("status") != "ok"
            for record in producer
        ),
        "consumer_bad_writes": sum(
            record.get("event") == "io.write" and record.get("status") != "ok"
            for record in consumer
        ),
        "drops": sum(record.get("event") == "io.buffer_drop" for record in producer),
        "connection_losses": sum(
            record.get("event") == "consumer.connection_lost" for record in producer
        ),
        "launch_retries": sum(
            record.get("event") == "consumer.launch_retry" for record in producer
        ),
        "io_retries": sum(record.get("event") == "io.retry" for record in producer),
        "bytes_per_step": bytes_per_step,
        "raw_bytes_written": bytes_per_step * len(consumer_writes),
        "elapsed_seconds": elapsed,
        "sustained_mib_per_second": (
            bytes_per_step * len(consumer_writes) / 2**20 / elapsed
            if elapsed > 0 else math.nan
        ),
        "aggregate_write_mib_per_second": (
            bytes_per_step * len(consumer_writes) / 2**20 / consumer_write_seconds
            if consumer_write_seconds > 0 else math.nan
        ),
        "maximum_unacknowledged": maximum_outstanding,
        "maximum_queued": max(0, maximum_outstanding - 1),
        "maximum_queue_at": maximum_at,
        "snapshot_tail_difference": latest_produced - latest_consumed,
        "schedule_drift_seconds": drift[-1],
        "finish_time": (
            first_time + timedelta(
                seconds=(target_last_step - first_step)
                * settings["interval_seconds"]
            )
            if target_last_step is not None else None
        ),
        "remaining_steps": (
            max(0, target_last_step - latest_produced)
            if target_last_step is not None else None
        ),
        "remaining_seconds": (
            max(0, target_last_step - latest_produced)
            * settings["interval_seconds"]
            if target_last_step is not None else None
        ),
        "completion_fraction": (
            min(1.0, (latest_produced + 1) / total_steps)
            if total_steps is not None else None
        ),
        "clusters": latency_clusters(latency_by_step),
    }


def format_duration(seconds):
    seconds = max(0, round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def process_rss_mib(process_line):
    if not process_line:
        return None
    fields = process_line.split(None, 6)
    if len(fields) < 7:
        return None
    try:
        return int(fields[4]) / 1024
    except ValueError:
        return None


def sampled_bytes_per_step(remote):
    payload = remote.get("payload") or {}
    sampled_steps = payload.get("sampled_steps", 0)
    if not sampled_steps:
        return None
    return payload["estimated_raw_bytes"] / sampled_steps


def render_report(
    config, config_path, producer_log, producer_stdout, settings, summary, remote
):
    now = datetime.now().astimezone()
    latency = summary["latency"]
    cadence = summary["cadence"]
    consumer_write = summary["consumer_write"]
    buffer_capacity = math.ceil(
        settings["buffer_seconds"] / settings["interval_seconds"]
    )
    peak_percent = 100 * summary["maximum_queued"] / buffer_capacity
    disk_gib = remote.get("disk_kib", 0) / 2**20
    payload_known = math.isfinite(summary["bytes_per_step"])
    payload_per_shot = (
        f"{summary['bytes_per_step'] / 2**20:.3f} MiB"
        if payload_known else "not available"
    )
    raw_payload = (
        f"{summary['raw_bytes_written'] / 2**30:.3f} GiB"
        if payload_known else "not available"
    )
    queued_payload = (
        f"{summary['maximum_queued'] * summary['bytes_per_step'] / 2**20:.1f} MiB raw"
        if payload_known else "raw size unavailable"
    )
    sustained_rate = (
        f"{summary['sustained_mib_per_second']:.3f} MiB/s"
        if payload_known else "not available"
    )
    aggregate_rate = (
        f"{summary['aggregate_write_mib_per_second']:.1f} MiB/s"
        if payload_known else "not available"
    )
    total_steps = settings.get("total_steps")
    completion = (
        f"{summary['produced_steps']:,} / {total_steps:,} "
        f"({summary['completion_fraction']:.2%})"
        if total_steps is not None else f"{summary['produced_steps']:,} observed"
    )
    remaining = (
        f"{summary['remaining_steps']:,} shots; "
        f"{format_duration(summary['remaining_seconds'])}"
        if total_steps is not None else "not available (no target supplied)"
    )
    finish = (
        summary["finish_time"].isoformat(timespec="seconds")
        if summary["finish_time"] is not None else "not available"
    )
    final_raw_gib = (
        total_steps * summary["bytes_per_step"] / 2**30
        if total_steps is not None and payload_known else None
    )
    status = "healthy"
    if (
        summary["drops"]
        or summary["producer_bad_writes"]
        or summary["consumer_bad_writes"]
        or not summary["producer_contiguous"]
        or not summary["consumer_contiguous"]
    ):
        status = "needs attention"

    lines = [
        "# Remote experiment analysis",
        "",
        f"Generated: **{now.isoformat(timespec='seconds')}**  ",
        f"Configuration: `{config_path}`  ",
        f"Remote endpoint: `{config.host}` (`{remote.get('hostname', 'unknown')}`)",
        "",
        "## Summary",
        "",
        f"The experiment is **{status}**. The producer reached shot "
        f"**{summary['latest_produced']:,}** and the consumer reached shot "
        f"**{summary['latest_consumed']:,}** in the captured timing logs.",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Completed | {completion} |",
        f"| Remaining | {remaining} |",
        f"| Scheduled finish | {finish} |",
        f"| Producer/consumer tail difference | "
        f"{summary['snapshot_tail_difference']:+d} shots |",
        f"| Failed producer/consumer writes | "
        f"{summary['producer_bad_writes']} / {summary['consumer_bad_writes']} |",
        f"| Dropped buffered shots | {summary['drops']} |",
        f"| Connection losses / launch retries / I/O retries | "
        f"{summary['connection_losses']} / {summary['launch_retries']} / "
        f"{summary['io_retries']} |",
        "",
        "## Timing and throughput",
        "",
        "| Metric | Mean | Median | p95 | p99 | Maximum |",
        "|---|---:|---:|---:|---:|---:|",
        f"| Acquisition cadence | {cadence['mean']:.6f} s | "
        f"{cadence['median']:.6f} s | {cadence['p95']:.6f} s | "
        f"{cadence['p99']:.6f} s | {cadence['max']:.6f} s |",
        f"| End-to-end shot latency | {latency['mean']:.3f} s | "
        f"{latency['median']:.3f} s | {latency['p95']:.3f} s | "
        f"{latency['p99']:.3f} s | {latency['max']:.3f} s |",
        f"| Consumer output write | {consumer_write['mean'] * 1000:.3f} ms | "
        f"{consumer_write['median'] * 1000:.3f} ms | "
        f"{consumer_write['p95'] * 1000:.3f} ms | "
        f"{consumer_write['p99'] * 1000:.3f} ms | "
        f"{consumer_write['max']:.3f} s |",
        "",
        f"- Sustained uncompressed application throughput: "
        f"**{sustained_rate}**.",
        f"- Aggregate output throughput while writes were active: "
        f"**{aggregate_rate}**.",
        f"- Cumulative production schedule drift: "
        f"**{summary['schedule_drift_seconds']:.3f} s**.",
        "",
        "## Reliability and buffering",
        "",
        f"- Producer shots contiguous: **{'yes' if summary['producer_contiguous'] else 'no'}**.",
        f"- Consumer shots contiguous: **{'yes' if summary['consumer_contiguous'] else 'no'}**.",
        f"- Ring-buffer capacity: **{buffer_capacity} shots** "
        f"({settings['buffer_seconds']:g} seconds).",
        f"- Maximum unacknowledged shots: **{summary['maximum_unacknowledged']}**.",
        f"- Estimated maximum queued shots: **{summary['maximum_queued']}** "
        f"({peak_percent:.1f}% of capacity, "
        f"{queued_payload}).",
        "",
        "## Data volume",
        "",
        f"- Mean sampled payload per shot: **{payload_per_shot}**.",
        f"- Raw payload consumed: **{raw_payload}**.",
        f"- Remote disk allocation: **{disk_gib:.3f} GiB**.",
        f"- Output segments: **{len(remote.get('segments', []))}**.",
        (
            f"- Expected final raw payload: **{final_raw_gib:.3f} GiB**."
            if final_raw_gib is not None
            else "- Expected final raw payload: **not available**."
        ),
    ]

    compression = remote.get("compression")
    if compression and compression.get("ratio"):
        ratio = compression["ratio"]
        estimated_wire = summary["raw_bytes_written"] / ratio
        savings = 100 * (1 - 1 / ratio)
        wire_rate = estimated_wire / 2**20 / summary["elapsed_seconds"]
        samples = compression.get("samples", [])
        first_ratio = samples[0]["ratio"] if samples else math.nan
        last_ratio = samples[-1]["ratio"] if samples else math.nan
        lines.extend(
            [
                "",
                "## Compression estimate",
                "",
                f"Midpoint sampling of **{len(samples)}** output segments estimates:",
                "",
                f"- Run-wide payload compression ratio: **{ratio:.2f}x**.",
                f"- Estimated wire payload: **{estimated_wire / 2**30:.1f} GiB**.",
                f"- Estimated wire-payload reduction: **{savings:.1f}%**.",
                f"- Estimated compressed payload rate: **{wire_rate:.2f} MiB/s** "
                f"({wire_rate * 2**20 * 8 / 1e6:.1f} Mbit/s).",
                f"- First/latest segment sample ratios: **{first_ratio:.2f}x / "
                f"{last_ratio:.2f}x**.",
                "",
                "The estimate excludes socket framing, SSH, and TCP/IP overhead. "
                "Compression applies to the wire payload; the BP output is written "
                "after decompression.",
            ]
        )
    elif remote.get("compression_error"):
        lines.extend(
            [
                "",
                "## Compression estimate",
                "",
                f"Compression sampling was unavailable: "
                f"`{remote['compression_error']}`",
            ]
        )

    if remote.get("payload_error"):
        lines.extend(
            [
                "",
                "Payload sampling was unavailable: "
                f"`{remote['payload_error']}`",
            ]
        )

    clusters = summary["clusters"]
    lines.extend(["", "## Latency incidents", ""])
    if not clusters:
        lines.append("No contiguous latency intervals exceeded three seconds.")
    else:
        lines.extend(
            [
                f"There were **{len(clusters)}** contiguous intervals above three "
                f"seconds, affecting **{sum(len(group) for group in clusters)}** shots.",
                "",
                "| Shots | Count | Maximum latency | Started |",
                "|---|---:|---:|---|",
            ]
        )
        producer_by_step = {
            record["step"]: record for record in summary["producer_writes"]
        }
        for group in clusters:
            maximum = max(summary["latency_by_step"][step] for step in group)
            step_range = (
                str(group[0]) if len(group) == 1 else f"{group[0]}–{group[-1]}"
            )
            started = producer_by_step[group[0]]["called_at"]
            lines.append(
                f"| {step_range} | {len(group)} | {maximum:.3f} s | {started} |"
            )

    rss = process_rss_mib(remote.get("consumer_process"))
    lines.extend(["", "## Remote process", ""])
    if remote.get("consumer_process"):
        lines.append(f"- Consumer process: `{remote['consumer_process']}`")
        if rss is not None:
            lines.append(f"- Consumer resident memory: **{rss:.1f} MiB**.")
    else:
        lines.append(
            "The consumer PID was not visible on this SSH endpoint. This can happen "
            "when the configured hostname load-balances across several DTNs."
        )

    lines.extend(
        [
            "",
            "## Inputs and methodology",
            "",
            f"- Producer timing log: `{producer_log}`",
            f"- Producer stdout log: `{producer_stdout}`",
            f"- Consumer timing log: `{config.timing_log}` on `{config.host}`",
            "- End-to-end latency is measured from producer enqueue time through "
            "completion of the matching consumer write.",
            "- The analyzer uses one multiplexed SSH connection for all remote reads.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="remote server configuration file")
    parser.add_argument("report", type=Path, help="Markdown report to write")
    parser.add_argument(
        "--producer-log",
        type=Path,
        help="producer JSONL timing log (normally derived from the config)",
    )
    parser.add_argument(
        "--producer-stdout",
        type=Path,
        help="producer stdout log (normally derived from the config)",
    )
    parser.add_argument(
        "--skip-compression",
        action="store_true",
        help="skip compression encoding while retaining raw-payload sampling",
    )
    parser.add_argument(
        "--total-shots",
        type=lambda value: positive_int(value, "total shots"),
        help="planned shot count (enables completion and finish estimates)",
    )
    parser.add_argument(
        "--interval-seconds",
        type=lambda value: positive_float(value, "output interval"),
        help="planned shot interval (default: infer the median from the log)",
    )
    parser.add_argument(
        "--buffer-seconds",
        type=lambda value: positive_float(value, "buffer duration"),
        help="producer buffer duration (default: 600)",
    )
    parser.add_argument(
        "--ssh-control-path",
        type=Path,
        default=os.environ.get("BAPSF_INTERFEROMETER_SSH_CONTROL_PATH"),
        help=(
            "reuse an existing OpenSSH control socket without closing it "
            "(or set BAPSF_INTERFEROMETER_SSH_CONTROL_PATH)"
        ),
    )
    return parser.parse_args(argv)


def positive_float(value, description):
    try:
        converted = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{description} must be a number") from exc
    if not math.isfinite(converted) or converted <= 0:
        raise argparse.ArgumentTypeError(f"{description} must be greater than zero")
    return converted


def positive_int(value, description):
    try:
        converted = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{description} must be an integer") from exc
    if converted <= 0:
        raise argparse.ArgumentTypeError(f"{description} must be greater than zero")
    return converted


def main(argv=None):
    args = parse_arguments(argv)
    try:
        if args.report.suffix.lower() != ".md":
            raise ValueError("report path must have a .md extension")
        config = ServerConfig(args.config)
        producer_log, producer_stdout = derive_local_paths(
            config, args.producer_log, args.producer_stdout
        )
        producer = read_json_lines(producer_log)
        stdout_settings = {}
        if args.producer_stdout is not None or producer_stdout.is_file():
            stdout_settings = parse_producer_stdout(
                producer_stdout, require_mock_header=False
            )
        settings = experiment_settings(
            producer,
            stdout_settings,
            total_shots=args.total_shots,
            interval_seconds=args.interval_seconds,
            buffer_seconds=args.buffer_seconds,
        )
        operation = (
            load_data_operation(config.compression_config)
            if config.compression_config and not args.skip_compression else None
        )
        operation_description = operation.description() if operation else None
        consumer_bytes, remote = remote_snapshot(
            config,
            operation_description,
            skip_compression=args.skip_compression,
            control_path=args.ssh_control_path,
        )
        consumer = read_json_lines_bytes(
            consumer_bytes, f"{config.host}:{config.timing_log}"
        )
        summary = summarize(
            producer,
            consumer,
            settings,
            bytes_per_step=sampled_bytes_per_step(remote),
        )
        report = render_report(
            config,
            args.config,
            producer_log,
            producer_stdout,
            settings,
            summary,
            remote,
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
        print(f"Wrote {args.report}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
