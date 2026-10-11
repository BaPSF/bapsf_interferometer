"""The diag_ioc host: load the TOML config, build every module's records, run the EPICS IOC (CA + PVA).

softioc is imported only inside run(), after EPICS_CA_MAX_ARRAY_BYTES is set: it reads EPICS_* when it
loads, and iocInit can run once per process.
"""
import atexit
import importlib
import logging
import math
import os
import re
import threading
import time
import tomllib
from dataclasses import dataclass, field, fields

from diag_ioc.link import LinkListener, parse_address
from diag_ioc.module import ShotPipeline
from diag_ioc.network import ipv4_network
from diag_ioc.outage import Outage
from diag_ioc.records import INVALID_ALARM, MAJOR_ALARM, SOFT_ALARM, UDF_ALARM, fit_text

log = logging.getLogger(__name__)

WATCHDOG_PERIOD_S = 1.0
ERROR_TEXT_BYTES = 256  # STAT:ERROR length, NUL included
READY_MESSAGE = "serving modules"  # logged once every module is started; tests/ioc_harness.py waits for it
_FACTORY = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_]\w*")


#===============================================================================================================================================
# Configuration

@dataclass(frozen=True)
class IocConfig:
	name: str  # devIocStats prefix: <name>:HEARTBEAT, ...
	ca_max_array_bytes: int = 16777216  # applied unless EPICS_CA_MAX_ARRAY_BYTES is already set


@dataclass(frozen=True)
class ModuleConfig:
	name: str
	factory: str  # "module:callable", called as callable(name, options) -> DiagnosticModule
	prefix: str  # PV prefix, no trailing colon
	listen: str | None = None  # IocLink address; None for a module that brings its own data (start(host))
	allow: tuple[str, ...] = ()  # tcp peers accepted; empty: LinkListener's default, 127.0.0.1/32
	mailbox_depth: int = 2
	stale_seconds: float = 10.0
	options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class HostConfig:
	ioc: IocConfig
	modules: tuple[ModuleConfig, ...]


def load_config(path):
	"""HostConfig from a TOML file; ValueError (TOMLDecodeError included) names the first problem."""
	with open(path, "rb") as f:
		return parse_config(tomllib.load(f))


def parse_config(data):
	"""HostConfig from the parsed TOML; unknown keys are errors, so a typo cannot silently fall back to a default.

	Type checks use type() is, not isinstance: TOML values are exact built-in types, and bool is an int subclass.
	"""
	_check_keys("top level", data, {"ioc", "module"})
	ioc = data.get("ioc")
	_require(type(ioc) is dict, "an [ioc] table is required")
	_check_keys("[ioc]", ioc, {f.name for f in fields(IocConfig)})
	_require(_is_pv_prefix(ioc.get("name")), "[ioc] name must be a PV prefix without a trailing colon")
	ioc_config = IocConfig(**ioc)
	_require(type(ioc_config.ca_max_array_bytes) is int and ioc_config.ca_max_array_bytes > 0,
	         "[ioc] ca_max_array_bytes must be a positive integer")
	modules = data.get("module")
	_require(type(modules) is list and modules, "at least one [[module]] table is required")
	module_configs = tuple(_module_config(i, m) for i, m in enumerate(modules))
	for key in ("name", "prefix", "listen"):
		values = [getattr(m, key) for m in module_configs if getattr(m, key) is not None]
		_require(len(values) == len(set(values)), f"[[module]] {key} values must be unique, got {values}")
	return HostConfig(ioc_config, module_configs)


def _module_config(index, table):
	where = f"[[module]] #{index + 1}"
	_require(type(table) is dict, f"{where} must be a table")
	_check_keys(where, table, {f.name for f in fields(ModuleConfig)})
	_require(type(table.get("name")) is str and table["name"], f"{where}: name is required")
	_require(type(table.get("factory")) is str and _FACTORY.fullmatch(table["factory"]),
	         f"{where}: factory must be 'module:callable'")
	_require(_is_pv_prefix(table.get("prefix")), f"{where}: prefix must be a PV prefix without a trailing colon")
	allow = table.get("allow", [])
	_require(type(allow) is list and all(type(a) is str for a in allow), f"{where}: allow must be a list of CIDR strings")
	config = ModuleConfig(**dict(table, allow=tuple(allow)))
	if config.listen is not None:
		_check_value(where, "listen", parse_address, config.listen)
	for cidr in config.allow:
		_check_value(where, "allow", ipv4_network, cidr)
	_require(type(config.mailbox_depth) is int and config.mailbox_depth >= 1, f"{where}: mailbox_depth must be an integer >= 1")
	_require(type(config.stale_seconds) in (int, float) and config.stale_seconds > 0, f"{where}: stale_seconds must be a positive number")
	_require(type(config.options) is dict, f"{where}: options must be a table")
	return config


def _require(condition, message):
	if not condition:
		raise ValueError(f"diag_ioc config: {message}")


def _check_keys(where, table, allowed):
	_require(not set(table) - allowed, f"{where}: unknown keys {sorted(set(table) - allowed)}")


def _check_value(where, key, parse, value):
	try:
		parse(value)
	except Exception as e:  # ValueError, TypeError, or argparse.ArgumentTypeError from ipv4_network
		raise ValueError(f"diag_ioc config: {where}: {key}: {e}") from None


def _is_pv_prefix(value):
	return type(value) is str and bool(value) and not value.endswith(":")


