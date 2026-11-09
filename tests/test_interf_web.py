"""interf_web's limits (docs/ARCHITECTURE.md D18) on loopback, with a stub in place of the p4p monitors, and the page's
session.js in node with fake timers. Server tests patch the limits down so each takes seconds."""
import contextlib
import http.client
import json
import logging
import math
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import interf_web.__main__ as web
from ioc_harness import REPO, free_port, have, wait_for

WEB = have("fastapi", "uvicorn", "websockets")
if WEB:
	import uvicorn
	from websockets.exceptions import ConnectionClosed
	from websockets.sync.client import connect

HOST = "127.0.0.1"
SESSION_JS = REPO / "interf_web" / "static" / "session.js"
PAUSED = "Paused - reload to resume"
DISCONNECTED = "Disconnected - reload the page to reconnect"


@contextlib.contextmanager
def running(**limits):
	"""interf_web on a free loopback port, with `limits` (module constants) patched and the ShotFeed in place of p4p;
	yields (port, feed)."""
	feeds = []

	def monitor(prefix, feed):
		feeds.append(feed)
		return lambda: None

	with contextlib.ExitStack() as stack:
		if limits:  # patch.multiple refuses no keywords
			stack.enter_context(mock.patch.multiple(web, **limits))
		state = stack.enter_context(tempfile.TemporaryDirectory())
		port = free_port()
		server = uvicorn.Server(uvicorn.Config(web.create_app("TEST", Path(state), monitor=monitor),
		                                       **web.server_settings(HOST, port)))
		thread = threading.Thread(target=server.run, name="interf_web test server", daemon=True)
		thread.start()
		try:
			wait_for(lambda: server.started, 15, "the server to start")
			yield port, feeds[0]
		finally:
			server.should_exit = True
			thread.join(20)
			if thread.is_alive():
				raise AssertionError("the server did not stop within 20 s")


def request(port, method="GET", path="/"):
	"""The status of one request on a new connection."""
	connection = http.client.HTTPConnection(HOST, port, timeout=10)
	try:
		connection.request(method, path)
		return connection.getresponse().status
	finally:
		connection.close()


def viewers(feed):
	return feed.hub.viewers.total()


def publish(feed, number, size):
	"""Hand a shot with a `size`-byte payload to the server's event loop, as the p4p callback does."""
	shot = web.WebShot(20261106, number, time.time(), (math.nan,) * len(web.PORTS), bytes(size))
	feed.loop.call_soon_threadsafe(feed.deliver, shot)


def closed_by_server(sock):
	"""True once the server has closed `sock`; waits up to the socket's timeout."""
	try:
		return sock.recv(1) == b""
	except ConnectionResetError:
		return True


