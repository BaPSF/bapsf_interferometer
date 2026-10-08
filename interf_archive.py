# -*- coding: utf-8 -*-
"""Read the raw ADIOS archive (`--raw-output`) back as shots, for offline reanalysis (docs/ARCHITECTURE.md D9).

The archive is written by streamer, which another group maintains and we never edit (D6), so its reader lives
here. Typical use: `analyze_shot(decode(variables)) for variables in iter_steps(path)`. The format is
streamer.payload's schema 1, with no shot identity (D14): decode() does not add one. To date an archived shot,
apply interf_shot.trigger_time to its stored WAVEDESC.
"""
from dataclasses import dataclass

import numpy as np

from streamer.payload import SCHEMA_VERSION, decode_json

try:
	import adios2
except ImportError:
	adios2 = None

ADIOS2_AVAILABLE = adios2 is not None


def _steps(stream, path):
	"""Begin each step written so far in turn; yields the step index while inside the step.

	Not adios2.Stream.steps(): on a file whose writer is still running, or died without closing
	it, that waits forever for the next step, holding the GIL so every thread of the process
	stops. begin_step(timeout=0) returns NotReady there instead, and the reading ends.
	"""
	index = 0
	while True:
		status = stream.begin_step(timeout=0.0)
		if status == adios2.bindings.StepStatus.OtherError:
			raise RuntimeError(f"{path}: ADIOS2 error at step {index}")
		if status != adios2.bindings.StepStatus.OK:  # EndOfStream, or NotReady: nothing newer written
			return
		yield index
		stream.end_step()
		index += 1


def _step_variables(stream):
	"""{name: array} of the current step, each array read at this step's own shape.

	The explicit count matters with BP4, which keeps a variable's selection from an earlier
	step: a plain read would truncate an array that grew, and fail on one that shrank.
	"""
	variables = {}
	for name in stream.available_variables():
		shape = stream.inquire_variable(name).shape()
		variables[name] = stream.read(name, start=[0] * len(shape), count=shape) if shape else stream.read(name)
	return variables


def iter_steps(path):
	"""Yield each step of a raw-output BP file as {name: array}, as that step was written.

	A step holds only its own variables (a channel or scope that was not read is absent) at its
	own shapes (missing_json and record lengths change between steps). Prefer this to
	adios2.FileReader.read(name, step_selection=[i, 1]): ADIOS2 counts step_selection per
	variable, so a variable absent from earlier steps returns a later step's data, and a read
	without an explicit count takes the variable's first shape.

	Only the steps written so far are read: on a file still being written, or whose writer died
	without closing it, iteration ends at the last complete step instead of waiting.
	"""
	if not ADIOS2_AVAILABLE:
		raise RuntimeError("adios2 is not available")
	with adios2.Stream(str(path), "r") as stream:
		for _ in _steps(stream, path):
			yield _step_variables(stream)


def read_step(path, index):
	"""Step `index` (from 0) as {name: array}, like iter_steps; earlier steps are skipped unread."""
	if not ADIOS2_AVAILABLE:
		raise RuntimeError("adios2 is not available")
	if index < 0:
		raise IndexError(f"step {index}: index must be >= 0")
	with adios2.Stream(str(path), "r") as stream:
		for current in _steps(stream, path):
			if current == index:
				return _step_variables(stream)
	raise IndexError(f"{path} has no step {index}")


@dataclass
class ArchiveShot:
	"""A shot rebuilt by decode(); `lecroy`, `rigol` and `missing` are laid out as in RawShot, channels upper case."""
	schema_version: int
	shot_index: int  # RawOutput's own per-process step counter, from 0 at each start: not a shot identity
	host_time: float
	critical_path_s: float
	missing: dict[str, str]
	lecroy: dict[str, tuple[np.ndarray, bytes]]
	rigol: dict[str, tuple[np.ndarray, dict]]


def _scalar(value):
	# .item(), not int()/float(): adios2 returns a scalar as shape (1,), and int()/float() of an ndim > 0
	# array is deprecated in NumPy. .item() takes shape () or (1,) and raises ValueError for anything larger.
	return np.asarray(value).item()


def decode(variables):
	"""Inverse of streamer.payload.shot_variables; ValueError for a schema_version other than streamer's."""
	version = int(_scalar(variables["schema_version"]))
	if version != SCHEMA_VERSION:
		raise ValueError(f"unsupported schema_version {version} (expected {SCHEMA_VERSION})")
	lecroy, rigol = {}, {}
	for name, samples in variables.items():
		scope, _, rest = name.partition("_")
		if not rest.endswith("_samples"):
			continue
		channel = rest.removesuffix("_samples")
		if scope == "lecroy":
			lecroy[channel.upper()] = (samples, np.asarray(variables[f"lecroy_{channel}_wavedesc"], dtype=np.uint8).tobytes())
		elif scope == "rigol":
			rigol[channel.upper()] = (samples, decode_json(variables[f"rigol_{channel}_metadata_json"]))
	return ArchiveShot(
		schema_version=version,
		shot_index=int(_scalar(variables["shot_index"])),
		host_time=float(_scalar(variables["host_time"])),
		critical_path_s=float(_scalar(variables["critical_path_s"])),
		missing=decode_json(variables["missing_json"]),
		lecroy=lecroy,
		rigol=rigol,
	)