#===============================================================================================================================================
# Runtime

class StatusRecords:
	"""<prefix>:STAT:* of one module. All but CONNECTED report the pipeline, which every module has, including
	one that feeds it from start(host) instead of a `listen` link; CONNECTED exists only with a link."""

	def __init__(self, builder, linked):
		self.connected = None
		if linked:
			self.connected = builder.boolIn("STAT:CONNECTED", ZNAM="no", ONAM="yes", ZSV="MINOR", DESC="a sender is connected")
		self.stale = builder.boolIn("STAT:STALE", ZNAM="fresh", ONAM="stale", OSV="MAJOR", DESC="no shot within stale_seconds")
		self.age = builder.aIn("STAT:AGE_S", EGU="s", PREC=1, DESC="time since the last shot arrived")
		# IOC-side only: shots lost before the IOC (IocLink drops, link outages) show as gaps in the shot number instead.
		self.dropped = builder.longIn("STAT:DROPPED", DESC="shots dropped by the IOC mailbox")
		self.analysis = builder.aIn("STAT:ANALYSIS_S", EGU="s", PREC=3, DESC="analyze + publish time, last shot")
		self.error = builder.longStringIn("STAT:ERROR", length=ERROR_TEXT_BYTES, DESC="last analysis error")

	def shot_done(self, analysis_s, error):
		self.analysis.set(analysis_s)
		if error:
			self.error.set(fit_text(error, ERROR_TEXT_BYTES), severity=MAJOR_ALARM, alarm=SOFT_ALARM)
		else:
			self.error.set("")

	def tick(self, now, connected, last_received, stale_seconds, dropped):
		# Re-set every period: a set() posts a monitor only when the value or severity changes.
		if self.connected is not None:
			self.connected.set(int(connected))
		if last_received is None:  # no shot since start: no age to report, and nothing fresh
			self.age.set(math.nan, severity=INVALID_ALARM, alarm=UDF_ALARM)
			stale = True
		else:
			self.age.set(now - last_received)
			stale = now - last_received > stale_seconds
		self.stale.set(int(stale))
		self.dropped.set(dropped)


class ModuleHost:
	"""One configured module at runtime: its pipeline and status records, and with `listen` its listener."""

	def __init__(self, config, module, status):
		self.config = config
		self.module = module
		self.status = status
		self.pipeline = ShotPipeline(module, config.mailbox_depth, status.shot_done)
		self.listener = None

	def start(self):
		# Module before listener: no shot is published before the module's start() has set its fixed records.
		self.module.start(self)
		if self.config.listen is not None:
			self.listener = LinkListener(self.config.listen, self.pipeline.submit, self.config.allow).start()
			log.info("module %s listening on %s", self.config.name, self.listener.address)

	def tick(self, now):
		connected = self.listener is not None and self.listener.connections > 0
		self.status.tick(now, connected, self.pipeline.last_received, self.config.stale_seconds, self.pipeline.dropped)

	def stop(self):
		# Listener first, so no shot arrives at a stopped pipeline; each step runs even if one before it fails.
		steps = [self.pipeline.close, self.module.stop]
		if self.listener is not None:
			steps.insert(0, self.listener.close)
		for step in steps:
			try:
				step()
			except Exception:
				log.exception("module %s: shutdown step %s failed", self.config.name, step.__qualname__)


def _watchdog(hosts, stop):
	outage = Outage(log, "status watchdog")
	while not stop.wait(WATCHDOG_PERIOD_S):
		try:
			now = time.time()
			for host in hosts:
				host.tick(now)
		except Exception as e:
			outage.failed(e, exc_info=True)
		else:
			outage.ended()


def _factory(spec):
	module_name, _, attribute = spec.partition(":")
	return getattr(importlib.import_module(module_name), attribute)


def run(config, interactive=False):
	"""Build every module's records, start the IOC, serve until SIGINT/SIGTERM (or the shell exits). Does not return.

	Exit runs epicsExit after Python's atexit handlers, which close every listener (removing unix sockets).
	"""
	os.environ.setdefault("EPICS_CA_MAX_ARRAY_BYTES", str(config.ioc.ca_max_array_bytes))
	from softioc import asyncio_dispatcher, builder, softioc

	dispatcher = asyncio_dispatcher.AsyncioDispatcher()
	softioc.devIocStats(config.ioc.name)
	hosts = []
	for module_config in config.modules:
		module = _factory(module_config.factory)(module_config.name, module_config.options)
		builder.SetDeviceName(module_config.prefix)
		module.create_records(builder)
		hosts.append(ModuleHost(module_config, module, StatusRecords(builder, linked=module_config.listen is not None)))
	builder.LoadDatabase()
	softioc.iocInit(dispatcher)

	stop = threading.Event()

	def shutdown():
		stop.set()
		for host in reversed(hosts):
			host.stop()

	atexit.register(shutdown)  # before start(): a module failing to start still gets the others closed
	for host in hosts:
		host.start()
	threading.Thread(target=_watchdog, args=(hosts, stop), name="diag-ioc-watchdog", daemon=True).start()
	log.info("IOC %s %s %s", config.ioc.name, READY_MESSAGE, ", ".join(f"{h.config.name} ({h.config.prefix})" for h in hosts))
	if interactive:
		softioc.interactive_ioc({"hosts": {h.config.name: h for h in hosts}})
	else:
		softioc.non_interactive_ioc()
