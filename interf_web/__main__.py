# -*- coding: utf-8 -*-
"""interf-web --prefix P: read-only browser page of the IOC's PVA group <P>:SHOT (docs/ARCHITECTURE.md D18).

History covers only what this process has seen, in memory and its own files, never HDF5 (D18, section 2).
"""
import argparse
import asyncio
import concurrent.futures
import contextlib
import dataclasses
import datetime
import json
import logging
import math
import os
import struct
import sys
import time
from pathlib import Path

import numpy as np

from diag_ioc.outage import LOG_FORMAT, Outage
from interf_ioc import time_text
from interf_shot import SHOT_ZONE, local_time, shot_id
from interf_store import PORTS, _text

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
# Selects the fields shot_message reads. If QSRV2 ignores the selection the whole group arrives, and still works.
_PORT_FIELDS = ("time_array", "ne", "ne_mean", "missing")
SHOT_REQUEST = "field(timeStamp,num,date,host_time,missing,{})".format(
	",".join(f"{port.lower()}.{key}" for port in PORTS for key in _PORT_FIELDS))
WAITING_AFTER_S = 10.0  # about 3 missed shots at one per 3 s
STATUS_PERIOD_S = 1.0  # also how soon a socket that gets no shots notices its browser left: only a send can tell
KEEP_DAYS = 2  # shot dates held, the newest ones; normally today and yesterday
TREND_POINTS_MAX = 5000
STOP_TIMEOUT_S = 5  # for open sockets at exit; under the unit's TimeoutStopSec (30 s)
_ROW_WIDTH = 1 + len(PORTS)  # a history row: host_time, then one ne_mean per port


@dataclasses.dataclass(frozen=True)
class WebShot:
	"""One shot for the page. ne_mean (m^-3, NaN for none) is aligned with PORTS. payload is the /ws binary frame: "<u4"
	header length, a JSON header padded to 4 bytes (so a Float32Array can start right after it), then "<f4" time_array
	(ms) and ne (m^-3) of each header port in order."""
	shot_date: int
	shot_number: int
	host_time: float
	ne_mean: tuple
	payload: bytes


def _none_if_nan(value):
	"""JSON has no NaN: JSON.parse rejects it, and FastAPI's response raises on it."""
	return None if math.isnan(value) else value


def shot_message(value):
	"""WebShot of one <P>:SHOT value (a p4p Value, or the same fields as nested dicts), with nothing kept from `value`;
	None for the IOC's state before its first shot after a start (date or timeStamp still 0). A port's own reason is
	"" when it has data (interf_ioc.publish)."""
	date = int(value["date"])
	stamp = value["timeStamp"]
	if date == 0 or stamp["secondsPastEpoch"] + stamp["nanoseconds"] == 0:
		return None
	number, host_time = int(value["num"]), float(value["host_time"])
	ports, arrays, means = [], [], []
	for port in PORTS:
		field = value[port.lower()]
		t_ms = np.asarray(field["time_array"]["value"], dtype="<f4")
		ne = np.asarray(field["ne"]["value"], dtype="<f4")
		if t_ms.size != ne.size:
			raise ValueError(f"{port}: {t_ms.size} times, {ne.size} densities")
		mean = float(field["ne_mean"])
		means.append(mean)
		ports.append({"name": port, "n": int(ne.size), "ne_mean": _none_if_nan(mean), "missing": _text(field["missing"])})
		arrays += [t_ms.tobytes(), ne.tobytes()]
	header = json.dumps({"id": shot_id(date, number), "time": time_text(host_time), "missing": _text(value["missing"]),
	                     "ports": ports}, allow_nan=False).encode()
	header += b" " * (-len(header) % 4)
	return WebShot(date, number, host_time, tuple(means), b"".join((struct.pack("<I", len(header)), header, *arrays)))


