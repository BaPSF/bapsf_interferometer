import json
import os
import queue
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

import interf_payload
from diag_ioc.framing import receive_message, send_arrays, send_end
from diag_ioc.link import UNIX_PATH_MAX_BYTES, IocLink, LinkListener, parse_address
from diag_ioc.network import create_tcp_listener
from interf_sim.synthetic import make_raw_shot
from ioc_harness import as_is, free_port, wait_for

RECEIVE_TIMEOUT_S = 5.0


def _receive_while_writing(received, link, item):
	"""First message received while re-writing `item` every 50 ms; fails after RECEIVE_TIMEOUT_S."""
	deadline = time.monotonic() + RECEIVE_TIMEOUT_S
	while time.monotonic() < deadline:
		link.write(item)
		try:
			return received.get(timeout=0.05)
		except queue.Empty:
			pass
	raise AssertionError(f"nothing received within {RECEIVE_TIMEOUT_S} s")


def _shots(count):
	return [make_raw_shot(4096, host_time=1_700_000_000.0 + 3 * i, rng=np.random.default_rng(i), shot_number=i)
	        for i in range(count)]


class FramingTests(unittest.TestCase):
	def _pair(self):
		left, right = socket.socketpair()
		self.addCleanup(left.close)
		self.addCleanup(right.close)
		return left, right

	def test_arrays_round_trip_with_dtype_and_shape(self):
		left, right = self._pair()
		sent = {"scalar": np.array(7, dtype=np.uint64), "time": np.array(12.5), "codes": np.arange(6, dtype=np.int16).reshape(2, 3),
		        "strided": np.arange(10.0)[::2], "empty": np.empty(0)}
		send_arrays(left, sent.items())
		send_end(left)
		received = receive_message(right)
		self.assertEqual(set(received), set(sent))
		for name, array in sent.items():
			with self.subTest(name):
				self.assertEqual((received[name].dtype, received[name].shape), (array.dtype, array.shape))
				np.testing.assert_array_equal(received[name], array)
		self.assertIsNone(receive_message(right))  # "end"

	def test_message_over_max_bytes_is_refused_before_reading_payload(self):
		left, right = self._pair()
		send_arrays(left, [("a", np.zeros(60, dtype=np.uint8)), ("b", np.zeros(50, dtype=np.uint8))])
		with self.assertRaisesRegex(ValueError, "over the 100-byte limit"):
			receive_message(right, max_bytes=100)

	def test_compressed_payload_is_refused(self):
		# A streamer sender with compression on announces an "operation"; this link carries raw arrays only.
		left, right = self._pair()
		header = json.dumps({"type": "data", "variables": [{"name": "a", "dtype": "|u1", "shape": [4], "nbytes": 4,
		                                                    "payload_nbytes": 2, "operation": {"name": "blosc2"}}]}).encode()
		left.sendall(struct.pack("!Q", len(header)) + header + b"\0\0")
		with self.assertRaisesRegex(ValueError, "not supported"):
			receive_message(right)


class ParseAddressTests(unittest.TestCase):
	def test_valid_addresses(self):
		self.assertEqual(parse_address("unix:/run/diag-ioc/x/link.sock"), ("unix", "/run/diag-ioc/x/link.sock"))
		self.assertEqual(parse_address("tcp:127.0.0.1:5064"), ("tcp", ("127.0.0.1", 5064)))
		self.assertEqual(parse_address("tcp:host.example:0"), ("tcp", ("host.example", 0)))

	def test_invalid_addresses(self):
		for text in ("", "unix:", "tcp:host", "tcp:host:", "tcp::5000", "tcp:host:x", "tcp:host:70000", "udp:h:1", "/tmp/x"):
			with self.subTest(text=text), self.assertRaises(ValueError):
				parse_address(text)

	def test_unix_path_over_the_kernel_limit_is_rejected(self):
		at_limit = "/tmp/" + "x" * (UNIX_PATH_MAX_BYTES - 5)
		self.assertEqual(parse_address("unix:" + at_limit), ("unix", at_limit))
		with self.assertRaises(ValueError):
			parse_address("unix:" + at_limit + "x")
		with self.assertRaises(ValueError):  # interf_main and interf_sim fail at startup, not on every send
			IocLink("unix:" + at_limit + "x", encode=as_is)

	@unittest.skipUnless(sys.platform.startswith("linux"), "the 107-byte limit is Linux's (macOS allows 103)")
	def test_a_socket_path_at_the_limit_binds(self):
		with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
			name = UNIX_PATH_MAX_BYTES - len(os.fsencode(tmp)) - 1
			if name < 1:
				self.skipTest(f"temporary directory path too long: {tmp}")
			path = f"{tmp}/{'s' * name}"
			with LinkListener(f"unix:{path}", lambda variables: None).start():
				self.assertTrue(Path(path).is_socket())


