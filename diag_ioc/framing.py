"""Message framing of the shot link: a length-prefixed JSON header, then each array's raw bytes.

Copied from streamer/socket_protocol.py (protocol 4) without compression or authentication, so diag_ioc never
imports streamer, which another group maintains (docs/ARCHITECTURE.md D6).
"""
import json
import math
import struct

import numpy as np

_HEADER_LENGTH = struct.Struct("!Q")
_MAX_HEADER_BYTES = 1024 * 1024


def _send_header(sock, header):
	encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
	sock.sendall(_HEADER_LENGTH.pack(len(encoded)) + encoded)  # one call: with TCP_NODELAY each call is a packet


def _contiguous(value):
	array = np.asarray(value)
	# Not np.ascontiguousarray unconditionally: it returns ndim >= 1, so a scalar would arrive with shape (1,).
	return array if array.flags.c_contiguous else np.ascontiguousarray(array)


def send_arrays(sock, variables):
	"""Send one data message; `variables` is an iterable of (name, array-like)."""
	arrays = [(name, _contiguous(value)) for name, value in variables]
	descriptions = [{"name": name, "dtype": array.dtype.str, "shape": list(array.shape), "nbytes": array.nbytes}
	                for name, array in arrays]
	_send_header(sock, {"type": "data", "variables": descriptions})
	for _, array in arrays:
		sock.sendall(memoryview(array).cast("B"))


def send_end(sock):
	_send_header(sock, {"type": "end"})


def _recv_into(sock, view):
	received = 0
	while received < len(view):
		count = sock.recv_into(view[received:])
		if count == 0:
			raise EOFError("socket closed in the middle of a message")
		received += count


def _recv_exact(sock, size):
	data = bytearray(size)
	_recv_into(sock, memoryview(data))
	return data


def _receive_header(sock):
	header_size = _HEADER_LENGTH.unpack(_recv_exact(sock, _HEADER_LENGTH.size))[0]
	if header_size > _MAX_HEADER_BYTES:
		raise ValueError(f"message header is too large: {header_size} bytes")
	return json.loads(_recv_exact(sock, header_size).decode("utf-8"))


def receive_message(sock, max_bytes=None):
	"""Next data message as {name: array}, or None for "end".

	With max_bytes, a message announcing more array bytes in total raises ValueError before any payload is read
	or allocated; the stream is then mid-message, so the caller must close it.
	"""
	header = _receive_header(sock)
	if header.get("type") == "end":
		return None
	if header.get("type") != "data":
		raise ValueError(f"unknown message type: {header.get('type')}")
	descriptions = header["variables"]
	if max_bytes is not None:
		announced = sum(int(d["nbytes"]) for d in descriptions)
		if announced > max_bytes:
			raise ValueError(f"message announces {announced} bytes, over the {max_bytes}-byte limit")
	variables = {}
	for description in descriptions:
		name = description["name"]
		if "operation" in description or description.get("payload_nbytes", description["nbytes"]) != description["nbytes"]:
			raise ValueError(f"{name}: encoded (compressed) payloads are not supported on this link")
		dtype = np.dtype(description["dtype"])
		shape = tuple(description["shape"])
		expected = math.prod(shape) * dtype.itemsize
		if description["nbytes"] != expected:
			raise ValueError(f"invalid size for {name}: {description['nbytes']} != {expected}")
		raw = np.empty(expected, dtype=np.uint8)  # not bytearray: that zero-fills a buffer recv_into overwrites anyway
		_recv_into(sock, memoryview(raw))
		variables[name] = raw.view(dtype).reshape(shape)
	return variables