def trend(rows, start, end, points):
	"""{"t": [...], port: {"min", "max", "mean": [...]}} of history `rows` over points equal time buckets of [start, end).
	Only buckets holding a shot are returned, and t is their mean host_time, so a short range gives the shots themselves.
	A value is None where the bucket's shots have no NE_MEAN."""
	rows = rows[(rows[:, 0] >= start) & (rows[:, 0] < end)]
	buckets, index = np.unique(((rows[:, 0] - start) / (end - start) * points).astype(np.int64), return_inverse=True)
	result = {"t": (np.bincount(index, weights=rows[:, 0]) / np.bincount(index)).tolist()}
	for column, port in enumerate(PORTS, start=1):
		values = rows[:, column]
		has = ~np.isnan(values)
		count = np.bincount(index[has], minlength=buckets.size)
		total = np.bincount(index[has], weights=values[has], minlength=buckets.size)
		low, high = np.full(buckets.size, np.nan), np.full(buckets.size, np.nan)
		np.fmin.at(low, index, values)  # fmin and fmax skip NaN
		np.fmax.at(high, index, values)
		mean = np.divide(total, count, out=np.full(buckets.size, np.nan), where=count > 0)
		result[port] = {key: [_none_if_nan(v) for v in array.tolist()]
		                for key, array in (("min", low), ("max", high), ("mean", mean))}
	return result


class _Day:
	"""One shot date of the history: its rows as trend() takes them, and the shot numbers held."""

	def __init__(self):
		self.numbers = set()
		self._rows = np.empty((1024, _ROW_WIDTH))

	def add(self, number, row):
		"""False if `number` is already held: the IOC sends its last shot to every new monitor."""
		if number in self.numbers:
			return False
		if len(self.numbers) == len(self._rows):
			self._rows = np.concatenate((self._rows, np.empty_like(self._rows)))
		self._rows[len(self.numbers)] = row
		self.numbers.add(number)
		return True

	@property
	def rows(self):
		return self._rows[:len(self.numbers)]


class History:
	"""NE_MEAN per shot of the KEEP_DAYS newest shot dates, keyed by (date, number), since the number restarts each
	midnight (D14). Every method but the file ones runs on the event loop, so no lock; file work runs on one thread, in
	order, so a slow disk holds up neither the page nor the p4p callback.

	File ne_mean_<YYYYMMDD>.txt, one line per shot: number, then a row. Lines, not binary records: a line torn by a crash
	or a full disk fails the field count on load and costs that shot, not the file."""

	def __init__(self, state_dir):
		self.state_dir = state_dir
		self._days = {}
		self._file = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="interf_web file")
		self._outage = Outage(log, f"history file in {state_dir}")

	def _path(self, date):
		return self.state_dir / f"ne_mean_{date}.txt"

	def load(self):
		"""Call once before monitoring. Trouble is logged and leaves that much less history; the page runs regardless."""
		try:
			self.state_dir.mkdir(parents=True, exist_ok=True)
			paths = sorted(self.state_dir.glob("ne_mean_*.txt"))  # the fixed-width date sorts by name
		except OSError as e:
			log.warning("cannot use %s (%s: %s); history starts empty", self.state_dir, type(e).__name__, e)
			return
		for path in paths[:-KEEP_DAYS]:
			self._file.submit(self._delete, path)
		for path in paths[-KEEP_DAYS:]:
			try:
				self._load_file(path)
			except (OSError, ValueError) as e:
				log.warning("cannot read %s (%s: %s)", path, type(e).__name__, e)

	def _load_file(self, path):
		day, skipped = _Day(), 0
		with open(path, encoding="ascii", errors="replace") as f:
			for line in f:
				fields = line.split()
				try:
					if len(fields) != 1 + _ROW_WIDTH:
						raise ValueError
					day.add(int(fields[0]), [float(field) for field in fields[1:]])
				except ValueError:
					skipped += 1
		self._days[int(path.stem.rsplit("_", 1)[1])] = day
		log.info("history: %d shots from %s, %d lines skipped", len(day.numbers), path.name, skipped)

	def add(self, shot):
		"""Add a WebShot once. A new date drops the oldest day; a shot older than both kept days (the IOC's last shot
		after a long stop) is not history."""
		day = self._days.get(shot.shot_date)
		if day is None:
			if len(self._days) >= KEEP_DAYS and shot.shot_date < min(self._days):
				return
			day = self._days[shot.shot_date] = _Day()
			for old in sorted(self._days)[:-KEEP_DAYS]:
				del self._days[old]
				self._file.submit(self._delete, self._path(old))
		if day.add(shot.shot_number, (shot.host_time, *shot.ne_mean)):
			means = " ".join(f"{mean:.5e}" for mean in shot.ne_mean)
			self._file.submit(self._append, self._path(shot.shot_date), f"{shot.shot_number} {shot.host_time:.3f} {means}\n")

	def _append(self, path, line):
		try:
			with open(path, "a", encoding="ascii") as f:
				f.write(line)
		except OSError as e:
			self._outage.failed(e, "shot kept in memory only")
		else:
			self._outage.ended()

	def _delete(self, path):
		try:
			path.unlink(missing_ok=True)
		except OSError as e:
			self._outage.failed(e, f"{path.name} not removed")

	def rows(self):
		return np.concatenate([day.rows for day in self._days.values()] or [np.empty((0, _ROW_WIDTH))])

	def close(self):
		"""Finish the queued file work. A hung disk blocks here until the unit's TimeoutStopSec, as at interpreter exit."""
		self._file.shutdown()


