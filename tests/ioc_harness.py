"""Shared test support: link helpers, and diag_ioc in a subprocess (C4-C7) with isolated EPICS and a CA client.

IOC data is read back over CA (pyepics), which also works where PVA cannot (no kernel IPv6). Importing this
module needs neither softioc nor pyepics.
"""
import functools
import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from diag_ioc.host import READY_MESSAGE

REPO = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent


def as_is(item):
	"""IocLink encode for items that already are {name: array}."""
	return item


def have(*modules):
	return all(importlib.util.find_spec(name) is not None for name in modules)


def free_port(kind=socket.SOCK_STREAM):
	with socket.socket(socket.AF_INET, kind) as s:
		s.bind(("127.0.0.1", 0))
		return s.getsockname()[1]


@functools.cache
def isolated_epics_env():
	"""EPICS_* for the IOC subprocess and this process's client: loopback only, private ports.

	Cached: libca reads them once per process, so every IOC one test process starts uses the same ports.
	"""
	return {
		"EPICS_CA_SERVER_PORT": str(free_port()),
		"EPICS_CA_REPEATER_PORT": str(free_port(socket.SOCK_DGRAM)),
		"EPICS_CA_ADDR_LIST": "127.0.0.1",
		"EPICS_CA_AUTO_ADDR_LIST": "NO",
		"EPICS_CAS_INTF_ADDR_LIST": "127.0.0.1",
		"EPICS_CA_MAX_ARRAY_BYTES": str(1 << 24),
		"EPICS_PVA_SERVER_PORT": str(free_port()),
		"EPICS_PVA_BROADCAST_PORT": str(free_port(socket.SOCK_DGRAM)),
		"EPICS_PVA_ADDR_LIST": "127.0.0.1",
		"EPICS_PVA_AUTO_ADDR_LIST": "NO",
		"EPICS_PVAS_INTF_ADDR_LIST": "127.0.0.1",
	}


def epics_client():
	"""pyepics, imported only after this process's environment is set to isolated_epics_env()."""
	os.environ.update(isolated_epics_env())
	import epics
	return epics


def read_pv(name, as_string=False, ioc=None):
	"""{value, severity, timestamp, ...} of `name` read fresh over CA (use_monitor=False: never a cached monitor value).

	AssertionError on no reply, with `ioc`'s output (an IocProcess) when given.
	"""
	pv = epics_client().get_pv(name, form="time", connect=True, timeout=5)
	data = pv.get_with_metadata(as_string=as_string, use_monitor=False, form="time", timeout=5)
	if data is None:
		raise AssertionError(f"{name}: no reply" + (f"\n{ioc.log()}" if ioc is not None else ""))
	return data


def monitor_all(names, callback, timeout):
	"""pyepics monitors on `names`, returned once every one has delivered its first value; the caller disconnects them.

	pyepics reports a PV connected before it subscribes, so a monitor can silently never start (handoff §3): CA is
	flushed until each PV has delivered, else AssertionError names the ones that did not. That first value is the
	connect-time one, not a shot, so it is dropped; callback(pvname, value, timestamp, severity) gets the later ones,
	on a CA thread (make no CA calls in it).
	"""
	epics = epics_client()
	first, lock = set(), threading.Lock()

	def on_update(pvname=None, value=None, timestamp=None, severity=None, **kw):
		with lock:
			if pvname not in first:
				first.add(pvname)
				return
		callback(pvname, value, timestamp, severity)

	pvs = [epics.PV(name, auto_monitor=True, form="time", callback=on_update) for name in names]
	deadline = time.monotonic() + timeout
	while True:
		epics.ca.flush_io()
		with lock:
			silent = [name for name in names if name not in first]
		if not silent:
			return pvs
		if time.monotonic() > deadline:
			for pv in pvs:
				pv.disconnect()
			raise AssertionError(f"no first value within {timeout} s from {', '.join(silent)}")
		time.sleep(0.05)


