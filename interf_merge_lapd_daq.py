# coding utf-8
'''
Merge interferometer data into LAPD_DAQ-format datarun hdf5 files.

Applies to any run acquired with LeCroy scopes through the LAPD_DAQ repo --
Data_Run.py (grid / stationary) and Data_Run_bmotion.py alike: both write shots
through the same acquisition/hdf5_writer code, so the per-scope layout read
here is identical. (Old-format dataruns, with a "data run sequence" group, are
handled by interf_merge_datarun.py instead.)

Author: Jia Han
Created: 2026-07-06

Merges TWO traces per interferometer channel into the datarun file: among the
interferometer traces collected strictly WITHIN the run's duration (first shot
trigger .. last shot trigger), the one closest to the first shot is saved as
shot_1 and the one closest to the last shot as the last shot number. This
guarantees the merged traces were acquired while the run was in progress
without requiring the two clocks to agree tightly, and is enough to
sanity-check plasma density at the start and end of a run (same spirit as the
interf_merge_datarun.py default).

If no trace falls inside the run window, the merge (when run interactively)
prompts to retry with a padded window; non-interactive/batch callers pass
pad_seconds instead. The interferometer time actually saved for each shot is
recorded as attributes on every merged dataset and printed to the terminal.

TODO (future, if needed): shot-to-shot matching for every shot. The
building blocks are already here -- get_shot_timestamps_lapd_daq() returns
every shot's trigger time, and estimate_clock_offset() robustly estimates the
constant datarun-vs-interferometer clock offset so a tight per-shot tolerance
can be used. It was left out for now because the offset is only identifiable
modulo the shot period (both systems trigger off the same periodic LAPD edge),
which makes fully-automatic per-shot pairing ambiguous when the clocks are
badly out of sync; resolve that before enabling it (e.g. require the estimated
offset to be well under half the cadence, or take an operator-supplied offset).

The old datarun format (handled by interf_merge_datarun.py) carried a "data run
sequence" log whose SIS rows gave per-shot timestamps. The new acquisition
(LAPD_DAQ repo, spooled; Data_Run.py and Data_Run_bmotion.py) writes a
different layout:

	/<scope_name>/                 attrs: description, ip_address, scope_type
		time_array                 float64 seconds
		shot_<n>/                  attrs: acquisition_time [, skipped, skip_reason]
			C<k>_data              int16, (N,) or (n_segments, N) in sequence mode
			C<k>_header            346-byte LeCroy WAVEDESC (np.void)

Per-shot timestamps for matching:
- Primary: the trigger time embedded in each stored C*_header WAVEDESC
  (tt_year..tt_second fields). This is stamped by the scope at the actual
  trigger, has sub-second resolution, and is immune to how far behind the
  offload process was when it wrote the shot into the HDF5.
- Fallback: the shot group's 'acquisition_time' attr (ctime string, 1 s
  resolution). In runs written before the spool acquire-time fix this attr is
  the offload *write* time and can lag the real shot, so it is only used when
  the WAVEDESC cannot be decoded (e.g. skipped shots have no header).

The scope RTC fields are wall-clock local time, so they are converted with
time.mktime (local), matching the interferometer dataset names which are
seconds since epoch from the interferometer machine's clock.

Note the strict-window rule interacts with clock mis-sync: if the
interferometer clock runs AHEAD of the DAQ clock by dt, the trace belonging to
the last shot lands at (t_last + dt), just outside the window, so the trace
saved for the last shot is the in-window one closest to it (typically the
previous shot's trace). The recorded 'time difference from shot trigger (s)'
attribute makes this visible per merged dataset.

Sequence-mode shots: one shot_<n> group holds n_segments plasma triggers, but
only the WAVEDESC time of the sequence (= first segment trigger) is stored, so
a sequence shot is matched to the single interferometer trace nearest its
first segment ("first segment only").

Functions in this script:

get_shot_timestamps_lapd_daq(datarun_path, verbose=True)
	- Returns (shot_numbers, timestamps, sources, reference_scope) for the
	  non-skipped shots of one scope (the first scope whose headers decode).
estimate_clock_offset(shot_timestamps, interf_timestamps, ...)
	- Robust (median + one refinement pass) constant clock-offset estimate.
	  Diagnostic utility / building block for the shot-to-shot TODO; not used
	  by the first+last merge.
merge_interferometer_data_lapd_daq(datarun_path, interf_path, ...)
	- Merges the traces nearest the first and last shots (strict run window,
	  optional padded retry) into the datarun file under
	  diagnostics/interferometer/<group>/<shot_number>, same layout as
	  interf_merge_datarun.py produces for old-format files.
merge_folder_lapd_daq(datarun_dir, interf_dir)
	- Batch wrapper over a folder of datarun files; pairs each with
	  interferometer_data_<date>.hdf5 (date from filename or ctime, with
	  prev/next-day fallback) and logs progress to interf_merge_log.txt.
	  Old-format dataruns are skipped with a pointer to interf_merge_datarun.
	  Batch mode never prompts; pass pad_seconds to widen the window.

How to access the merged data: identical to old-format merges -- see the
"How to access interferometer data in datarun file" section in
interf_merge_datarun.py. Which interferometer times were saved is noted:
- on every merged dataset: 'interferometer timestamp (s since epoch)',
  'interferometer time (local)', 'time difference from shot trigger (s)';
- on diagnostics/interferometer: 'timestamp source', 'reference scope',
  'run window (s since epoch)', 'window pad applied (s)',
  'merged shot numbers', 'merged interferometer timestamps (s since epoch)'.
'''

