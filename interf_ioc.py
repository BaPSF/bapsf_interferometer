# -*- coding: utf-8 -*-
"""diag_ioc module of the interferometers: raw shots from the link -> analyze_shot -> the D4 PVs (docs/ARCHITECTURE.md).

Config: factory = "interf_ioc:InterferometerModule"; deploy/diag-ioc/interferometer.toml lists the options.
Imports neither softioc (the host passes `builder`) nor streamer (D6).
"""
import math
import re
from functools import partial

import numpy as np

import interf_payload
from diag_ioc.module import DiagnosticModule
from diag_ioc.records import INVALID_ALARM, READ_ALARM, UDF_ALARM, add_group, chain, fit_text, passive, scalar, waveform
from interf_analysis import PORTS, Port, analyze_shot, get_calibration_factor
from interf_shot import clock, local_time

# Points per port waveform (NELM). A 1,000,002-sample LeCroy record (HDO4104 at 0.1 us) gives 1953 phase points at
# FT_len 512, so every point fits; a longer record is decimated by analyze_shot to fit.
MAX_POINTS = 2048
NE_WINDOW_MS = (5.0, 10.0)
PLASMA_LENGTH_M = 0.4
MISSING_TEXT_BYTES = 1024  # SHOT:MISSING, NUL included
_OPTIONS = {"ports", "max_points", "ne_window_ms", "plasma_length_m"}
_PORT_KEYS = {"name", "scope", "ref", "plasma", "freq_hz"}
_PORT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,7}")  # a PV name part, and lower-cased a PVA group field name


def _require(condition, message):
	if not condition:
		raise ValueError(f"interferometer options: {message}")


def _number(value):
	return type(value) in (int, float) and math.isfinite(value)


def _ports(tables):
	"""Ports from [[module.options.ports]] tables (keys _PORT_KEYS)."""
	_require(type(tables) is list and tables, "ports must be a non-empty list of tables")
	ports = []
	for i, table in enumerate(tables):
		where = f"ports #{i + 1}"
		_require(type(table) is dict and set(table) == _PORT_KEYS, f"{where}: keys must be {sorted(_PORT_KEYS)}")
		_require(type(table["name"]) is str and _PORT_NAME.fullmatch(table["name"]),
		         f"{where}: name must be a letter then up to 7 letters, digits or _")
		_require(table["scope"] in ("lecroy", "rigol"), f"{where}: scope must be 'lecroy' or 'rigol'")
		_require(all(type(table[k]) is str and table[k] for k in ("ref", "plasma")), f"{where}: ref and plasma must be channel names")
		_require(_number(table["freq_hz"]) and table["freq_hz"] > 0, f"{where}: freq_hz must be a positive number")
		# Upper case: interf_raw and interf_payload.decode key channels as "C1".
		ports.append(Port(table["name"], table["scope"], table["ref"].upper(), table["plasma"].upper(), float(table["freq_hz"])))
	names = [p.name for p in ports]
	_require(len(set(names)) == len(names), f"port names must be unique, got {names}")
	return tuple(ports)


def time_text(shot_time):
	"""SHOT:TIME_STR: YYYY-MM-DD HH:MM:SS.fff in America/Los_Angeles, never the IOC host's zone (D15)."""
	return f"{local_time(shot_time):%Y-%m-%d} {clock(shot_time)}"


def missing_text(acq_missing, ports):
	"""SHOT:MISSING: each acquisition reason once, then each port reason that is not one of them (a port missing
	because its scope is, e.g. P40 without the Rigol, repeats the acquisition reason)."""
	parts = [f"{scope}: {reason}" for scope, reason in acq_missing.items()]
	parts += [f"{name}: {port.missing}" for name, port in ports.items()
	          if port.missing and port.missing not in acq_missing.values()]
	return "; ".join(parts)


class LostShots:
	"""SHOT:LOST: running total of gaps in shot_number. A new date, a number not above the last, and the first shot
	after IOC start reset the baseline, so they add nothing (shots lost across midnight are not counted)."""

	def __init__(self):
		self.total = 0
		self._last = None  # (shot_date, shot_number)

	def update(self, date, number):
		if self._last is not None and self._last[0] == date and number > self._last[1]:
			self.total += number - self._last[1] - 1
		self._last = (date, number)
		return self.total