class ShotHub:
	"""The newest shot and what the page needs beside it. Runs on the event loop except for ne_window_ms, a plain
	reference set from a p4p callback. Keeping only the newest is what makes /ws latest-wins: a browser blocked in a send
	takes whatever is newest when it returns, and holds up nobody else."""

	def __init__(self):
		self.latest = None
		self.ne_window_ms = None
		self._changed = asyncio.Event()

	def publish(self, shot):
		self.latest = shot
		changed, self._changed = self._changed, asyncio.Event()
		changed.set()

	async def wait_newer(self, sent, timeout):
		"""Until `latest` is no longer `sent`, for at most `timeout` s."""
		if self.latest is sent:
			with contextlib.suppress(TimeoutError):
				await asyncio.wait_for(self._changed.wait(), timeout)

	def status(self):
		"""The /ws text frame. Times are Bobby's clock (D18), never the browser's; age comes from host_time rather than
		arrival, so the IOC's last shot received at a restart does not read as new."""
		now = time.time()
		age = None if self.latest is None else max(0.0, now - self.latest.host_time)
		today = local_time(now).date()
		midnights = [datetime.datetime.combine(day, datetime.time(), SHOT_ZONE).timestamp()
		             for day in (today, today - datetime.timedelta(days=1))]
		return json.dumps({"now": now, "age_s": age, "waiting": age is None or age >= WAITING_AFTER_S,
		                   "today_start": midnights[0], "yesterday_start": midnights[1], "ne_window_ms": self.ne_window_ms})


class ShotFeed:
	"""The p4p callbacks. They never block (D18) and must not raise: p4p closes a subscription whose callback raises."""

	def __init__(self, group, loop, deliver, hub):
		self.group, self._loop, self._deliver, self._hub = group, loop, deliver, hub
		self._last = None  # (shot_date, shot_number)
		self._connected = None  # None until p4p's first notice
		self._monitor = Outage(log, f"PVA monitor of {group}")
		self._reads = Outage(log, f"reading {group}")

	def on_shot(self, value):
		"""A <P>:SHOT notice: a value, or an exception (Disconnected, RemoteError). The page goes to its waiting state by
		itself when shots stop."""
		if isinstance(value, Exception):
			# Only p4p's very first notice is skipped, always a Disconnected: a RemoteError before any connection (a
			# pvRequest the server rejects) must reach the log.
			if self._connected is not None:
				self._monitor.failed(value)
			self._connected = False
			return
		if not self._connected:
			self._connected = True
			if self._monitor.since is None:
				log.info("%s connected", self.group)
			else:
				self._monitor.ended()
		try:
			shot = shot_message(value)
		except Exception as e:  # a group without these fields: another IOC module or layout
			self._reads.failed(e, "update not shown", exc_info=True)
			return
		self._reads.ended()
		if shot is None or (shot.shot_date, shot.shot_number) == self._last:  # the same shot again after a reconnect
			return
		self._last = (shot.shot_date, shot.shot_number)
		self._loop.call_soon_threadsafe(self._deliver, shot)

	def on_window(self, value):
		"""<P>:CFG:NE_WINDOW_MS, for the page's shading. Its own monitor: the record is not in the group (interf_ioc)."""
		try:
			self._hub.ne_window_ms = [float(edge) for edge in value]
		except Exception as e:
			log.warning("unreadable NE_MEAN window %r (%s)", value, e)


