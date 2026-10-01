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


def positive_float(value):
	converted = float(value)
	if not math.isfinite(converted) or converted <= 0:
		raise ValueError("must be greater than zero")
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
	)
