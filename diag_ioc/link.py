"""Shot transport from acquisition to a diag_ioc module: IocLink sends, LinkListener receives.

Latest-wins on purpose: the live path never blocks acquisition and never replays a backlog;
completeness is the ADIOS archive's job. The framing (diag_ioc.framing) has no authentication,
so listen only on a unix socket or an allow-listed TCP address.
"""
import argparse
import logging
import os
import socket
import stat
import threading
import time
from collections import deque

from diag_ioc.framing import receive_message, send_arrays, send_end
from diag_ioc.network import accept_from_allowed_network, create_tcp_listener, ipv4_network
from diag_ioc.outage import Outage

log = logging.getLogger(__name__)

_LISTEN_BACKLOG = 4
_ACCEPT_RETRY_S = 1.0  # bounds the rate of a persistent accept() failure (e.g. EMFILE)
# Per-message cap on announced array bytes, checked before allocating. Today's shot is ~12 MB
# (LeCroy 4 x 1M + Rigol 2 x 1M int16); 1 GiB leaves room for higher-resolution records
# (4 x 50M LeCroy + 2 x 25M Rigol is ~0.5 GB) while refusing a corrupt or hostile size.
MAX_MESSAGE_BYTES = 1 << 30
# Longest unix socket path Linux accepts (sun_path holds 108 bytes with the terminating NUL). A longer
# path would pass parse_address and then fail every bind and connect, so it is rejected here.
UNIX_PATH_MAX_BYTES = 107


def parse_address(text):
	"""("unix", path) or ("tcp", (host, port)) from "unix:/path" or "tcp:host:port"; ValueError otherwise.

	Port 0 is accepted so a listener can bind an ephemeral port (see LinkListener.address).
	"""
	kind, _, rest = text.partition(":")
	if kind == "unix" and rest:
		size = len(os.fsencode(rest))
		if size > UNIX_PATH_MAX_BYTES:
			raise ValueError(f"link address {text!r}: the socket path is {size} bytes, over the {UNIX_PATH_MAX_BYTES}-byte limit")
		return "unix", rest
	if kind == "tcp":
		host, _, port = rest.rpartition(":")
		if host and port.isdigit() and int(port) <= 65535:
			return "tcp", (host, int(port))
	raise ValueError(f"link address {text!r}: expected unix:/path or tcp:host:port")


def address_arg(text):
	"""argparse `type=` for a link address: checked by parse_address, returned unchanged."""
	try:
		parse_address(text)
	except ValueError as e:
		raise argparse.ArgumentTypeError(str(e)) from None
	return text


class IocLink:
	"""Latest-wins shot sender: write() returns at once and never raises for link problems.

	A daemon thread connects lazily, encodes (`encode(item) -> {name: array}`) and sends. Counters:
	`dropped` = replaced in the queue by newer shots (also during an outage), `failed` = taken for
	sending but not delivered, `sent`. The link numbers nothing: a receiver sees lost shots only as
	gaps in an identity the item carries (interf_payload's shot_number).
	"""

	def __init__(self, address, encode, depth=2, retry_interval=5.0, connect_timeout=2.0, send_timeout=10.0):
		self.address = address
		self._kind, self._target = parse_address(address)
		self._encode = encode
		self._depth = depth
		self._retry_interval = retry_interval
		self._connect_timeout = connect_timeout
		self._send_timeout = send_timeout
		self._queue = deque()
		self._cond = threading.Condition()
		self._closing = False
		self._dropped = self._sent = self._failed = 0
		self._sock = None
		self._outage = Outage(log, f"IOC link {address}")  # used by the sender thread only
		self._lost_at_outage_start = 0
		self._thread = threading.Thread(target=self._run, name="ioc-link-sender", daemon=True)
		self._thread.start()

	@property
	def dropped(self):
		return self._dropped

	@property
	def sent(self):
		return self._sent

	@property
	def failed(self):
		return self._failed

	def write(self, item):
		with self._cond:
			if self._closing:
				raise RuntimeError(f"IocLink {self.address} is closed")  # a caller bug; interf_main isolates it
			if len(self._queue) == self._depth:
				self._queue.popleft()
				self._dropped += 1
			self._queue.append(item)
			self._cond.notify()

	def close(self, timeout=None):
		"""Send what is queued and an end message, then stop; returns within `timeout` + 1 s (default send_timeout).

		After the first failed send, the rest of the queue counts as failed, unsent.
		"""
		with self._cond:
			if self._closing:
				return
			self._closing = True
			self._cond.notify_all()
		self._thread.join(self._send_timeout if timeout is None else timeout)
		if self._thread.is_alive():
			# Stuck in sendall to a listener that stopped reading: shutdown makes the send fail.
			sock = self._sock
			if sock is not None:
				try:
					sock.shutdown(socket.SHUT_RDWR)
				except OSError:
					pass
			self._thread.join(1.0)
			if self._thread.is_alive():
				log.warning("IOC link %s sender did not stop; abandoning it (daemon thread)", self.address)

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		self.close()

	def _run(self):
		try:
			while True:
				with self._cond:
					while not self._queue and not self._closing:
						self._cond.wait()
					if not self._queue:
						return
					item = self._queue.popleft()
				if not self._deliver(item):
					with self._cond:
						if self._closing:
							self._failed += len(self._queue)
							self._queue.clear()
							return
						self._cond.wait_for(lambda: self._closing, self._retry_interval)
		finally:
			self._disconnect(graceful=True)

	def _deliver(self, item):
		"""False when the link failed (connection dropped); an encoding bug counts as failed but returns True."""
		try:
			if self._sock is None:
				self._sock = self._connect()
			data = self._encode(item)
			send_arrays(self._sock, data.items())
		except (OSError, EOFError) as e:
			self._failed += 1
			self._disconnect(graceful=False)
			if self._outage.since is None:
				self._lost_at_outage_start = self._dropped + self._failed - 1  # this shot is the first loss
			self._outage.failed(e, f"{self._undelivered()} shots not delivered")
			return False
		except Exception:
			# An encoding bug, not a link problem: the connection stays usable.
			self._failed += 1
			log.exception("IOC link %s: shot not sent (encode failed)", self.address)
			return True
		self._sent += 1
		if self._outage.since is not None:
			self._outage.ended(f"{self._undelivered()} shots not delivered")
		return True

	def _connect(self):
		if self._kind == "unix":
			sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
			try:
				sock.settimeout(self._connect_timeout)
				sock.connect(self._target)
			except BaseException:
				sock.close()
				raise
		else:
			sock = socket.create_connection(self._target, timeout=self._connect_timeout)
			sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
		sock.settimeout(self._send_timeout)
		return sock

	def _disconnect(self, graceful):
		sock, self._sock = self._sock, None
		if sock is None:
			return
		try:
			if graceful:
				send_end(sock)
		except OSError:
			pass
		finally:
			sock.close()

	def _undelivered(self):
		# Shots lost in this outage: failed sends (Outage.count) plus shots replaced in the queue meanwhile.
		return self._dropped + self._failed - self._lost_at_outage_start


