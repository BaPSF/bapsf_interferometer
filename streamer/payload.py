"""Convert an acquired :class:`interf_raw.RawShot` to transport variables and back."""

import base64
import json
from dataclasses import dataclass

import numpy as np


SCHEMA_VERSION = 1


def _json_default(value):
	if isinstance(value, np.ndarray):
		return value.tolist()
	if isinstance(value, np.generic):
		return value.item()
	if isinstance(value, (bytes, bytearray, memoryview)):
		return {"base64": base64.b64encode(value).decode("ascii")}
	raise TypeError(f"{type(value).__name__} is not JSON serializable")


def encode_json(value):
	"""Encode metadata as a portable UTF-8 byte array."""
	encoded = json.dumps(
		value,
		default=_json_default,
		separators=(",", ":"),
		sort_keys=True,
	).encode("utf-8")
	return np.frombuffer(encoded, dtype=np.uint8).copy()


def decode_json(value):
	"""Decode a metadata variable produced by :func:`encode_json`."""
	return json.loads(np.asarray(value, dtype=np.uint8).tobytes().decode("utf-8"))


def shot_variables(shot, shot_index):
	"""Return the complete named-array payload for one raw acquisition.

	Missing channels are omitted and described by ``missing_json``. This avoids
	writing fabricated samples while allowing the variable set to change as a
	scope drops out or recovers.
	"""
	data = {
		"schema_version": np.array(SCHEMA_VERSION, dtype=np.uint16),
		"shot_index": np.array(shot_index, dtype=np.uint64),
		"host_time": np.array(shot.host_time, dtype=np.float64),
		"critical_path_s": np.array(shot.critical_path_s, dtype=np.float64),
		"missing_json": encode_json(shot.missing),
	}
	for channel, (samples, wavedesc) in sorted(shot.lecroy.items()):
		prefix = f"lecroy_{channel.lower()}"
		data[f"{prefix}_samples"] = np.require(samples, requirements=("C", "A", "O"))
		data[f"{prefix}_wavedesc"] = np.frombuffer(wavedesc, dtype=np.uint8).copy()
	for channel, (samples, metadata) in sorted(shot.rigol.items()):
		prefix = f"rigol_{channel.lower()}"
		data[f"{prefix}_samples"] = np.require(samples, requirements=("C", "A", "O"))
		data[f"{prefix}_metadata_json"] = encode_json(metadata)
	return data


@dataclass
class DecodedShot:
	"""A shot rebuilt by :func:`shot_from_variables`; `lecroy`, `rigol` and `missing` are laid out as in RawShot."""
	schema_version: int
	shot_index: int
	host_time: float
	critical_path_s: float
	missing: dict[str, str]
	lecroy: dict[str, tuple[np.ndarray, bytes]]
	rigol: dict[str, tuple[np.ndarray, dict]]


def _scalar(value):
	# .item(), not int()/float(): adios2 FileReader.read(..., step_selection=...) returns a scalar as
	# shape (1,), and int()/float() of an ndim > 0 array is deprecated in NumPy (it failed the cloud
	# test). .item() takes shape () or (1,) and raises ValueError for anything larger.
	return np.asarray(value).item()


def shot_from_variables(variables):
	"""Inverse of :func:`shot_variables`; channel names come back upper case.

	Raises ValueError for a schema_version this code does not know.
	"""
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
			wavedesc = np.asarray(variables[f"lecroy_{channel}_wavedesc"], dtype=np.uint8).tobytes()
			lecroy[channel.upper()] = (samples, wavedesc)
		elif scope == "rigol":
			rigol[channel.upper()] = (samples, decode_json(variables[f"rigol_{channel}_metadata_json"]))
	return DecodedShot(
		schema_version=version,
		shot_index=int(_scalar(variables["shot_index"])),
		host_time=float(_scalar(variables["host_time"])),
		critical_path_s=float(_scalar(variables["critical_path_s"])),
		missing=decode_json(variables["missing_json"]),
		lecroy=lecroy,
		rigol=rigol,
	)
