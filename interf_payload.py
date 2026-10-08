# -*- coding: utf-8 -*-
"""Live-link message of one interf_raw.RawShot: encode() for IocLink, decode() for the IOC module.

Copied from streamer/payload.py, not imported: streamer belongs to another group, and the live path must not
depend on it (docs/ARCHITECTURE.md D6). The ADIOS archive keeps streamer's own schema 1.
"""
import base64
import json
from dataclasses import dataclass

import numpy as np

SCHEMA_VERSION = 2  # 1 is streamer's archive layout (shot_index, no shot identity)


def _json_default(value):
	if isinstance(value, np.ndarray):
		return value.tolist()
	if isinstance(value, np.generic):
		return value.item()
	if isinstance(value, (bytes, bytearray, memoryview)):
		return {"base64": base64.b64encode(value).decode("ascii")}
	raise TypeError(f"{type(value).__name__} is not JSON serializable")


def encode_json(value):
	"""UTF-8 JSON as a uint8 array."""
	encoded = json.dumps(value, default=_json_default, separators=(",", ":"), sort_keys=True).encode("utf-8")
	return np.frombuffer(encoded, dtype=np.uint8).copy()


def decode_json(value):
	return json.loads(np.asarray(value, dtype=np.uint8).tobytes().decode("utf-8"))


def _text(value):
	return np.frombuffer(value.encode("utf-8"), dtype=np.uint8).copy()


def encode(shot):
	"""{name: array} for one RawShot: the envelope, then lecroy_<ch>_* and rigol_<ch>_* per channel read.

	ValueError when interf_main has not assigned the shot identity (shot_date ... time_source). Missing channels
	are omitted and described by missing_json, never filled with fabricated samples.
	"""
	identity = (shot.shot_date, shot.shot_number, shot.shot_time, shot.time_source)
	if None in identity:
		raise ValueError("shot has no identity (shot_date, shot_number, shot_time, time_source); interf_main assigns it")
	data = {
		"schema_version": np.array(SCHEMA_VERSION, dtype=np.uint16),
		"shot_date": np.array(shot.shot_date, dtype=np.uint32),
		"shot_number": np.array(shot.shot_number, dtype=np.uint64),
		"shot_time": np.array(shot.shot_time, dtype=np.float64),
		"time_source": _text(shot.time_source),
		"host_time": np.array(shot.host_time, dtype=np.float64),
		"critical_path_s": np.array(shot.critical_path_s, dtype=np.float64),
		"missing_json": encode_json(shot.missing),
	}
	# Samples go in as they are, not copied (streamer's np.require(..., "O") copies every shot): the send is
	# synchronous while the shot is alive, and diag_ioc.framing makes non-contiguous arrays contiguous itself.
	for channel, (samples, wavedesc) in sorted(shot.lecroy.items()):
		prefix = f"lecroy_{channel.lower()}"
		data[f"{prefix}_samples"] = samples
		data[f"{prefix}_wavedesc"] = np.frombuffer(wavedesc, dtype=np.uint8)
	for channel, (samples, metadata) in sorted(shot.rigol.items()):
		prefix = f"rigol_{channel.lower()}"
		data[f"{prefix}_samples"] = samples
		data[f"{prefix}_metadata_json"] = encode_json(metadata)
	return data


@dataclass
class DecodedShot:
	"""A shot rebuilt by decode(); every field but schema_version is as in interf_raw.RawShot, channels upper case."""
	schema_version: int
	shot_date: int
	shot_number: int
	shot_time: float
	time_source: str
	host_time: float
	critical_path_s: float
	missing: dict[str, str]
	lecroy: dict[str, tuple[np.ndarray, bytes]]
	rigol: dict[str, tuple[np.ndarray, dict]]


def _scalar(value):
	# .item() takes shape () or (1,) and raises ValueError for anything larger.
	return np.asarray(value).item()


def decode(variables):
	"""Inverse of encode(); ValueError for a schema_version other than SCHEMA_VERSION."""
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
	return DecodedShot(
		schema_version=version,
		shot_date=int(_scalar(variables["shot_date"])),
		shot_number=int(_scalar(variables["shot_number"])),
		shot_time=float(_scalar(variables["shot_time"])),
		time_source=np.asarray(variables["time_source"], dtype=np.uint8).tobytes().decode("utf-8"),
		host_time=float(_scalar(variables["host_time"])),
		critical_path_s=float(_scalar(variables["critical_path_s"])),
		missing=decode_json(variables["missing_json"]),
		lecroy=lecroy,
		rigol=rigol,
	)