import os
import re
import bisect
import contextlib
import datetime
import time

import h5py
import numpy as np

from lab_scopes.lecroy import LeCroyWavedesc
from interf_merge_datarun import (
	init_datarun_groups,
	_available_groups,
	_candidate_interf_files,
	_copy_shot_datasets,
	_normalize_interf_paths,
	_open_interf_index,
)

#===============================================================================================================================================
# New-format datarun reading
#===============================================================================================================================================

# Root groups that are never scope groups. 'diagnostics' is where this script
# writes its own output, so it must be excluded when re-running on a file.
NON_SCOPE_GROUPS = {"Configuration", "Control", "diagnostics"}

_SHOT_RE = re.compile(r'^shot_(\d+)$')

_CTIME_FMT = "%a %b %d %H:%M:%S %Y"  # what time.ctime() produces


def _scope_groups(f):
	'''Return the datarun's scope group names (root groups with shot_* children).'''
	names = []
	for name, g in f.items():
		if name in NON_SCOPE_GROUPS or not hasattr(g, 'keys'):
			continue
		if any(_SHOT_RE.match(k) for k in g.keys()):
			names.append(name)
	return names


def _shot_numbers(scope_group):
	'''Sorted shot numbers (ints) present in a scope group.'''
	nums = []
	for k in scope_group.keys():
		m = _SHOT_RE.match(k)
		if m:
			nums.append(int(m.group(1)))
	return sorted(nums)


def _wavedesc_epoch_local(wd):
	'''
	Convert a WAVEDESC's trigger-time fields to seconds since epoch.

	The scope RTC runs on wall-clock local time, so the fields are interpreted
	with time.mktime (local timezone, DST resolved automatically). This keeps
	the value directly comparable to the interferometer dataset names (epoch
	seconds), leaving only genuine clock mis-sync as the difference.
	(lab_scopes' wavedesc_trigger_timestamp uses UTC on purpose -- it is meant
	only for same-shot differences between scopes -- so it is not used here.)

	Returns float epoch seconds, or None if the timestamp fields are unset.
	'''
	try:
		if int(wd.tt_year) <= 0:
			return None
		sec = float(wd.tt_second)
		whole = int(sec)
		frac = sec - whole
		t = time.mktime((int(wd.tt_year), int(wd.tt_months), int(wd.tt_days),
		                 int(wd.tt_hours), int(wd.tt_minute), whole, 0, 0, -1))
		return float(t) + frac
	except Exception:
		return None


