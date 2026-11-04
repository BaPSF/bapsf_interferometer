import tempfile
import json
import os
import subprocess
import sys
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from streamer.analyze_remote_experiment import (
    REMOTE_SNAPSHOT_PROGRAM,
    bp_shot_indexes,
    experiment_settings,
    normalize_consumer_writes,
    records_at_cutoff,
    parse_producer_stdout,
    process_rss_mib,
    render_report,
    sampled_bytes_per_step,
    summarize,
)


class AnalyzeRemoteExperimentTests(unittest.TestCase):
    def test_remote_snapshot_survives_native_reader_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            (output / "broken.bp").mkdir()
            (output / "healthy.bp").mkdir()
            (output / "broken.bp" / "md.idx").write_bytes(b"metadata")
            (output / "healthy.bp" / "md.idx").write_bytes(b"metadata")
            (output / "empty.bp").mkdir()
            (output / "empty.bp" / "md.idx").touch()
            (root / "consumer.pid").write_text(str(os.getpid()))
            (root / "stdout.log").write_text("still running\n")
            (root / "adios2.py").write_text(textwrap.dedent("""
                import os
                import signal
                class Value:
                    nbytes = 8
                    def __init__(self, count):
                        self.count = count
                    def reshape(self, *_):
                        return list(range(42, 42 + self.count))
                class FileReader:
                    def __init__(self, path):
                        if path.endswith("broken.bp"):
                            os.kill(os.getpid(), signal.SIGTERM)
                    def available_variables(self):
                        return {"shot_index": {"SingleValue": "false", "AvailableStepsCount": "2"}}
                    def read(self, *args, **kwargs):
                        return Value(kwargs["step_selection"][1])
                    def close(self):
                        pass
            """))
            package = root / "streamer"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "data_operations.py").write_text(
                "def data_operation_from_description(value): return None\n"
            )
            completed = subprocess.run(
                [sys.executable, "-c", REMOTE_SNAPSHOT_PROGRAM, str(output),
                 str(root / "consumer.pid"), str(root / "stdout.log"), ""],
                cwd=root, capture_output=True, text=True, timeout=15, check=True,
            )
            metadata = json.loads(completed.stdout)
        self.assertEqual(metadata["payload"]["sampled_steps"], 2)
        self.assertEqual(metadata["payload"]["estimated_raw_bytes"], 16)
        self.assertEqual(metadata["payload_sampling_errors"][0]["segment"], "broken.bp")
        self.assertIn("signal 15", metadata["payload_sampling_errors"][0]["error"])
        self.assertEqual(metadata["payload_sampling_errors"][1]["segment"], "empty.bp")
        self.assertIn("Empty BP segment", metadata["payload_sampling_errors"][1]["error"])

    def test_legacy_restart_uses_bp_shots_for_counts_and_latency(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        producer = [dict(event="raw_output.write", shot=shot, status="ok",
                         called_at=(start + timedelta(seconds=3 * shot)).isoformat(),
                         duration_seconds=0.001) for shot in range(6)]
        consumer = []
        # The failed restart receives a shot but never completes a write.
        for shot, step in enumerate([0, 1, 2, 0, 1, 2]):
            if shot == 3:
                consumer.append(dict(event="socket.receive_all", step=0))
            consumer.append(dict(event="io.write", step=step, status="ok",
                                 called_at=(start + timedelta(seconds=3 * shot + 0.5)).isoformat(),
                                 duration_seconds=0.01))
        remote = {"payload": {"samples": [
            {"shot_indexes": [0, 1, 2]}, {"shot_indexes": [3, 4, 5]}]},
            "payload_sampling_errors": [{"segment": "empty.bp", "error": "Empty BP segment"}]}
        result = summarize(producer, consumer, experiment_settings(producer),
                           consumer_shots=bp_shot_indexes(remote), bytes_per_step=8)
        self.assertEqual(result["consumed_steps"], 6)
        self.assertEqual(result["latest_consumed"], 5)
        self.assertEqual(result["snapshot_tail_difference"], 0)
        self.assertTrue(result["consumer_contiguous"])
        self.assertAlmostEqual(result["latency"]["max"], 0.51)
        self.assertEqual(result["drops"], 0)

    def test_missing_nonempty_bp_segment_cannot_shift_legacy_matching(self):
        remote = {"payload": {"samples": [{"shot_indexes": [10, 11]}]},
                  "payload_sampling_errors": [{"segment": "earlier.bp", "error": "signal 11"}]}
        self.assertIsNone(bp_shot_indexes(remote))

    def test_legacy_reset_requires_shot_evidence(self):
        writes = [dict(event="io.write", status="ok", step=step) for step in [0, 1, 0]]
        with self.assertRaisesRegex(ValueError, "BP shot indexes are required"):
            normalize_consumer_writes(writes)
        with self.assertRaisesRegex(ValueError, "do not cover"):
            normalize_consumer_writes(writes, [0, 1])

    def test_global_shots_keep_replay_and_real_gap_distinct(self):
        writes = [dict(event="io.write", status="ok", step=step, shot=shot)
                  for step, shot in [(0, 0), (1, 1), (0, 1), (1, 3)]]
        normalized, duplicates = normalize_consumer_writes(writes)
        self.assertEqual([row["step"] for row in normalized], [0, 1, 3])
        self.assertEqual(duplicates, 1)

    def cutoff_fixture(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        producer = [dict(event="raw_output.write", shot=shot, status="ok",
                         called_at=(start + timedelta(seconds=3 * shot)).isoformat(),
                         duration_seconds=0.001) for shot in range(6)]
        consumer = [dict(event="io.write", shot=shot, step=shot, status="ok",
                         called_at=(start + timedelta(seconds=3 * shot + 0.1)).isoformat(),
                         duration_seconds=0.01) for shot in range(6)]
        return start, producer, consumer

    def test_common_cutoff_excludes_later_consumer_and_producer_events(self):
        start, producer, consumer = self.cutoff_fixture()
        cutoff = start + timedelta(seconds=6.5)
        result = summarize(records_at_cutoff(producer, cutoff),
                           records_at_cutoff(consumer, cutoff),
                           experiment_settings(producer, total_shots=6),
                           snapshot_at=cutoff)
        self.assertEqual(result["produced_steps"], 3)
        self.assertEqual(result["consumed_steps"], 3)
        self.assertEqual(result["snapshot_tail_difference"], 0)
        self.assertEqual(result["outstanding_shots"], 0)

    def test_completed_is_consumer_writes_and_preserves_inflight_shot(self):
        start, producer, consumer = self.cutoff_fixture()
        cutoff = start + timedelta(seconds=6.05)
        settings = experiment_settings(producer, total_shots=6)
        result = summarize(records_at_cutoff(producer, cutoff),
                           records_at_cutoff(consumer, cutoff), settings,
                           snapshot_at=cutoff)
        self.assertEqual(result["produced_steps"], 3)
        self.assertEqual(result["consumed_steps"], 2)
        self.assertEqual(result["remaining_steps"], 4)
        self.assertEqual(result["completion_fraction"], 2 / 6)
        self.assertEqual(result["outstanding_shots"], 1)
        self.assertEqual(result["snapshot_tail_difference"], 1)
        report = render_report(SimpleNamespace(host="host", timing_log="log"),
                               Path("conf"), Path("producer"), Path("stdout"),
                               settings, result, {})
        self.assertIn("| Completed | 2 / 6 (33.33%) |", report)
        self.assertIn("| Produced | 3 |", report)
        self.assertIn("| Produced but not completed | 1 |", report)

    def test_common_cutoff_does_not_hide_stalled_consumer_backlog(self):
        start, producer, consumer = self.cutoff_fixture()
        cutoff = start + timedelta(seconds=16)
        result = summarize(records_at_cutoff(producer, cutoff),
                           records_at_cutoff(consumer[:2], cutoff),
                           experiment_settings(producer))
        self.assertEqual(result["produced_steps"], 6)
        self.assertEqual(result["consumed_steps"], 2)
        self.assertEqual(result["outstanding_shots"], 4)
        self.assertEqual(result["snapshot_tail_difference"], 4)

    def test_reads_rss_from_remote_ps_line(self):
        process = "123 00:01 1.0 0.1 2048 S python consumer.py"

        self.assertEqual(process_rss_mib(process), 2.0)

    def test_parses_legacy_producer_header_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "producer.stdout.log"
            path.write_text(
                "\n".join(
                    (
                        "Array size             : 512 x 256",
                        "Number of output steps : 100",
                        "Output interval        : 3 seconds",
                        "Output buffer          : 600 seconds",
                        "Using engine           : BP5",
                    )
                ),
                encoding="utf-8",
            )

            settings = parse_producer_stdout(path)

        self.assertEqual(settings["nx"], 512)
        self.assertEqual(settings["ny"], 256)
        self.assertEqual(settings["total_steps"], 100)
        self.assertEqual(settings["interval_seconds"], 3.0)
        self.assertEqual(settings["buffer_seconds"], 600.0)

    def test_infers_interferometer_settings_from_raw_output_log(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        producer = [
            {
                "event": "raw_output.write",
                "shot": shot,
                "called_at": (start + timedelta(seconds=3 * shot)).isoformat(),
                "duration_seconds": 0.001,
                "status": "ok",
            }
            for shot in range(3)
        ]

        settings = experiment_settings(producer, total_shots=4)

        self.assertEqual(settings["interval_seconds"], 3.0)
        self.assertEqual(settings["buffer_seconds"], 600.0)
        self.assertEqual(settings["total_steps"], 4)

    def test_summarizes_raw_shots_and_renders_markdown(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        producer = []
        consumer = []
        for shot in range(3):
            called = start + timedelta(seconds=3 * shot)
            producer.append(
                {
                    "event": "raw_output.write",
                    "shot": shot,
                    "called_at": called.isoformat(),
                    "duration_seconds": 0.001,
                    "status": "ok",
                }
            )
            consumer.append(
                {
                    "event": "socket.receive_all",
                    "step": shot,
                    "duration_seconds": 0.4,
                }
            )
            consumer.append(
                {
                    "event": "io.write",
                    "step": shot,
                    "shot": shot,
                    "called_at": (called + timedelta(seconds=0.5)).isoformat(),
                    "duration_seconds": 0.01,
                    "status": "ok",
                }
            )
        settings = {
            "total_steps": 4,
            "interval_seconds": 3.0,
            "buffer_seconds": 6.0,
        }
        remote = {
            "hostname": "remote.example",
            "consumer_process": "123 00:01 1.0 0.1 2048 S python consumer.py",
            "disk_kib": 1,
            "segments": ["one.bp", "empty.bp"],
            "payload_sampling_errors": [{"segment": "empty.bp", "error": "Empty BP segment"}],
            "payload": {
                "sampled_steps": 3,
                "estimated_raw_bytes": 3 * 1024 * 1024,
            },
        }
        summary = summarize(
            producer,
            consumer,
            settings,
            bytes_per_step=sampled_bytes_per_step(remote),
        )
        config = SimpleNamespace(
            host="user@example",
            timing_log="consumer.jsonl",
        )

        report = render_report(
            config,
            Path("server.conf"),
            Path("producer.jsonl"),
            Path("producer.stdout.log"),
            settings,
            summary,
            remote,
        )

        self.assertEqual(summary["produced_steps"], 3)
        self.assertEqual(summary["remaining_steps"], 1)
        self.assertAlmostEqual(summary["latency"]["median"], 0.51)
        self.assertIn("The experiment is **healthy**", report)
        self.assertIn("`empty.bp`: Empty BP segment", report)
        self.assertIn("3 / 4 (75.00%)", report)
        self.assertIn("Mean sampled payload per shot: **1.000 MiB**", report)
        process_section = report.split("## Remote process", 1)[1]
        self.assertIn("- Consumer hostname: `remote.example`", process_section)

    def test_open_ended_run_omits_completion_estimate(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        producer = [
            {
                "event": "raw_output.write",
                "shot": shot,
                "called_at": (start + timedelta(seconds=shot)).isoformat(),
                "duration_seconds": 0.001,
                "status": "ok",
            }
            for shot in range(2)
        ]
        consumer = [
            {
                "event": "io.write",
                "step": shot,
                "called_at": (
                    start + timedelta(seconds=shot, milliseconds=100)
                ).isoformat(),
                "duration_seconds": 0.01,
                "status": "ok",
            }
            for shot in range(2)
        ]
        settings = experiment_settings(producer)
        summary = summarize(producer, consumer, settings, bytes_per_step=1024)
        config = SimpleNamespace(host="example", timing_log="consumer.jsonl")

        report = render_report(
            config,
            Path("server.conf"),
            Path("producer.jsonl"),
            Path("producer.stdout.log"),
            settings,
            summary,
            {"segments": []},
        )

        self.assertIsNone(summary["completion_fraction"])
        self.assertIn("2 observed", report)
        self.assertIn("no target supplied", report)


if __name__ == "__main__":
    unittest.main()
