# -*- coding: utf-8 -*-
"""interf-store --prefix P [--data-dir DIR]: store every shot of the IOC's PVA group <P>:SHOT in the daily HDF5 file.

Best effort (docs/ARCHITECTURE.md D9): a shot published while the store is down or behind, or one whose write fails, is
logged and not stored; the raw stream is the record. Its own process, fed only by the group (one complete update per
shot, D7): nothing in acquisition or the IOC imports it, calls it or waits on it.
"""
import argparse
import dataclasses
import datetime
import logging
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

import h5py
import numpy as np

from diag_ioc.outage import LOG_FORMAT, Outage
from diag_ioc.records import INVALID_ALARM
from interf_file import ShotExists, create_sourcefile_dataset, init_hdf5_file
from interf_ioc import LostShots
from interf_shot import shot_id

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("INTERF_DATA_DIR", Path.home() / "data" / "interferometer"))
# dataA, dataB, dataC of the legacy layout (create_sourcefile_dataset); the group's port fields are lower-case.
PORTS = ("P20", "P29", "P40")
QUEUE_SHOTS = 100  # about 5 min at a shot per 3 s; a shot is about 150 kB at 2048 points per array
STOP_TIMEOUT_S = 20.0  # for the writer to store what is queued at exit; under the unit's TimeoutStopSec (30 s), so the summary is logged before SIGKILL


@dataclasses.dataclass(frozen=True)
class GroupShot:
	"""One <P>:SHOT update. phase (rad) and t_ms (ms) are {port: float64 array}, empty for a missing port. missing is
	{port: its <port>:MISSING reason} for each port whose PHASE is INVALID."""
	shot_date: int
	shot_number: int
	shot_time: float
	time_source: str
	phase: dict
	t_ms: dict
	missing: dict


def _text(value):
	"""A text group field: str, or a longStringIn's NUL-terminated char array (QSRV serves a char waveform as an array)."""
	if isinstance(value, str):
		return value
	return np.asarray(value).astype(np.uint8).tobytes().split(b"\0", 1)[0].decode("utf-8", "replace")


def shot_from_group(value):
	"""GroupShot of one <P>:SHOT value (a p4p Value, or the same fields as nested dicts), with every array copied; None
	for the IOC's state before its first shot after a start (date or timeStamp still 0), which a new or reconnected
	monitor receives. Identity and data come from this one update, never from another PV."""
	date = int(value["date"])
	if date == 0:
		return None
	stamp = value["timeStamp"]  # SHOT:NUM's, which publish() sets to shot_time
	# softioc stores int(t) s and int(1e9 * (t % 1)) ns of the float64 shot_time. 1 ns is far below a float64 step at
	# today's epoch (2.4e-7 s), so this rounds back to shot_time exactly, and str() of it (the dataset name) equals
	# str() of the trigger time recomputed offline from the WAVEDESC.
	shot_time = stamp["secondsPastEpoch"] + stamp["nanoseconds"] * 1e-9
	if shot_time == 0:
		return None
	phase, t_ms, missing = {}, {}, {}
	for port in PORTS:
		field = value[port.lower()]
		phase[port] = np.array(field["phase"]["value"], dtype=np.float64)
		t_ms[port] = np.array(field["time_array"]["value"], dtype=np.float64)
		if field["phase"]["alarm"]["severity"] == INVALID_ALARM:
			missing[port] = _text(field["missing"])
	return GroupShot(date, int(value["num"]), shot_time, str(value["time_source"]), phase, t_ms, missing)