def _shot_trigger_timestamp(shot_group):
	'''
	Best-effort timestamp for one shot_<n> group.

	Returns (timestamp, source) where source is 'wavedesc' or 'acquisition_time',
	or (None, None) if neither is available. Skipped shots carry no header, so
	they land on the acquisition_time fallback (and are flagged as such).
	'''
	for key in sorted(shot_group.keys()):
		if not key.endswith('_header'):
			continue
		try:
			raw = bytes(shot_group[key][()])
			ts = _wavedesc_epoch_local(LeCroyWavedesc(raw).wd)
		except Exception:
			ts = None
		if ts is not None:
			return ts, 'wavedesc'

	ctime_str = shot_group.attrs.get('acquisition_time')
	if ctime_str is not None:
		if isinstance(ctime_str, bytes):
			ctime_str = ctime_str.decode('utf-8', 'replace')
		try:
			return time.mktime(time.strptime(str(ctime_str), _CTIME_FMT)), 'acquisition_time'
		except ValueError:
			pass
	return None, None


def get_shot_timestamps_lapd_daq(datarun_path, verbose=True):
	'''
	Get shot numbers and trigger timestamps from a LAPD_DAQ-format datarun file.

	One scope is used as the timestamp reference: the first scope (file order)
	for which at least one WAVEDESC decodes. All scopes trigger off the same
	edge, so any scope's trigger times identify the plasma shots.

	Skipped shots (attrs['skipped']) are excluded -- they have no scope data and
	their acquisition_time may be an offload-lagged stamp.

	Parameters:
	datarun_path (str): Path to the datarun hdf5 file.
	verbose (bool): Print the reference scope and shot count.

	Returns:
	(numpy.ndarray, numpy.ndarray, list, str): parallel arrays of shot numbers
		(int) and timestamps (epoch seconds, float), the per-shot timestamp
		source ('wavedesc' or 'acquisition_time'), and the reference scope name.
	'''
	with h5py.File(datarun_path, 'r') as f:
		scopes = _scope_groups(f)
		if not scopes:
			raise ValueError(f"No scope groups with shot_* found in {datarun_path} "
			                 "(is this an old-format datarun? use interf_merge_datarun.py)")

		best = None  # (shots, timestamps, sources, scope_name)
		for scope_name in scopes:
			sg = f[scope_name]
			shots, stamps, sources = [], [], []
			for n in _shot_numbers(sg):
				shot = sg[f'shot_{n}']
				if shot.attrs.get('skipped', False):
					continue
				ts, source = _shot_trigger_timestamp(shot)
				if ts is None:
					continue
				shots.append(n)
				stamps.append(ts)
				sources.append(source)
			if not shots:
				continue
			if 'wavedesc' in sources:
				best = (shots, stamps, sources, scope_name)
				break
			if best is None:
				best = (shots, stamps, sources, scope_name)

	if best is None:
		raise ValueError(f"No usable shot timestamps in {datarun_path}")

	shots, stamps, sources, scope_name = best
	if verbose:
		n_fallback = sum(1 for s in sources if s != 'wavedesc')
		print(f"Timestamps from scope '{scope_name}': {len(shots)} shots "
		      f"({len(shots) - n_fallback} WAVEDESC, {n_fallback} acquisition_time fallback)")
		if n_fallback:
			print("Warning: acquisition_time-fallback shots may carry the offload write "
			      "time (pre-fix runs) and can fail to match.")
	return (np.array(shots, dtype=int), np.array(stamps, dtype=float),
	        sources, scope_name)


#===============================================================================================================================================
# Clock-offset estimation (diagnostic utility; building block for the
# shot-to-shot matching TODO -- not used by the first+last merge below)
#===============================================================================================================================================