def create_app(prefix, state_dir):
	# The web extra; imported here so the pure parts load without it.
	from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
	from fastapi.responses import FileResponse, JSONResponse
	from fastapi.staticfiles import StaticFiles

	hub = ShotHub()
	history = History(state_dir)

	def deliver(shot):
		hub.publish(shot)
		history.add(shot)

	@contextlib.asynccontextmanager
	async def lifespan(app):
		from p4p.client.thread import Context  # the clients extra

		history.load()
		group = f"{prefix}:SHOT"
		feed = ShotFeed(group, asyncio.get_running_loop(), deliver, hub)
		ctx = Context("pva")  # EPICS_PVA_* from the environment
		subscriptions = [ctx.monitor(group, feed.on_shot, request=SHOT_REQUEST, notify_disconnect=True),
		                 ctx.monitor(f"{prefix}:CFG:NE_WINDOW_MS", feed.on_window)]
		log.info("monitoring %s", group)
		try:
			yield
		finally:
			for subscription in subscriptions:
				subscription.close()
			ctx.close()
			history.close()

	# No openapi_url, so no generated docs either: they load from a CDN the lab network may not reach.
	app = FastAPI(lifespan=lifespan, openapi_url=None)
	app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

	@app.get("/")
	async def page():
		return FileResponse(STATIC_DIR / "index.html")

	@app.get("/api/trend")
	async def api_trend(start: float, end: float, points: int = Query(ge=1, le=TREND_POINTS_MAX)):
		# async: it must stay on the event loop with History.add (History has no lock).
		if not (math.isfinite(start) and math.isfinite(end) and start < end):
			raise HTTPException(422, "start and end must be finite, with start < end")
		# JSONResponse here, or FastAPI's encoder walks every float once more on the event loop.
		return JSONResponse(trend(history.rows(), start, end, points))

	@app.websocket("/ws")
	async def stream(socket: WebSocket):
		"""Never receives (D18)."""
		await socket.accept()
		sent = None
		with contextlib.suppress(WebSocketDisconnect):
			while True:
				shot = hub.latest
				if shot is not sent:
					sent = shot
					await socket.send_bytes(shot.payload)
				# After the shot, so `now` is never older than its host_time when the page asks for the trend up to `now`.
				await socket.send_text(hub.status())
				await hub.wait_newer(sent, STATUS_PERIOD_S)

	return app


def main(argv=None):
	parser = argparse.ArgumentParser(prog="interf-web", description="read-only browser page of the IOC's PVA group <P>:SHOT")
	parser.add_argument("--prefix", required=True, help="the IOC module's PV prefix (its config's prefix); the group is <prefix>:SHOT")
	args = parser.parse_args(argv)
	logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)  # under systemd the journal keeps it
	import uvicorn  # the web extra

	state_dir = Path(os.environ.get("INTERF_WEB_STATE_DIR", Path.home() / "data" / "interferometer" / "web"))
	# ws and lifespan are named so trouble stops the start: with "auto", a missing websockets package leaves every /ws
	# refused, and a failed start of the monitor can read as "lifespan unsupported" and serve a page with no data.
	# No deflate: float32 samples barely compress on a LAN, and each browser's copy would cost the event loop.
	uvicorn.run(create_app(args.prefix, state_dir), host=os.environ.get("INTERF_WEB_HOST", "127.0.0.1"),
	            port=int(os.environ.get("INTERF_WEB_PORT", "8080")), ws="websockets-sansio", ws_per_message_deflate=False,
	            lifespan="on", log_config=None, access_log=False, timeout_graceful_shutdown=STOP_TIMEOUT_S)
	return 0


if __name__ == "__main__":
	sys.exit(main())