class LinkListener:
	"""Accepts IocLink connections and calls on_message(variables) once per shot.

	on_message runs on that connection's reader thread; an exception in it is logged and the
	connection kept. TCP peers outside `allow` (CIDR strings or networks, default 127.0.0.1/32) are closed unread.
	A message over max_message_bytes closes its connection unread.
	"""

	def __init__(self, address, on_message, allow=(), max_message_bytes=MAX_MESSAGE_BYTES):
		self.address = address  # after start(), a tcp port 0 is replaced by the bound port
		self._kind, self._target = parse_address(address)
		self._on_message = on_message
		self._allow = tuple(ipv4_network(a) for a in allow) or (ipv4_network("127.0.0.1/32"),)
		self._max_message_bytes = max_message_bytes
		self._listener = None
		self._connections = set()
		self._lock = threading.Lock()  # also serializes _drops across reader threads
		self._drops = Outage(log, f"IOC link listener {address}")  # a persistently bad sender reconnects every retry_interval
		self._closed = False

	@property
	def connections(self):
		with self._lock:
			return len(self._connections)

	def start(self):
		if self._kind == "unix":
			path = self._target
			try:
				if stat.S_ISSOCK(os.stat(path).st_mode):
					os.unlink(path)  # left by a process that died; a non-socket file makes bind fail instead
			except FileNotFoundError:
				pass
			sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
			try:
				sock.bind(path)
				os.chmod(path, 0o660)  # the acquisition user reaches it through a shared group
				sock.listen(_LISTEN_BACKLOG)
			except BaseException:
				sock.close()
				raise
		else:
			host, port = self._target
			sock = create_tcp_listener(host, port, _LISTEN_BACKLOG)
			self.address = f"tcp:{host}:{sock.getsockname()[1]}"
		self._listener = sock
		threading.Thread(target=self._accept_loop, name="ioc-link-accept", daemon=True).start()
		return self

	def close(self):
		with self._lock:
			if self._closed:
				return
			self._closed = True
			connections = list(self._connections)
		for sock in [self._listener, *connections]:
			if sock is None:
				continue
			try:
				sock.shutdown(socket.SHUT_RDWR)  # wakes a thread blocked in accept()/recv(); close() alone may not
			except OSError:
				pass
			sock.close()
		if self._kind == "unix" and self._listener is not None:
			try:
				os.unlink(self._target)
			except FileNotFoundError:
				pass

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		self.close()

	def _accept_loop(self):
		while True:
			try:
				if self._kind == "unix":
					conn, _ = self._listener.accept()
				else:
					conn, _ = accept_from_allowed_network(self._listener, self._allow)
			except OSError:
				if self._closed:
					return
				log.exception("IOC link listener %s: accept failed", self.address)
				time.sleep(_ACCEPT_RETRY_S)
				continue
			with self._lock:
				if self._closed:
					conn.close()
					return
				self._connections.add(conn)
			threading.Thread(target=self._read_loop, args=(conn,), name="ioc-link-reader", daemon=True).start()

	def _read_loop(self, conn):
		try:
			with conn:
				while (message := receive_message(conn, self._max_message_bytes)) is not None:
					with self._lock:
						self._drops.ended()
					try:
						self._on_message(message)
					except Exception:
						log.exception("IOC link listener %s: on_message failed; shot skipped", self.address)
		except Exception as e:
			# Peer gone, oversized message, or stream desynchronized: drop the connection; the sender reconnects.
			if not self._closed:
				with self._lock:
					self._drops.failed(e, "connection closed")
		finally:
			with self._lock:
				self._connections.discard(conn)