def _nearest_delta(sorted_arr, t, window):
	'''Signed delta (nearest - t) to the closest value in sorted_arr, or None
	if the closest value is farther than window.'''
	if len(sorted_arr) == 0:
		return None
	idx = bisect.bisect_left(sorted_arr, t)
	best = None
	for j in (idx - 1, idx):
		if 0 <= j < len(sorted_arr):
			d = sorted_arr[j] - t
			if best is None or abs(d) < abs(best):
				best = d
	if best is not None and abs(best) <= window:
		return best
	return None


def estimate_clock_offset(shot_timestamps, interf_timestamps,
                          max_search=60.0, tolerance=1.0, verbose=True):
	'''
	Estimate the constant clock offset between datarun and interferometer stamps.

	offset is defined so that (shot_timestamp + offset) lines up with the
	interferometer timestamps. Method: median of nearest-neighbor deltas within
	max_search, then one refinement pass with a narrower window around the first
	estimate to shed wrong-neighbor pairings.

	Both stamps also carry small systematic lags (scope trigger stamp vs the
	interferometer trace's file-write mtime); those are ~constant over a run and
	are absorbed into the same offset.

	Note both trigger trains are periodic with the same rep rate, so the offset
	is only identifiable modulo the shot period: a true mis-sync larger than
	half the inter-trace interval aliases onto a neighboring shot. A warning is
	printed when the estimate exceeds ~40% of the interferometer cadence. This
	ambiguity is why per-shot matching is left as a TODO.

	Parameters:
	shot_timestamps (array): datarun shot epoch seconds.
	interf_timestamps (array): interferometer trace epoch seconds (need not be sorted).
	max_search (float): widest plausible clock mis-sync in seconds.
	tolerance (float): the matching tolerance a caller would use; the fit is
		flagged when its spread exceeds this.
	verbose (bool): print the estimate and warnings.

	Returns:
	(float, int, float): (offset seconds, number of shots used, median absolute
		deviation of the refined deltas). offset is 0.0 with n=0 if nothing
		matched within max_search.
	'''
	interf_sorted = np.sort(np.asarray(interf_timestamps, dtype=float))
	shot_ts = np.asarray(shot_timestamps, dtype=float)

	deltas = [d for t in shot_ts
	          if (d := _nearest_delta(interf_sorted, t, max_search)) is not None]
	if not deltas:
		if verbose:
			print(f"Warning: no interferometer trace within {max_search:.0f} s of any "
			      "shot; clock offset assumed 0.")
		return 0.0, 0, float('nan')

	offset = float(np.median(deltas))

	# Refinement: re-pair around the first estimate with a narrow window so a
	# neighbor one shot-period away can no longer pull the median.
	refine_window = max(3.0 * tolerance, 3.0)
	deltas2 = [d for t in shot_ts
	           if (d := _nearest_delta(interf_sorted, t + offset, refine_window)) is not None]
	if deltas2:
		offset += float(np.median(deltas2))
	used = np.asarray(deltas2 or deltas)
	mad = float(np.median(np.abs(used - np.median(used))))
	n_used = len(used)

	if verbose:
		print(f"Estimated clock offset: {offset:+.3f} s "
		      f"(from {n_used} shots, MAD {mad:.3f} s)")
		if n_used < 5:
			print("Warning: offset estimated from fewer than 5 shots -- low confidence.")
		if mad > tolerance:
			print(f"Warning: residual spread ({mad:.3f} s) exceeds the match tolerance "
			      f"({tolerance:.3f} s); the clock lag may not be constant over this run.")
		# Aliasing guard: with two periodic trigger trains the offset is only
		# determined modulo the shot period, so an estimate near half the
		# interferometer cadence may really be pairing neighboring shots.
		if len(interf_sorted) > 1:
			cadence = float(np.median(np.diff(interf_sorted)))
			if cadence > 0 and abs(offset) > 0.4 * cadence:
				print(f"Warning: |offset| ({abs(offset):.2f} s) is a large fraction of "
				      f"the interferometer cadence ({cadence:.2f} s); the true mis-sync "
				      f"may differ by a multiple of the shot period, i.e. shots could be "
				      f"paired with a neighboring shot's trace. Verify one shot manually.")
	return offset, n_used, mad


