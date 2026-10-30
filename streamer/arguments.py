"""Command-line and environment configuration shared by raw-output producers."""

import math
import os
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
	socket_timeout_seconds: float = 30.0  # connect and per-step ack, as a .conf's default
	retry_delay_seconds: float = 3.0  # after any output error, before opening a new output
	file_interval_seconds: float = 3600.0  # a directory destination starts a new file this often

	def __post_init__(self):
		# Here, not where each value is used: file_interval_seconds is first used on the writer thread.
		for name, check in (
			("buffer_seconds", positive_float),
			("output_interval_seconds", positive_float),
			("socket_timeout_seconds", positive_float),
			("file_interval_seconds", positive_float),
			("retry_delay_seconds", nonnegative_float),
		):
			try:
				check(getattr(self, name))
			except ValueError as exc:
				raise ValueError(f"{name} {exc}") from None


def positive_float(value):
	converted = float(value)
	if not math.isfinite(converted) or converted <= 0:
		raise ValueError("must be greater than zero")
	return converted


def nonnegative_float(value):
	converted = float(value)
	if not math.isfinite(converted) or converted < 0:
		raise ValueError("must not be negative")
	return converted


def add_raw_output_arguments(parser, default_timing_log):
	group = parser.add_argument_group("raw output")
	group.add_argument(
		"--raw-output",
		metavar="DESTINATION",
		help="ADIOS path, directory for timestamped ADIOS files, "
		"encrypted connection file, or server .conf",
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
		"--raw-output-socket-timeout",
		type=positive_float,
		default=RawOutputSettings.socket_timeout_seconds,
		help="seconds for a socket connect or step acknowledgement (default: %(default)g)",
	)
	group.add_argument(
		"--raw-output-retry-delay",
		type=nonnegative_float,
		default=RawOutputSettings.retry_delay_seconds,
		help="seconds between output retries after an error (default: %(default)g)",
	)
	group.add_argument(
		"--raw-output-file-interval-seconds",
		type=positive_float,
		default=RawOutputSettings.file_interval_seconds,
		help="seconds between new files in a directory destination (default: %(default)g)",
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
		socket_timeout_seconds=args.raw_output_socket_timeout,
		retry_delay_seconds=args.raw_output_retry_delay,
		file_interval_seconds=args.raw_output_file_interval_seconds,
	)


_ENV_SETTINGS = (
	("private_key", "PRIVATE_KEY", str),
	("compression_config", "COMPRESSION", str),
	("buffer_seconds", "BUFFER_S", float),
	("file_interval_seconds", "FILE_S", float),
	("retry_delay_seconds", "RETRY_S", float),
	("socket_timeout_seconds", "SOCKET_TIMEOUT_S", float),
)


def settings_from_env(prefix, timing_log, environ=None, **defaults):
	"""Settings from ``<prefix>`` (the destination) and ``<prefix>_<NAME>`` (_ENV_SETTINGS), which override ``defaults``.

	None when ``<prefix>`` is unset or empty.
	"""
	environ = os.environ if environ is None else environ
	destination = environ.get(prefix)
	if not destination:
		return None
	values = dict(defaults)
	for field, name, convert in _ENV_SETTINGS:
		value = environ.get(f"{prefix}_{name}")
		if value:
			values[field] = convert(value)
	return RawOutputSettings(destination=destination, timing_log=str(timing_log), **values)
