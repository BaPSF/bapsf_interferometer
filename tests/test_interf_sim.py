"""interf_sim --loop on the recorded .trc files in trc_replay.TRC_DIR (test-on-Bobby); skipped where they are absent."""
import datetime
import re
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from lab_scopes.io.lecroy_files import TRACE_DATA_OFFSET
from lab_scopes.lecroy import LeCroyWavedesc, wavedesc_trigger_timestamp
from lab_scopes.lecroy.wavedesc import WAVEDESC_FMT

import interf_payload
from diag_ioc.link import LinkListener
from interf_shot import SHOT_ZONE, ShotCounter, local_time, shot_date
from interf_sim.trc_replay import (MACHINE_PERIOD_S, TRC_DIR, ReplayLeCroy, _wavedesc, loop_offset, shift_trigger,
                                   trc_shots, trc_trigger_time)
from ioc_harness import REPO, wait_for

RECORDED = TRC_DIR.is_dir() and any(TRC_DIR.glob("C*-interf-shot*.trc"))
NEEDS_RECORDINGS = f"needs the recorded .trc files in {TRC_DIR} (interf_sim.trc_replay.TRC_DIR)"


def _midnight_after(t, days=1):
	"""Unix s of the Los Angeles midnight `days` days after the date of `t`."""
	day = local_time(t).date() + datetime.timedelta(days=days)
	return datetime.datetime.combine(day, datetime.time(), tzinfo=SHOT_ZONE).timestamp()


def _trigger(wavedesc):
	return wavedesc_trigger_timestamp(LeCroyWavedesc(wavedesc).wd)


class ReplayLoopTests(unittest.TestCase):
	"""ReplayLeCroy's capture order alone: no file is read."""
	SHOTS = [(10, {}), (11, {}), (12, {})]

	def _capture(self, lecroy):
		lecroy.set_mode("SINGLE")
		lecroy.update()
		return lecroy.counter, lecroy.offset_s

	def test_a_loop_serves_every_shot_each_pass_with_the_pass_offset(self):
		lecroy = ReplayLeCroy(self.SHOTS, loop_offset_s=100.0)
		captures = [self._capture(lecroy) for _ in range(7)]
		self.assertEqual(captures, [(10, 0.0), (11, 0.0), (12, 0.0), (10, 100.0), (11, 100.0), (12, 100.0), (10, 200.0)])
		self.assertFalse(lecroy.exhausted)

	def test_without_a_loop_the_shots_run_out_once(self):
		lecroy = ReplayLeCroy(self.SHOTS)
		calls = []
		lecroy.on_exhausted = lambda: calls.append(1)
		self.assertEqual([self._capture(lecroy) for _ in range(3)], [(10, 0.0), (11, 0.0), (12, 0.0)])
		self.assertTrue(lecroy.exhausted)
		lecroy.set_mode("SINGLE")
		lecroy.update()
		lecroy.update()
		self.assertEqual(calls, [1])