def raw_websocket(port, receive_buffer=None):
	"""A /ws socket opened by hand, which reads nothing after the handshake."""
	sock = socket.socket()
	if receive_buffer:
		sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
	sock.settimeout(10)
	sock.connect((HOST, port))
	sock.sendall(b"GET /ws HTTP/1.1\r\nHost: test\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
	             b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
	reply = b""
	while b"\r\n\r\n" not in reply:
		chunk = sock.recv(1)
		if not chunk:
			raise AssertionError(f"handshake closed: {reply!r}")
		reply += chunk
	assert reply.startswith(b"HTTP/1.1 101"), reply
	return sock


class DecidedValuesTests(unittest.TestCase):
	"""The numbers of D18 (J, 2026-11-06), and the units' accounts and the page's limits and sandbox (D10, D18)."""

	def test_limits(self):
		self.assertEqual((web.MAX_VIEWERS, web.MAX_VIEWERS_PER_ADDRESS, web.TRY_AGAIN_LATER), (20, 3, 1013))
		self.assertEqual((web.MAX_CONNECTIONS, web.KEEP_ALIVE_S, web.SEND_TIMEOUT_S), (80, 30, 30))
		self.assertEqual((web.WS_MAX_SIZE, web.PING_INTERVAL_S, web.PING_TIMEOUT_S), (4096, 30, 60))
		self.assertEqual((web.TREND_RATE_PER_S, web.TREND_BURST), (2, 5))

	@unittest.skipUnless(WEB, "needs the web extra")
	def test_server_settings(self):
		"""The settings no server test below exercises."""
		settings = web.server_settings(HOST, 1)
		self.assertEqual(settings["ws_max_size"], web.WS_MAX_SIZE)
		self.assertEqual((settings["ws_ping_interval"], settings["ws_ping_timeout"]), (web.PING_INTERVAL_S, web.PING_TIMEOUT_S))
		self.assertFalse(settings["access_log"])
		self.assertEqual((settings["proxy_headers"], settings["forwarded_allow_ips"]), (True, "127.0.0.1"))

	def test_unit_accounts(self):
		"""D10: acquisition alone as lapd (the NERSC key), reaching the link socket through the diag-ioc group; the
		others as diag-ioc and off the scope network, which only acquisition talks to."""
		def unit(name):
			return (REPO / "deploy" / "systemd" / f"{name}.service").read_text(encoding="utf-8").splitlines()

		for name in ("diag-ioc@", "interf-store", "interf-web"):
			lines = unit(name)
			for line in ("User=diag-ioc", "Group=diag-ioc", "IPAddressDeny=10.106.34.0/24"):
				self.assertIn(line, lines, name)
		self.assertIn("RuntimeDirectoryMode=0750", unit("diag-ioc@"))
		acquire = unit("interf-acquire")
		for line in ("User=lapd", "Group=lapd", "SupplementaryGroups=diag-ioc"):
			self.assertIn(line, acquire)
		self.assertEqual([line for line in acquire if line.startswith("IPAddressDeny=")], [])
		for name in ("interf-store", "interf-web"):  # the lab's gateway republishes our PVs on the subnet (D3)
			lines = unit(name)
			self.assertIn("Environment=EPICS_PVA_ADDR_LIST=127.0.0.1", lines, name)
			self.assertIn("Environment=EPICS_PVA_AUTO_ADDR_LIST=NO", lines, name)

	def test_web_unit_sandbox(self):
		unit = (REPO / "deploy" / "systemd" / "interf-web.service").read_text(encoding="utf-8").splitlines()
		for line in ("CPUQuota=10%", "MemoryMax=1G", "TasksMax=128", "LimitNOFILE=256",
		             "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes",
		             "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectKernelLogs=yes",
		             "ProtectControlGroups=yes", "ProtectClock=yes", "ProtectHostname=yes", "ProtectProc=invisible",
		             "ProcSubset=pid", "RestrictSUIDSGID=yes", "LockPersonality=yes", "RestrictNamespaces=yes",
		             "RestrictRealtime=yes", "RemoveIPC=yes", "CapabilityBoundingSet=", "AmbientCapabilities=",
		             "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK", "SystemCallArchitectures=native",
		             "SystemCallFilter=@system-service", "SystemCallErrorNumber=EPERM"):
			self.assertIn(line, unit)
		hidden = next(line for line in unit if line.startswith("InaccessiblePaths=")).split("=", 1)[1].split()
		self.assertEqual(sorted(hidden), sorted(["-/etc/interferometer/acquire.env", "-/etc/diag-ioc",
		                                         "-/var/lib/interferometer", "-/var/lib/interf-acquire",
		                                         "-/var/log/interferometer"]))
		# 127.0.0.1 is the code's default; nginx is the way in from the lab network (D18).
		self.assertEqual([line for line in unit if "INTERF_WEB_HOST" in line and not line.startswith("#")], [])


class ThrottledLogTests(unittest.TestCase):
	def test_passes_errors_and_a_minute_s_quota(self):
		now = [0.0]
		throttle = web.ThrottledLog(3, clock=lambda: now[0])
		info = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "accepted", None, None)
		error = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "failed", None, None)
		self.assertEqual([throttle.filter(info) for _ in range(5)], [True, True, True, False, False])
		self.assertTrue(throttle.filter(error))
		now[0] = 61.0
		with self.assertLogs(web.log, logging.WARNING) as logged:
			self.assertTrue(throttle.filter(info))
		self.assertIn("2 lines of the uvicorn.error log dropped", logged.output[0])


