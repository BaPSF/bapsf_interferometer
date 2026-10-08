"""Convert an acquired :class:`interf_raw.RawShot` to transport variables."""

import base64
import json

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