class InterferometerModule(DiagnosticModule):
	"""Options (all optional): ports (default interf_analysis.PORTS), max_points (MAX_POINTS), ne_window_ms
	(NE_WINDOW_MS, (start, stop) on the trigger-relative time axis), plasma_length_m (PLASMA_LENGTH_M).
	ValueError at construction names a bad option."""

	def __init__(self, name, options):
		super().__init__(name, options)
		_require(not set(self.options) - _OPTIONS, f"unknown keys {sorted(set(self.options) - _OPTIONS)}")
		self.ports = _ports(self.options["ports"]) if "ports" in self.options else PORTS
		self.max_points = self.options.get("max_points", MAX_POINTS)
		_require(type(self.max_points) is int and self.max_points >= 1, "max_points must be an integer >= 1")
		window = self.options.get("ne_window_ms", NE_WINDOW_MS)
		_require(type(window) in (list, tuple) and len(window) == 2 and all(map(_number, window)) and window[0] < window[1],
		         "ne_window_ms must be [start, stop] in ms with start < stop")
		self.ne_window_ms = (float(window[0]), float(window[1]))
		self.plasma_length_m = self.options.get("plasma_length_m", PLASMA_LENGTH_M)
		_require(_number(self.plasma_length_m) and self.plasma_length_m > 0, "plasma_length_m must be a positive number")
		self._lost = LostShots()

	def create_records(self, builder):
		# Publish order (D7): SHOT:NUM is the I/O Intr head, so its set() runs the whole chain, last port's NE_MEAN last.
		# Each maker is called once by chain(), with FLNK= for all but the last.
		makers = [
			("num", partial(builder.longIn, "SHOT:NUM", TSE=-2, DESC="shot number within the day")),
			("date", partial(passive, builder.longIn, "SHOT:DATE", "shot date YYYYMMDD, Los Angeles")),
			# MPST: a stringin posts only on change by default (a waveform always), and TIME_SOURCE repeats.
			("time_source", partial(passive, builder.stringIn, "SHOT:TIME_SOURCE", "trigger or host: shot time source", MPST="Always")),
			("host_time", partial(scalar, builder, "SHOT:HOST_TIME", egu="s", prec=6, desc="epoch s the PC saw the capture")),
			("time_str", partial(passive, builder.stringIn, "SHOT:TIME_STR", "shot time, Los Angeles", MPST="Always")),
			("crit_path", partial(scalar, builder, "SHOT:CRIT_PATH", egu="s", prec=3, desc="acquisition critical path")),
			("trig_lag", partial(scalar, builder, "STAT:TRIG_LAG", egu="s", prec=3, desc="host_time - shot time")),
			("lost", partial(passive, builder.longIn, "SHOT:LOST", "shots lost on the link (number gaps)")),
			("missing", partial(passive, builder.longStringIn, "SHOT:MISSING", "missing scope/channel and why", length=MISSING_TEXT_BYTES)),
		]
		for port in self.ports:
			p = port.name
			makers += [
				((p, "time_array"), partial(waveform, builder, f"{p}:TIME_ARRAY", self.max_points, egu="ms", desc="time axis, trigger-relative")),
				((p, "phase"), partial(waveform, builder, f"{p}:PHASE", self.max_points, egu="rad", desc="phase")),
				((p, "ne"), partial(waveform, builder, f"{p}:NE", self.max_points, egu="m^-3", desc="path-averaged density")),
				((p, "ne_mean"), partial(scalar, builder, f"{p}:NE_MEAN", egu="m^-3", desc="mean NE over CFG:NE_WINDOW_MS")),
			]
		keys, makes = zip(*makers)
		self._records = dict(zip(keys, chain(makes)))

		for port in self.ports:  # fixed from config, outside the chain
			builder.aIn(f"{port.name}:CAL", initial_value=get_calibration_factor(port.freq_hz, self.plasma_length_m),
			            EGU="m^-3/rad", DESC="density per rad of phase")
			builder.aIn(f"{port.name}:FREQ", initial_value=port.freq_hz / 1e9, EGU="GHz", PREC=1, DESC="interferometer frequency")
		builder.WaveformIn("CFG:NE_WINDOW_MS", initial_value=np.array(self.ne_window_ms), EGU="ms", DESC="NE_MEAN window start, stop")

		self._add_group(builder.RecordName("SHOT"))
		return self._records[(self.ports[-1].name, "ne_mean")]

	def _add_group(self, group):
		"""PVA group <P>:SHOT: one update per shot, posted when the last port's NE_MEAN processes (D7)."""
		r = self._records
		add_group(r["num"], group, {"": {"+type": "meta"}, "num": {"+type": "plain"}})
		for key in ("date", "time_source", "host_time", "lost", "missing"):
			add_group(r[key], group, {key: {"+type": "plain"}})
		for port in self.ports:
			field = port.name.lower()
			for key in ("time_array", "phase", "ne"):
				add_group(r[(port.name, key)], group, {f"{field}.{key}": {"+type": "scalar"}})
			trigger = {"+trigger": "*"} if port is self.ports[-1] else {}
			add_group(r[(port.name, "ne_mean")], group, {f"{field}.ne_mean": {"+type": "plain", **trigger}})

	def analyze(self, variables):
		return analyze_shot(interf_payload.decode(variables), ports=self.ports, plasma_length=self.plasma_length_m,
		                    max_points=self.max_points, ne_window_ms=self.ne_window_ms)

	def publish(self, result):
		r, ts = self._records, result.shot_time
		r["date"].set(result.shot_date, timestamp=ts)
		r["time_source"].set(result.time_source, timestamp=ts)
		r["host_time"].set(result.host_time, timestamp=ts)
		r["time_str"].set(time_text(ts), timestamp=ts)
		r["crit_path"].set(result.critical_path_s, timestamp=ts)
		r["trig_lag"].set(result.host_time - ts, timestamp=ts)
		r["lost"].set(self._lost.update(result.shot_date, result.shot_number), timestamp=ts)
		r["missing"].set(fit_text(missing_text(result.acq_missing, result.ports), MISSING_TEXT_BYTES), timestamp=ts)
		for name, port in result.ports.items():
			# Missing data is empty + INVALID, never fabricated zeros (D7).
			bad = {"severity": INVALID_ALARM, "alarm": READ_ALARM} if port.missing else {}
			for key, values in (("time_array", port.t_ms), ("phase", port.phase), ("ne", port.ne)):
				r[(name, key)].set(values, timestamp=ts, **bad)
			if not bad and math.isnan(port.ne_mean):  # window not inside the trace, see PortResult.ne_mean
				bad = {"severity": INVALID_ALARM, "alarm": UDF_ALARM}
			r[(name, "ne_mean")].set(port.ne_mean, timestamp=ts, **bad)
		r["num"].set(result.shot_number, timestamp=ts)  # last: processing the head publishes the shot