#===============================================================================================================================================
# Merge (first + last shot only, traces selected within the run window)
#===============================================================================================================================================

def _fmt_local(ts):
	'''Epoch seconds -> "YYYY-MM-DD HH:MM:SS" local wall time.'''
	return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _prompt_pad(default_pad):
	'''
	Terminal prompt asking how to resolve an empty run window.

	The user must choose: continue with the default pad, continue with a
	custom pad (type a number of seconds), or stop here (merge nothing).
	Re-asks on unrecognized input. Returns the pad in seconds, or None to
	stop. If no terminal input is available (piped/unattended), stops.
	'''
	print("Resolution required:")
	print(f"  c          continue: retry with the window padded by +/-{default_pad:.0f} s")
	print("  <number>   continue: retry with a custom pad (seconds per side)")
	print("  s          stop here: merge nothing for this datarun")
	while True:
		try:
			ans = input("Continue with pads or stop here? [c / <number> / s]: ").strip().lower()
		except (EOFError, OSError):
			print("No terminal input available; stopping here (nothing merged).")
			return None
		if ans in ('s', 'stop', 'n', 'no', 'q', 'quit'):
			return None
		if ans in ('c', 'continue', 'y', 'yes'):
			return float(default_pad)
		try:
			return float(ans)
		except ValueError:
			print(f"Unrecognized answer {ans!r} -- type 'c', a number of seconds, or 's'.")


