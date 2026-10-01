import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from streamer.analyze_remote_experiment import (
    experiment_settings,
    parse_producer_stdout,
    process_rss_mib,
    render_report,
    sampled_bytes_per_step,
    summarize,
)


class AnalyzeRemoteExperimentTests(unittest.TestCase):
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
            "disk_kib": 1,
            "segments": ["one.bp"],
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
        self.assertIn("3 / 4 (75.00%)", report)
        self.assertIn("Mean sampled payload per shot: **1.000 MiB**", report)

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
