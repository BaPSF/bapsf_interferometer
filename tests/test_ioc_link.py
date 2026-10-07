import queue
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from diag_ioc.link import IocLink, LinkListener, parse_address
from interf_sim.synthetic import make_raw_shot
from streamer.network_access import create_tcp_listener
from streamer.payload import shot_variables

RECEIVE_TIMEOUT_S = 5.0


def _free_port():
	with socket.socket() as s:
		s.bind(("127.0.0.1", 0))
		return s.getsockname()[1]


def _as_is(item, seq):  # IocLink encode for items that already are {name: array}
	return item


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
	return [make_raw_shot(4096, host_time=1_700_000_000.0 + 3 * i, rng=np.random.default_rng(i)) for i in range(count)]


class ParseAddressTests(unittest.TestCase):
	def test_valid_addresses(self):
		self.assertEqual(parse_address("unix:/run/diag-ioc/x/link.sock"), ("unix", "/run/diag-ioc/x/link.sock"))
		self.assertEqual(parse_address("tcp:127.0.0.1:5064"), ("tcp", ("127.0.0.1", 5064)))
		self.assertEqual(parse_address("tcp:host.example:0"), ("tcp", ("host.example", 0)))

	def test_invalid_addresses(self):
		for text in ("", "unix:", "tcp:host", "tcp:host:", "tcp::5000", "tcp:host:x", "tcp:host:70000", "udp:h:1", "/tmp/x"):
			with self.subTest(text=text), self.assertRaises(ValueError):
				parse_address(text)


class RoundTripTests(unittest.TestCase):
	def _round_trip(self, address):
		received = queue.Queue()
		shots = _shots(3)
		with LinkListener(address, received.put).start() as listener, IocLink(listener.address) as link:
			for i, shot in enumerate(shots):
				link.write(shot)
				# One at a time: depth 2 would otherwise drop shots written before the first connect.
				message = received.get(timeout=RECEIVE_TIMEOUT_S)
				expected = shot_variables(shot, i)
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
				IocLink(listener.address, encode=_as_is, retry_interval=0.05) as link:
			link.write({"big": np.zeros(2000, dtype=np.uint8)})
			# A small shot can still be lost on the closed socket before the sender notices, so keep writing.
			message = _receive_while_writing(received, link, {"small": np.zeros(10, dtype=np.uint8)})
			self.assertEqual(set(message), {"small"})


class NoListenerTests(unittest.TestCase):
	def test_writes_never_block_and_keep_only_the_newest(self):
		with IocLink(f"tcp:127.0.0.1:{_free_port()}", encode=_as_is, retry_interval=60.0) as link:
			t0 = time.perf_counter()
			for i in range(100):
				link.write({"i": np.array(i)})
			elapsed = time.perf_counter() - t0
			self.assertLess(elapsed, 0.05)
			# 2 stay queued and the sender may have taken 1 before its failed connect.
			self.assertGreaterEqual(link.dropped, 97)
			self.assertEqual(link.sent, 0)

	def test_reconnects_when_the_listener_starts_late(self):
		port = _free_port()
		received = queue.Queue()
		with IocLink(f"tcp:127.0.0.1:{port}", encode=_as_is, retry_interval=0.1, connect_timeout=0.5) as link:
			link.write({"x": np.arange(3)})
			deadline = time.monotonic() + RECEIVE_TIMEOUT_S
			while link.failed == 0:
				self.assertLess(time.monotonic(), deadline, "first send did not fail")
				time.sleep(0.01)
			with LinkListener(f"tcp:127.0.0.1:{port}", received.put).start():
				message = _receive_while_writing(received, link, {"x": np.arange(3)})
				np.testing.assert_array_equal(message["x"], np.arange(3))
		self.assertGreaterEqual(link.sent, 1)


class CloseTests(unittest.TestCase):
	def test_close_returns_within_its_bound_when_the_listener_stops_reading(self):
		server = create_tcp_listener("127.0.0.1", None, 1)
		accepted = []
		threading.Thread(target=lambda: accepted.append(server.accept()[0]), daemon=True).start()
		try:
			link = IocLink(f"tcp:127.0.0.1:{server.getsockname()[1]}", encode=_as_is, send_timeout=30.0)
			link.write({"big": np.zeros(64 << 20, dtype=np.uint8)})  # far beyond the socket buffers: sendall blocks
			time.sleep(0.5)
			self.assertEqual(link.sent, 0)
			t0 = time.monotonic()
			link.close(timeout=0.5)
			self.assertLess(time.monotonic() - t0, 0.5 + 1.0 + 0.5)  # timeout + shutdown join + slack
		finally:
			for sock in [server, *accepted]:
				sock.close()

	def test_write_after_close_raises(self):
		link = IocLink(f"tcp:127.0.0.1:{_free_port()}")
		link.close()
		with self.assertRaises(RuntimeError):
			link.write(object())


if __name__ == "__main__":
	unittest.main()