def merge_interferometer_data_lapd_daq(datarun_path, interf_path,
                                      pad_seconds=None, interactive=True,
                                      verbose=True):
	'''
	Merge interferometer traces for the first and last shots of a LAPD_DAQ run.

	Call init_datarun_groups(datarun_path, interf_path) first (reused unchanged
	from interf_merge_datarun -- it only creates groups and copies attributes).

	Selection rule: only interferometer traces acquired strictly within the
	run's duration (first shot trigger .. last shot trigger) are considered.
	Among them, the trace closest in time to the first shot is saved under the
	first shot's number and the trace closest to the last shot under the last
	shot's number. No timestamp tolerance is required, so ordinary clock
	mis-sync cannot make the merge come back empty as long as the two systems
	overlapped in time. If both selections land on the same trace (e.g. a
	single candidate), it is written once, under the shot it is closer to.

	If the strict window contains no trace:
	- with pad_seconds given, the window is widened by that many seconds on
	  each side and the selection retried once;
	- otherwise, when `interactive`, a terminal prompt asks for a resolution:
	  continue with a padded window (default pad = one inter-shot period, at
	  least 10 s; or type a custom number of seconds) or stop here (merge
	  nothing). The prompt re-asks on unrecognized input;
	- otherwise the merge returns 0.

	The interferometer time saved for each shot is printed and recorded as
	dataset attributes ('interferometer timestamp (s since epoch)',
	'interferometer time (local)', 'time difference from shot trigger (s)').

	TODO: shot-to-shot matching for every shot (see the module docstring and
	estimate_clock_offset for the intended approach and its caveat).

	Parameters:
	datarun_path (str): Path to the datarun hdf5 file.
	interf_path (str | list[str]): One or more interferometer hdf5 files (a run
		spanning midnight passes both days' files).
	pad_seconds (float | None): Widen the run window by this many seconds per
		side if the strict window is empty. None = strict only (prompt if
		interactive).
	interactive (bool): Allow the terminal prompt on strict-window failure.
		Set False for unattended/batch use.
	verbose (bool): Progress printing (the chosen interferometer times are
		printed regardless, unless verbose is False).

	Returns:
	int: Number of shots whose data was written (0, 1, or 2).
	'''
	def _log(msg):
		if verbose:
			print(msg)

	shot_numbers, timestamp_array, sources, ref_scope = \
		get_shot_timestamps_lapd_daq(datarun_path, verbose=verbose)

	t_first = float(timestamp_array[0])
	t_last = float(timestamp_array[-1])
	shot_first = int(shot_numbers[0])
	shot_last = int(shot_numbers[-1])

	interf_paths = _normalize_interf_paths(interf_path)

	shots_written = 0
	with contextlib.ExitStack() as stack:
		f_interfs, all_pairs, sorted_floats, per_file_groups = \
			_open_interf_index(stack, interf_paths)

		def window_selection(pad):
			'''Indices into all_pairs of the traces (within the padded window)
			closest to the first and last shot, or None if the window is empty.'''
			lo = bisect.bisect_left(sorted_floats, t_first - pad)
			hi = bisect.bisect_right(sorted_floats, t_last + pad)
			if lo >= hi:
				return None
			i_first = min(range(lo, hi), key=lambda j: abs(sorted_floats[j] - t_first))
			i_last = min(range(lo, hi), key=lambda j: abs(sorted_floats[j] - t_last))
			return i_first, i_last, hi - lo

		_log(f"Run window: {_fmt_local(t_first)} -> {_fmt_local(t_last)} "
		     f"({t_last - t_first:.1f} s, shots {shot_first}..{shot_last})")

		pad_applied = 0.0
		selection = window_selection(0.0)
		if selection is None:
			retry_pad = None
			if pad_seconds is not None:
				_log("No interferometer trace acquired within the run window.")
				retry_pad = float(pad_seconds)
			elif interactive:
				# The prompt must be self-explanatory even with verbose=False,
				# so its context prints unconditionally here.
				print(f"\nNo interferometer trace was acquired within the run "
				      f"window {_fmt_local(t_first)} -> {_fmt_local(t_last)} "
				      f"of {os.path.basename(datarun_path)}.")
				if len(timestamp_array) > 1:
					period = float(np.median(np.diff(timestamp_array)))
				else:
					period = 0.0
				retry_pad = _prompt_pad(max(period, 10.0))
				if retry_pad is None:
					print("Stopped here: nothing merged for this datarun.")
					return 0
			else:
				_log("No interferometer trace acquired within the run window; "
				     "nothing merged (non-interactive, no pad_seconds).")
			if retry_pad is not None:
				_log(f"Retrying with window padded by +/-{retry_pad:g} s.")
				pad_applied = float(retry_pad)
				selection = window_selection(pad_applied)
			if selection is None:
				_log("Still no interferometer trace found; nothing merged.")
				return 0

		i_first, i_last, n_candidates = selection
		_log(f"{n_candidates} interferometer trace(s) inside window"
		     + (f" (pad {pad_applied:g} s)" if pad_applied else ""))

		# (shot_number, shot_trigger_time, trace_time, trace_name, file_idx);
		# if both ends picked the same trace, keep it once, under the closer shot.
		if i_first == i_last:
			ts = sorted_floats[i_first]
			if abs(ts - t_first) <= abs(ts - t_last):
				picks = [(shot_first, t_first) + all_pairs[i_first]]
			else:
				picks = [(shot_last, t_last) + all_pairs[i_last]]
			_log("Only one distinct trace selected; writing it for the closer shot.")
		else:
			picks = [(shot_first, t_first) + all_pairs[i_first],
			         (shot_last, t_last) + all_pairs[i_last]]

		for shot_num, shot_ts, trace_ts, trace_name, _fidx in picks:
			_log(f"Shot {shot_num} <- interferometer trace {trace_name} "
			     f"({_fmt_local(trace_ts)}, {trace_ts - shot_ts:+.3f} s from shot trigger)")

		available_groups = _available_groups(datarun_path, per_file_groups)

		# Provenance: how the selection was made, recorded once per merge.
		with h5py.File(datarun_path, "a") as f_datarun:
			parent = f_datarun.require_group("diagnostics/interferometer")
			parent.attrs['timestamp source'] = (
				"Shot times: WAVEDESC trigger time from stored scope headers (scope "
				"RTC, local time); 'acquisition_time' attr fallback for undecodable "
				"headers. Interferometer traces acquired within the run window were "
				"selected: the one closest to the first shot (saved under the first "
				"shot number) and the one closest to the last shot (saved under the "
				"last shot number). Sequence-mode shots are timed by their first "
				"segment only. See each dataset's attributes for the exact "
				"interferometer time saved.")
			parent.attrs['reference scope'] = ref_scope
			parent.attrs['run window (s since epoch)'] = np.array([t_first, t_last])
			parent.attrs['window pad applied (s)'] = pad_applied
			parent.attrs['merged shot numbers'] = np.array(
				[p[0] for p in picks], dtype=int)
			parent.attrs['merged interferometer timestamps (s since epoch)'] = np.array(
				[p[2] for p in picks])

		for shot_num, shot_ts, trace_ts, trace_name, file_idx in picks:
			# Clearly note which interferometer time this data is from.
			provenance = {
				'interferometer timestamp (s since epoch)': float(trace_ts),
				'interferometer time (local)': _fmt_local(trace_ts),
				'time difference from shot trigger (s)': float(trace_ts - shot_ts),
			}
			if _copy_shot_datasets(datarun_path, f_interfs[file_idx],
			                       per_file_groups[file_idx], available_groups,
			                       trace_name, str(shot_num),
			                       extra_attrs=provenance):
				shots_written += 1

			_log(f"Shot {shot_num} wrote into datarun file")

	_log(f'Interferometer data merged into datarun file ({shots_written} shots written).')
	return shots_written