@unittest.skipUnless(WEB, "needs the web extra (fastapi, uvicorn, websockets)")
class ServerLimitsTests(unittest.TestCase):

	def test_viewer_cap(self):
		with running(MAX_VIEWERS=2, MAX_VIEWERS_PER_ADDRESS=10) as (port, feed):
			url = f"ws://{HOST}:{port}/ws"
			with connect(url, open_timeout=10) as first, connect(url, open_timeout=10) as second:
				first.recv(timeout=10)
				second.recv(timeout=10)
				with connect(url, open_timeout=10) as third, self.assertRaises(ConnectionClosed) as refused:
					third.recv(timeout=10)
				self.assertEqual(refused.exception.rcvd.code, 1013)
				self.assertIn("2 viewers", refused.exception.rcvd.reason)
			wait_for(lambda: viewers(feed) == 0, 10, "both viewers to leave")
			with connect(url, open_timeout=10) as again:
				json.loads(again.recv(timeout=10))  # a status frame: the slot is free again

	def test_per_address_cap(self):
		with running(MAX_VIEWERS=20, MAX_VIEWERS_PER_ADDRESS=3) as (port, feed):
			url = f"ws://{HOST}:{port}/ws"
			with contextlib.ExitStack() as stack:
				for _ in range(3):
					stack.enter_context(connect(url, open_timeout=10)).recv(timeout=10)
				with connect(url, open_timeout=10) as fourth, self.assertRaises(ConnectionClosed) as refused:
					fourth.recv(timeout=10)
				self.assertEqual(refused.exception.rcvd.code, 1013)
				self.assertEqual(viewers(feed), 3)

	def test_refused_socket_is_logged(self):
		"""uvicorn's own line for it says only "[accepted]"."""
		with running(MAX_VIEWERS=1, MAX_VIEWERS_PER_ADDRESS=10) as (port, feed):
			url = f"ws://{HOST}:{port}/ws"
			with connect(url, open_timeout=10) as first:
				first.recv(timeout=10)
				with self.assertLogs(web._refused_log, logging.INFO) as logged:
					with connect(url, open_timeout=10) as second, self.assertRaises(ConnectionClosed):
						second.recv(timeout=10)  # the log line comes before the close
		self.assertEqual(len(logged.output), 1)
		self.assertIn(f"/ws from {HOST} refused: the page already has 1 viewers", logged.output[0])

	def test_http_beyond_max_connections_gets_503(self):
		with running(MAX_CONNECTIONS=4) as (port, feed):
			idle = [socket.create_connection((HOST, port), timeout=10) for _ in range(4)]
			try:
				self.assertEqual(request(port), 503)
			finally:
				for sock in idle:
					sock.close()
			wait_for(lambda: request(port) == 200, 10, "a request to succeed once the idle connections closed")

	def test_incomplete_request_is_closed(self):
		"""Idle from the start, headers never finished, and headers dribbled in one byte at a time past the limit."""
		with running(KEEP_ALIVE_S=0.5) as (port, feed):
			for sent in (b"", b"GET / HTTP/1.1\r\nHost: test\r\n"):
				with socket.create_connection((HOST, port), timeout=10) as sock:
					sock.sendall(sent)
					start = time.monotonic()
					self.assertTrue(closed_by_server(sock), sent)
					self.assertLess(time.monotonic() - start, 5, sent)
			with socket.create_connection((HOST, port), timeout=10) as sock:
				with contextlib.suppress(OSError):  # the server may close it between two bytes
					for byte in b"GET / HTTP/1.1\r\nHost: test\r\n":
						sock.sendall(bytes([byte]))
						time.sleep(0.1)  # 30 bytes: 3 s, well past KEEP_ALIVE_S
				self.assertTrue(closed_by_server(sock))

	def test_send_timeout_frees_the_viewer(self):
		with running(SEND_TIMEOUT_S=1.0) as (port, feed):
			sock = raw_websocket(port, receive_buffer=4096)
			try:
				wait_for(lambda: viewers(feed) == 1, 10, "the viewer to register")
				numbers = iter(range(1, 10**6))

				def publish_and_check():
					publish(feed, next(numbers), 1 << 20)  # until the socket's buffers are full and a send stalls
					return viewers(feed) == 0

				wait_for(publish_and_check, 30, "the stalled viewer to be dropped")
			finally:
				sock.close()

	def test_trend_rate_limit(self):
		with running(TREND_RATE_PER_S=0.01, TREND_BURST=2) as (port, feed):
			path = "/api/trend?start=0&end=1&points=10"
			self.assertEqual([request(port, path=path) for _ in range(3)], [200, 200, 429])

	def test_read_only(self):
		with running() as (port, feed):
			for path in ("/", "/api/trend", "/static/index.html"):
				for method in ("POST", "PUT", "DELETE"):
					self.assertEqual(request(port, method, path), 405, (method, path))
			for path in ("/docs", "/redoc", "/openapi.json"):
				self.assertEqual(request(port, path=path), 404, path)