def pva_available():
	"""PVA tests need p4p and kernel IPv6: PVXS always opens an IPv6 socket (handoff §3)."""
	try:
		socket.socket(socket.AF_INET6, socket.SOCK_DGRAM).close()
	except OSError:
		return False
	return have("p4p")


def wait_for(predicate, timeout, what):
	"""predicate()'s first truthy value, polled every 0.1 s; AssertionError after `timeout` s."""
	deadline = time.monotonic() + timeout
	while True:
		value = predicate()
		if value:
			return value
		if time.monotonic() > deadline:
			raise AssertionError(f"timed out after {timeout} s waiting for {what}")
		time.sleep(0.1)


class IocProcess:
	"""`python -m diag_ioc` on `config_text` with isolated EPICS, ready once `ready_pv` connects over CA.

	A context manager; `output` collects the process's stdout and stderr lines, and every failure message
	includes them. The process imports test modules from tests/ (PYTHONPATH). The config is `dir`/ioc.toml, so with
	no db_dir the IOC writes `db_path`.
	"""

	def __init__(self, config_text, ready_pv, start_timeout=30.0):
		self.config_text = config_text
		self.ready_pv = ready_pv
		self.start_timeout = start_timeout
		self.output = []
		self.process = None
		self._dir = None

	def __enter__(self):
		self._dir = tempfile.TemporaryDirectory()
		self.dir = Path(self._dir.name)
		self.db_path = self.dir / "ioc.db"
		path = self.dir / "ioc.toml"
		path.write_text(self.config_text)
		paths = [str(REPO), str(TESTS)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
		env = dict(os.environ, **isolated_epics_env(), PYTHONPATH=os.pathsep.join(paths), PYTHONUNBUFFERED="1")
		env.pop("EPICS_CA_MAX_ARRAY_BYTES")  # the client's; the IOC takes the config's ca_max_array_bytes, as deployed
		self.process = subprocess.Popen([sys.executable, "-m", "diag_ioc", "--config", str(path)], cwd=REPO, env=env,
		                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
		threading.Thread(target=self._collect, daemon=True).start()
		try:
			self._wait_ready()
		except BaseException:
			self.__exit__(None, None, None)
			raise
		return self

	def _collect(self):
		for line in self.process.stdout:
			self.output.append(line.rstrip("\n"))

	def _wait_ready(self):
		# The ready log line comes after every module's listener is bound (HEARTBEAT exists earlier, from
		# iocInit); the PV connection then proves the CA client reaches the IOC.
		pv = epics_client().PV(self.ready_pv)
		deadline = time.monotonic() + self.start_timeout
		try:
			while not (any(READY_MESSAGE in line for line in self.output) and pv.wait_for_connection(timeout=0.5)):
				if self.process.poll() is not None:
					raise RuntimeError(f"diag_ioc exited with {self.process.returncode}:\n{self.log()}")
				if time.monotonic() > deadline:
					raise RuntimeError(f"diag_ioc not ready ({self.ready_pv}) after {self.start_timeout} s:\n{self.log()}")
				time.sleep(0.1)
		finally:
			pv.disconnect()

	def log(self):
		return "\n".join(self.output)

	def stop(self, timeout=10.0):
		"""SIGTERM and wait; the exit code. Past `timeout` the process is killed and AssertionError raised."""
		if self.process.poll() is None:
			self.process.terminate()
			try:
				self.process.wait(timeout)
			except subprocess.TimeoutExpired:
				self.process.kill()
				self.process.wait()
				raise AssertionError(f"diag_ioc ignored SIGTERM for {timeout} s:\n{self.log()}") from None
		return self.process.returncode

	def __exit__(self, *exc):
		try:
			if self.process is not None and self.process.poll() is None:
				self.stop()
		finally:
			if self.process is not None:
				self.process.stdout.close()
			self._dir.cleanup()