#===============================================================================================================================================
# Batch wrapper
#===============================================================================================================================================

def _is_lapd_daq_format(datarun_path):
	'''True if the file has at least one root scope group with shot_* children.'''
	try:
		with h5py.File(datarun_path, 'r') as f:
			return bool(_scope_groups(f))
	except OSError:
		return False


def _merged_times_note(datarun_path):
	'''Short "shot@time" summary of what a merge just wrote, from the
	provenance attrs (for batch log lines). Empty string if unavailable.'''
	try:
		with h5py.File(datarun_path, "r") as f:
			parent = f["diagnostics/interferometer"]
			shots = parent.attrs['merged shot numbers']
			stamps = parent.attrs['merged interferometer timestamps (s since epoch)']
		return ", ".join(f"shot {int(s)} @ {_fmt_local(t)}"
		                 for s, t in zip(shots, stamps))
	except Exception:
		return ""


def merge_folder_lapd_daq(datarun_dir, interf_dir, pad_seconds=None):
	'''
	Run the first+last interferometer merge for every LAPD_DAQ-format datarun
	hdf5 in a folder.

	Same pairing and logging scheme as interf_merge_datarun.merge_folder: each
	datarun is paired with interferometer_data_<date>.hdf5 (date from the
	filename or the file's ctime, with prev/next-day fallback), progress goes to
	the terminal and to interf_merge_log.txt in datarun_dir, and per-file errors
	don't abort the batch. Old-format dataruns are skipped with a pointer to
	interf_merge_datarun.py.

	Batch mode never prompts: a datarun whose strict run window contains no
	interferometer trace is reported EMPTY unless pad_seconds is given, in
	which case the padded retry is applied automatically. The interferometer
	times saved for each file are included in its OK log line.

	Returns:
	dict: {datarun_path: status_string} for each file processed.
	'''
	results = {}
	if not os.path.isdir(datarun_dir):
		print(f"Datarun directory not found: {datarun_dir}")
		return results
	if not os.path.isdir(interf_dir):
		print(f"Interferometer directory not found: {interf_dir}")
		return results

	datarun_files = sorted(f for f in os.listdir(datarun_dir)
	                       if f.lower().endswith('.hdf5')
	                       and not f.lower().startswith('interferometer_data_'))

	total = len(datarun_files)
	if total == 0:
		print(f"No .hdf5 datarun files found in {datarun_dir}")
		return results

	name_w = min(60, max((len(f) for f in datarun_files), default=20))
	idx_w = len(str(total))
	counts = {"ok": 0, "empty": 0, "skipped": 0, "error": 0}

	log_path = os.path.join(datarun_dir, "interf_merge_log.txt")
	try:
		log_file = open(log_path, "w", encoding="utf-8")
	except OSError as e:
		print(f"Warning: could not open log file {log_path}: {e}")
		log_file = None

	def emit(line):
		print(line)
		if log_file is not None:
			log_file.write(line + "\n")
			log_file.flush()

	emit(f"Batch merge (LAPD_DAQ, first+last shots) started at {datetime.datetime.now().isoformat(timespec='seconds')}")
	emit(f"Batch merge: {total} datarun file(s) from {datarun_dir}")
	emit(f"             interferometer files from {interf_dir}")
	emit(f"             pad_seconds={pad_seconds}")
	emit("-" * (idx_w * 2 + name_w + 30))

	try:
		for i, fname in enumerate(datarun_files, start=1):
			datarun_path = os.path.join(datarun_dir, fname)
			prefix = f"[{i:>{idx_w}}/{total}] {fname:<{name_w}}"

			if not _is_lapd_daq_format(datarun_path):
				emit(f"{prefix}  SKIP  not LAPD_DAQ format (old datarun? use interf_merge_datarun.py)")
				results[datarun_path] = "skipped: not LAPD_DAQ format"
				counts["skipped"] += 1
				continue

			candidates, date_source = _candidate_interf_files(datarun_path, interf_dir)
			if date_source is None:
				emit(f"{prefix}  SKIP  no date in filename or ctime")
				results[datarun_path] = "skipped: no date resolvable"
				counts["skipped"] += 1
				continue
			if not candidates:
				emit(f"{prefix}  SKIP  no interf file (date from {date_source})")
				results[datarun_path] = f"skipped: no interf file (date from {date_source})"
				counts["skipped"] += 1
				continue

			interf_name = os.path.basename(candidates[0])
			if len(candidates) > 1:
				interf_name = f"{interf_name} (+{len(candidates) - 1})"
			src_tag = " [ctime]" if date_source == "ctime" else ""

			try:
				init_datarun_groups(datarun_path, candidates, verbose=False)
				n_written = merge_interferometer_data_lapd_daq(
					datarun_path, candidates, pad_seconds=pad_seconds,
					interactive=False, verbose=False)
				if n_written == 0:
					emit(f"{prefix}  EMPTY {interf_name}{src_tag}  "
					     "(no interferometer trace in run window)")
					results[datarun_path] = f"empty ({interf_name})"
					counts["empty"] += 1
				else:
					note = _merged_times_note(datarun_path)
					emit(f"{prefix}  OK    {interf_name}{src_tag}  "
					     f"({n_written} shots: {note})")
					results[datarun_path] = f"ok: {n_written} shots ({interf_name})"
					counts["ok"] += 1
			except Exception as e:
				err = f"{type(e).__name__}: {e}"
				emit(f"{prefix}  ERROR {err}")
				results[datarun_path] = f"error: {err}"
				counts["error"] += 1

		emit("-" * (idx_w * 2 + name_w + 30))
		emit(f"Batch done: {counts['ok']} ok, {counts['empty']} empty, "
		     f"{counts['skipped']} skipped, {counts['error']} error "
		     f"(total {total})")
		emit(f"Batch merge finished at {datetime.datetime.now().isoformat(timespec='seconds')}")
	finally:
		if log_file is not None:
			log_file.close()
			print(f"Log written to {log_path}")
	return results


#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == '__main__':

	datarun_path = r"E:\Shadow data\Electrode_Biasing\jun2026\example_datarun.hdf5"
	interf_path = r"C:\data\LAPD\interferometer_samples\interferometer_data_2026-06-15.hdf5"

	init_datarun_groups(datarun_path, interf_path)

	merge_interferometer_data_lapd_daq(datarun_path, interf_path)