# Fake timers and a fake socket for session.js; each test appends its steps and prints one JSON line.
_SESSION_HARNESS = r"""
const S = require(process.env.SESSION_JS);
let now = 0, nextId = 1;
const timers = [];
const fake = {
  setTimeout(f, ms) { const id = nextId++; timers.push({id, at: now + ms, f}); return id; },
  setInterval(f, ms) { const id = nextId++; timers.push({id, at: now + ms, f, every: ms}); return id; },
  clearTimeout(id) { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); },
  clearInterval(id) { fake.clearTimeout(id); },
};
function advance(ms) {
  const end = now + ms;
  for (;;) {
    let next = null;
    for (const t of timers) if (next === null || t.at < next.at) next = t;
    if (next === null || next.at > end) break;
    now = next.at;
    if (next.every) next.at += next.every; else timers.splice(timers.indexOf(next), 1);
    next.f();
  }
  now = end;
}
const sockets = [];
class FakeSocket {
  constructor(url) { this.url = url; this.closed = false; sockets.push(this); }
  close() { this.closedBy("page", {code: 1000, reason: ""}); }
  closedBy(who, event) { if (this.closed) return; this.closed = who; this.onclose(event); }
}
const listeners = new Set();
const document = {hidden: false, addEventListener(type, f) { listeners.add(f); },
                  removeEventListener(type, f) { listeners.delete(f); }};
function setHidden(hidden) { document.hidden = hidden; for (const f of [...listeners]) f(); }
const closes = [];
S.openSession({url: "ws://test/ws", WebSocket: FakeSocket, document, timers: fake, now: () => now,
               onShot() {}, onStatus() {}, onClosed: (note, reason) => closes.push([note, reason])});
const socket = sockets[0];
const feedFrames = () => fake.setInterval(() => { if (!socket.closed) socket.onmessage({data: "{}"}); }, 1000);
const report = extra => console.log(JSON.stringify({sockets: sockets.length, closed: socket.closed, closes, ...extra}));
const HOUR = 3600 * 1000;
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class PageSessionTests(unittest.TestCase):
	"""session.js: one socket for the life of the page, the hidden-tab pause, and no timeout for a visible tab."""

	def run_session(self, steps):
		completed = subprocess.run(["node", "-e", _SESSION_HARNESS + steps], env={**os.environ, "SESSION_JS": str(SESSION_JS)},
		                           capture_output=True, text=True, timeout=120, check=True)
		return json.loads(completed.stdout.strip().splitlines()[-1])

	def test_no_reconnect_after_the_server_closes(self):
		for event in ('{code: 1006, reason: ""}', '{code: 1013, reason: "the page already has 20 viewers; try again later"}'):
			result = self.run_session(f"feedFrames(); socket.closedBy('server', {event}); advance(24 * HOUR); report();")
			self.assertEqual(result["sockets"], 1, event)
			self.assertEqual(result["closes"][0][0], DISCONNECTED, event)
			self.assertEqual(len(result["closes"]), 1, event)
		self.assertIn("20 viewers", result["closes"][0][1])

	def test_no_reconnect_after_a_dead_link(self):
		result = self.run_session("advance(S.FRAME_TIMEOUT_MS + 2000); advance(24 * HOUR); report();")
		self.assertEqual((result["sockets"], result["closed"], result["closes"]), (1, "page", [[DISCONNECTED, ""]]))

	def test_hidden_tab_pauses_after_the_limit(self):
		result = self.run_session("feedFrames(); setHidden(true); advance(S.HIDDEN_PAUSE_MS - 1000);"
		                          "const early = socket.closed; advance(2000); report({early, limit: S.HIDDEN_PAUSE_MS});")
		self.assertEqual(result["limit"], 15 * 60 * 1000)
		self.assertFalse(result["early"])
		self.assertEqual((result["sockets"], result["closed"], result["closes"]), (1, "page", [[PAUSED, ""]]))

	def test_hidden_timer_cancelled_on_return(self):
		result = self.run_session("feedFrames(); setHidden(true); advance(S.HIDDEN_PAUSE_MS / 2); setHidden(false);"
		                          "advance(2 * S.HIDDEN_PAUSE_MS); report();")
		self.assertEqual((result["closed"], result["closes"]), (False, []))

	def test_visible_tab_stays_for_days(self):
		result = self.run_session("feedFrames(); advance(3 * 24 * HOUR); report();")
		self.assertEqual((result["sockets"], result["closed"], result["closes"]), (1, False, []))


if __name__ == "__main__":
	unittest.main()