class RoundTripTests(unittest.TestCase):
	def _round_trip(self, address):
		received = queue.Queue()
		shots = _shots(3)
		with LinkListener(address, received.put).start() as listener, IocLink(listener.address, interf_payload.encode) as link:
			for shot in shots:
				link.write(shot)
				# One at a time: depth 2 would otherwise drop shots written before the first connect.
				message = received.get(timeout=RECEIVE_TIMEOUT_S)
				expected = interf_payload.encode(shot)
				self.assertEqual(set(message), set(expected))
				for name, array in expected.items():
					np.testing.assert_array_equal(message[name], array)
			self.assertEqual(listener.connections, 1)
		self.assertEqual((link.sent, link.dropped, link.failed), (3, 0, 0))

	def test_tcp_round_trip(self):
		self._round_trip("tcp:127.0.0.1:0")

	@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "no AF_UNIX on this platform")
	def test_unix_round_trip_and_socket_removed_on_close(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "link.sock"
			self._round_trip(f"unix:{path}")
			self.assertFalse(path.exists())

	@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "no AF_UNIX on this platform")
	def test_stale_unix_socket_is_replaced(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "link.sock"
			stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
			stale.bind(str(path))
			stale.close()  # leaves the path behind, as a crashed listener does
			with LinkListener(f"unix:{path}", lambda message: None).start():
				self.assertTrue(path.exists())


class SizeCapTests(unittest.TestCase):
	def test_oversized_message_drops_the_connection_and_later_shots_arrive(self):
		received = queue.Queue()
		with LinkListener("tcp:127.0.0.1:0", received.put, max_message_bytes=1000).start() as listener, \
				IocLink(listener.address, encode=as_is, retry_interval=0.05) as link:
			link.write({"big": np.zeros(2000, dtype=np.uint8)})
			# A small shot can still be lost on the closed socket before the sender notices, so keep writing.
			message = _receive_while_writing(received, link, {"small": np.zeros(10, dtype=np.uint8)})
			self.assertEqual(set(message), {"small"})


class NoListenerTests(unittest.TestCase):
	def test_writes_never_block_and_keep_only_the_newest(self):
		with IocLink(f"tcp:127.0.0.1:{free_port()}", encode=as_is, retry_interval=60.0) as link:
			t0 = time.perf_counter()
			for i in range(100):
				link.write({"i": np.array(i)})
			elapsed = time.perf_counter() - t0
			self.assertLess(elapsed, 0.05)
			# 2 stay queued and the sender may have taken 1 before its failed connect.
			self.assertGreaterEqual(link.dropped, 97)
			self.assertEqual(link.sent, 0)

	def test_reconnects_when_the_listener_starts_late(self):
		port = free_port()
		received = queue.Queue()
		with IocLink(f"tcp:127.0.0.1:{port}", encode=as_is, retry_interval=0.1, connect_timeout=0.5) as link:
			link.write({"x": np.arange(3)})
			wait_for(lambda: link.failed, RECEIVE_TIMEOUT_S, "the first send to fail")
			with LinkListener(f"tcp:127.0.0.1:{port}", received.put).start():
				message = _receive_while_writing(received, link, {"x": np.arange(3)})
				np.testing.assert_array_equal(message["x"], np.arange(3))
		self.assertGreaterEqual(link.sent, 1)


class CloseTests(unittest.TestCase):
	def test_close_returns_within_its_bound_when_the_listener_stops_reading(self):
		server = create_tcp_listener("127.0.0.1", 0, 1)
		accepted = []
		threading.Thread(target=lambda: accepted.append(server.accept()[0]), daemon=True).start()
		try:
			link = IocLink(f"tcp:127.0.0.1:{server.getsockname()[1]}", encode=as_is, send_timeout=30.0)
			link.write({"big": np.zeros(64 << 20, dtype=np.uint8)})  # far beyond the socket buffers: sendall blocks
			time.sleep(0.5)
			if link.sent:  # Windows buffers all 64 MB on loopback, so no send is stuck for close() to unblock
				link.close(timeout=0.5)
				self.skipTest("the OS buffered the whole message, so a stuck send cannot be set up here")
			t0 = time.monotonic()
			link.close(timeout=0.5)
			self.assertLess(time.monotonic() - t0, 0.5 + 1.0 + 0.5)  # timeout + shutdown join + slack
		finally:
			for sock in [server, *accepted]:
				sock.close()

	def test_write_after_close_raises(self):
		link = IocLink(f"tcp:127.0.0.1:{free_port()}", encode=as_is)
		link.close()
		with self.assertRaises(RuntimeError):
			link.write(object())


if __name__ == "__main__":
	unittest.main()