def store_shot(data_dir, shot):
	"""Write `shot` (a GroupShot) to interferometer_data_<YYYY-MM-DD of shot_date>.hdf5 in data_dir, in the previous
	acquisition's layout. False when the file already holds it: the IOC sends its last shot to every new or reconnected
	monitor."""
	day = datetime.date(shot.shot_date // 10000, shot.shot_date // 100 % 100, shot.shot_date % 100)
	path = Path(data_dir) / f"interferometer_data_{day.isoformat()}.hdf5"
	if not path.exists():
		init_hdf5_file(path)
	with h5py.File(path, "a", libver="latest") as f:  # opened per shot, so the file is complete between shots
		try:
			create_sourcefile_dataset(
				f, shot.phase["P20"], shot.phase["P29"], shot.phase["P40"],
				shot.t_ms["P20"] if shot.t_ms["P20"].size else shot.t_ms["P29"], shot.t_ms["P40"], shot.shot_time,
				rigol_missing="P40" in shot.missing, rigol_missing_reason=shot.missing.get("P40", ""),
				lecroy_missing={port: reason for port, reason in shot.missing.items() if port != "P40"}, shot=shot)
		except ShotExists:
			return False
	return True


class ShotWriter:
	"""on_update, the p4p monitor callback of <P>:SHOT, only copies each shot onto a bounded queue; run() stores them on
	its own thread. Each counter has one writing thread: dropped and unreadable the callback's, the rest run()'s."""

	def __init__(self, data_dir, group, queue_shots=QUEUE_SHOTS):
		self.data_dir, self.group = data_dir, group
		self.stored = self.duplicates = self.failed = 0
		self.dropped = self.unreadable = 0
		self._queue = queue.Queue(queue_shots)
		self._finished = threading.Event()
		self._connected = False
		self._gaps = LostShots()  # SHOT:LOST's rule, over the shots run() takes
		self._monitor = Outage(log, f"PVA monitor of {group}")
		self._reads = Outage(log, f"reading {group}")
		self._full = Outage(log, "store queue")
		self._writes = Outage(log, f"HDF5 write in {data_dir}")

	def on_update(self, value):
		"""A monitor notice: a value, or an exception (Disconnected, which p4p also sends once before the first
		connection; RemoteError). p4p calls it on one thread per subscription, in order."""
		if isinstance(value, Exception):
			if self._connected:
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
			shot = shot_from_group(value)
		except Exception as e:  # a group without the fields above: another IOC module or layout
			self.unreadable += 1
			self._reads.failed(e, "update not stored", exc_info=True)
			return
		self._reads.ended()
		if shot is None:
			return
		try:
			self._queue.put_nowait(shot)
		except queue.Full:
			self.dropped += 1
			self._full.failed("full", f"shot {shot_id(shot.shot_date, shot.shot_number)} not stored")
		else:
			self._full.ended()

	def run(self):
		"""Store queued shots until finish() has been called and the queue is empty."""
		while True:
			try:
				shot = self._queue.get(timeout=0.5)
			except queue.Empty:
				if self._finished.is_set():
					return
				continue
			self._store(shot)

	def finish(self):
		"""End run() once the queue is empty; call it after closing the subscription."""
		self._finished.set()

	def queued(self):
		return self._queue.qsize()

	@property
	def not_received(self):
		return self._gaps.total

	def _store(self, shot):
		sid = shot_id(shot.shot_date, shot.shot_number)
		before = self._gaps.total
		gap = self._gaps.update(shot.shot_date, shot.shot_number) - before
		if gap:
			# Never published by the IOC, published while this monitor was down, or dropped here (queue full).
			log.warning("%d shots not received, %s to %s", gap, shot_id(shot.shot_date, shot.shot_number - gap),
			            shot_id(shot.shot_date, shot.shot_number - 1))
		try:
			stored = store_shot(self.data_dir, shot)
		except Exception as e:
			self.failed += 1
			self._writes.failed(e, f"shot {sid} not stored", exc_info=True)
			return
		self._writes.ended()
		if stored:
			self.stored += 1
			log.info("shot %s stored as %s", sid, shot.shot_time)
		else:
			self.duplicates += 1
			log.info("shot %s already stored", sid)

	def summary(self):
		return (f"{self.stored} shots stored, {self.duplicates} already stored, {self.not_received} not received "
		        f"(gaps in shot_number), {self.dropped} dropped here with the queue full (also in the gaps), "
		        f"{self.failed} failed writes, {self.unreadable} unreadable updates")


def main(argv=None):
	parser = argparse.ArgumentParser(prog="interf-store", description=__doc__.split(": ", 1)[1])
	parser.add_argument("--prefix", required=True, help="the IOC module's PV prefix (its config's prefix); the group is <prefix>:SHOT")
	parser.add_argument("--data-dir", type=Path, default=DATA_DIR,
	                    help=f"directory of the daily files (default: $INTERF_DATA_DIR or ~/data/interferometer, now {DATA_DIR})")
	args = parser.parse_args(argv)
	logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)  # under systemd the journal keeps it
	args.data_dir.mkdir(parents=True, exist_ok=True)
	from p4p.client.thread import Context  # the clients extra; imported here so the pure parts load without it

	# A plain flag: Event.set() in a handler can deadlock with the main thread holding the Event's lock in wait().
	stopping = []
	for signum in (signal.SIGTERM, signal.SIGINT):
		signal.signal(signum, lambda signum, frame: stopping.append(signum))
	group = f"{args.prefix}:SHOT"
	writer = ShotWriter(args.data_dir, group)
	thread = threading.Thread(target=writer.run, name="interf_store writer", daemon=True)  # daemon: a hung write cannot block exit
	thread.start()
	ctx = Context("pva")  # EPICS_PVA_* from the environment
	try:
		subscription = ctx.monitor(group, writer.on_update, notify_disconnect=True)
		log.info("monitoring %s, storing in %s", group, args.data_dir)
		while not stopping:
			time.sleep(0.5)
		subscription.close()
	finally:
		ctx.close()
		writer.finish()
		thread.join(STOP_TIMEOUT_S)
		if thread.is_alive():
			log.error("writer still busy after %.0f s; %d queued shots not stored", STOP_TIMEOUT_S, writer.queued())
		log.info("stopped: %s", writer.summary())
	return 1 if thread.is_alive() else 0


if __name__ == "__main__":
	sys.exit(main())
