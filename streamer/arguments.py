"""Command-line configuration shared by raw-output producers."""

import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RawOutputSettings:
	destination: str
	timing_log: str
	private_key: str | None = None
	buffer_seconds: float = 600.0
	output_interval_seconds: float = 3.0
	compression_config: str | None = None
	engine: str = "BP5"
	launch_mode: str = "none"
	launch_command: str | None = None
	retry_interval_seconds: float = 1.0
	reconnect_timeout_seconds: float = 0.0


def positive_float(value):
	converted = float(value)
	if not math.isfinite(converted) or converted <= 0:
		raise ValueError("must be greater than zero")
	return converted


def nonnegative_float(value):
	converted = float(value)
	if not math.isfinite(converted) or converted < 0:
		raise ValueError("must be zero or greater")
	return converted


def add_raw_output_arguments(parser, default_timing_log):
	group = parser.add_argument_group("raw output")
	group.add_argument(
		"--raw-output",
		metavar="DESTINATION",
		help="ADIOS path, encrypted connection file, or server .conf",
	)
	group.add_argument(
		"--raw-output-private-key",
		metavar="FILE",
		help="Curve25519 private key required for socket output",
	)
	group.add_argument(
		"--raw-output-timing-log",
		type=Path,
		default=default_timing_log,
		help=f"timing log (default: {default_timing_log})",
	)
	group.add_argument(
		"--raw-output-buffer-seconds",
		type=positive_float,
		default=600.0,
		help="queued output duration (default: 600)",
	)
	group.add_argument(
		"--raw-output-compression-config",
		metavar="FILE",
		help="optional array-operation configuration",
	)
	group.add_argument(
		"--raw-output-engine",
		default="BP5",
		help="ADIOS engine for a direct output (default: BP5)",
	)
	group.add_argument(
		"--raw-output-launch-mode",
		choices=("none", "system"),
		default="none",
		help="WANStreamer consumer launch policy (default: none)",
	)
	group.add_argument(
		"--raw-output-launch-command",
		help="trusted command template for WANStreamer LaunchMode=system",
	)
	group.add_argument(
		"--raw-output-retry-interval-seconds",
		type=positive_float,
		default=1.0,
		help="WANStreamer reconnect interval (default: 1)",
	)
	group.add_argument(
		"--raw-output-reconnect-timeout-seconds",
		type=nonnegative_float,
		default=0.0,
		help="WANStreamer reconnect deadline; zero retries forever (default: 0)",
	)


def settings_from_args(args):
	if args.raw_output is None:
		return None
	return RawOutputSettings(
		destination=args.raw_output,
		private_key=args.raw_output_private_key,
		timing_log=str(args.raw_output_timing_log),
		buffer_seconds=args.raw_output_buffer_seconds,
		output_interval_seconds=args.period if args.period > 0 else 3.0,
		compression_config=args.raw_output_compression_config,
		engine=args.raw_output_engine,
		launch_mode=args.raw_output_launch_mode,
		launch_command=args.raw_output_launch_command,
		retry_interval_seconds=args.raw_output_retry_interval_seconds,
		reconnect_timeout_seconds=args.raw_output_reconnect_timeout_seconds,
	)