@unittest.skipUnless(RECORDED, NEEDS_RECORDINGS)
class RecordedTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.shots = trc_shots(TRC_DIR)
		path = next(iter(cls.shots[0][1].values()))
		with open(path, "rb") as f:
			cls.wavedesc = _wavedesc(f.read(TRACE_DATA_OFFSET), path)
		cls.times = [trc_trigger_time(files) for _, files in cls.shots[:3]]

	def test_shift_moves_only_the_trigger_time_across_midnight(self):
		t = _trigger(self.wavedesc)
		offset = _midnight_after(t) + 2.5 - t
		shifted = shift_trigger(self.wavedesc, offset)
		self.assertAlmostEqual(_trigger(shifted), t + offset, places=5)
		self.assertEqual(shot_date(_trigger(shifted)), shot_date(_midnight_after(t) + 2.5))
		before, after = LeCroyWavedesc(self.wavedesc).wd, LeCroyWavedesc(shifted).wd
		for name in before._fields:
			if not name.startswith("tt_"):
				self.assertEqual(getattr(after, name), getattr(before, name), name)

	def test_a_wavedesc_without_trigger_time_is_unchanged(self):
		unset = struct.pack(WAVEDESC_FMT, *LeCroyWavedesc(self.wavedesc).wd._replace(tt_year=0))
		self.assertEqual(shift_trigger(unset, 100.0), unset)

	def test_the_offset_defaults_to_the_span_plus_the_mean_spacing_and_must_exceed_the_span(self):
		self.assertEqual(len(self.times), 3, "needs 3 recorded shots")
		span = self.times[2] - self.times[0]
		self.assertAlmostEqual(loop_offset(self.shots[:3]), 1.5 * span)  # 3 shots: span + span / 2
		self.assertEqual(loop_offset(self.shots[:1]), MACHINE_PERIOD_S)
		self.assertEqual(loop_offset(self.shots[:3], span + 1.0), span + 1.0)
		with self.assertRaisesRegex(ValueError, "must exceed"):
			loop_offset(self.shots[:3], span)

	def test_conflicting_options_are_refused(self):
		span = self.times[2] - self.times[0]
		with tempfile.TemporaryDirectory() as tmp:
			raw = Path(tmp) / "raw.bp"
			cases = {
				"--loop replaces --repeat-traces": ["--loop", "--repeat-traces", "--limit", "3"],
				"--loop never writes raw output": ["--loop", "--raw-output", str(raw)],
				"--loop-offset needs --loop": ["--loop-offset", "10"],
				"must exceed the shots' trigger-time span": ["--loop", "--limit", "3", "--loop-offset", repr(span)],
			}
			for message, args in cases.items():
				with self.subTest(message):
					done = subprocess.run([sys.executable, "-m", "interf_sim", "--trc-dir", str(TRC_DIR), *args], cwd=REPO,
					                      capture_output=True, text=True, timeout=120)
					self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
					self.assertIn(message, done.stderr)
			self.assertFalse(raw.exists())

	def test_a_loop_sends_new_shots_until_sigterm(self):
		if not hasattr(signal, "setitimer"):
			self.skipTest("interf_main runs on Linux only")
		times = self.times
		self.assertEqual(len(times), 3, "needs 3 recorded shots")
		self.assertLess(times[2] - times[0], 86400.0, "the first 3 recorded shots must lie within a day")
		# Pass 1 crosses a Los Angeles midnight between its 2nd and 3rd shot: the date advances, numbering restarts at 0.
		offset = _midnight_after(times[2], days=2) - (times[1] + times[2]) / 2
		received, lock = [], threading.Lock()

		def on_message(variables):
			shot = interf_payload.decode(variables)
			with lock:
				received.append((shot.shot_date, shot.shot_number, shot.shot_time))

		with tempfile.TemporaryDirectory() as tmp, LinkListener("tcp:127.0.0.1:0", on_message).start() as listener:
			out_path = Path(tmp) / "interf_sim.out"
			with open(out_path, "w") as out:  # a file, not a pipe, so a long log can never block the replay
				sim = subprocess.Popen([sys.executable, "-m", "interf_sim", "--trc-dir", str(TRC_DIR), "--limit", "3",
				                        "--period", "0.5", "--loop", "--loop-offset", repr(offset), "--ioc-link",
				                        listener.address], cwd=REPO, stdout=out, stderr=subprocess.STDOUT)
				try:
					deadline = time.monotonic() + 120
					while len(received) < 8 and time.monotonic() < deadline and sim.poll() is None:
						time.sleep(0.1)
					sim.send_signal(signal.SIGTERM)
					sim.wait(60)
				finally:
					if sim.poll() is None:
						sim.kill()
						sim.wait()
			output = out_path.read_text()
			tail = output[-5000:]
			self.assertEqual(sim.returncode, 0, tail)
			summary = re.search(r"IOC link: sent (\d+), dropped (\d+), failed (\d+)", output)
			self.assertIsNotNone(summary, tail)
			self.assertEqual(summary.group(2, 3), ("0", "0"), tail)
			# close() delivered the queue before the summary; the listener may still be reading the last message.
			wait_for(lambda: len(received) >= int(summary[1]), 30, f"{summary[1]} shots at the listener")
		self.assertGreaterEqual(len(received), 8, tail)
		self.assertEqual(len(received), int(summary[1]))
		expected_times = [times[i % 3] + (i // 3) * offset for i in range(len(received))]
		counter = ShotCounter(None)  # the replay's: in memory, numbering from 0
		expected_ids = [counter.next(t) for t in expected_times]
		for i, (date, number, shot_time) in enumerate(received):
			self.assertAlmostEqual(shot_time, expected_times[i], places=5, msg=i)
			self.assertEqual((date, number), expected_ids[i], i)
		self.assertEqual(expected_ids[5][1], 0)  # pass 1's 3rd shot: the new date's first
		self.assertNotEqual(expected_ids[5][0], expected_ids[4][0])
		self.assertEqual(received, sorted(received))


if __name__ == "__main__":
	unittest.main()
