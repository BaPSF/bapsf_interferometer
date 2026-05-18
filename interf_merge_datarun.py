# coding utf-8
'''
Merge the interferometer data from the available channels to datarun hdf5 file.
Author: Jia Han
Last update: 2026-05-18

Functions used in this script:

init_datarun_groups(datarun_path, interf_path, verbose=True)
	- Creates the interferometer groups in the datarun file.
	- Copies per-channel attributes (description, unit, microwave frequency,
	  calibration factor) from the source file onto each
	  diagnostics/interferometer/<name> subgroup.
get_shot_timestamps(datarun_path, verbose=True)
	- Parses the datarun sequence and returns parallel (shot_numbers,
	  timestamps) arrays for completed shots.
	- Filters strictly on SIS DAQ rows ("SIS crate" or "SIS 3302") so
	  sequences that also log bmotion (or other subsystem) rows per shot
	  do not produce duplicate timestamps.
	- Shot numbers come from the message itself, not positional index, so
	  the result tracks real shot identity even when the sequence has gaps.
merge_interferometer_data(datarun_path, interf_path, all_shots=False, verbose=True)
	- For each completed datarun shot, finds the matching interferometer
	  trace by timestamp (±1 s tolerance, binary-search lookup) and copies
	  it into the datarun file under diagnostics/interferometer/<group>/<shot>.
	- The interferometer file is opened via a context manager, so an
	  exception mid-merge cannot leak the file handle.
	- Defaults to merging only the first and last completed shots (quick
	  sanity check, small file). Pass all_shots=True to merge every shot.
	- Returns the number of shots actually written (0 if no matches).
merge_folder(datarun_dir, interf_dir, all_shots=False)
	- Batch wrapper: runs init_datarun_groups + merge_interferometer_data
	  for every .hdf5 file in datarun_dir.
	- Resolves the matching interferometer file per datarun by parsing
	  YYYY-MM-DD from the filename, with prev/next-day fallback. If the
	  filename has no date, falls back to the file's creation time
	  (real ctime on Windows).
	- Per-file errors are caught so one bad file does not abort the batch.
	- Prints one [i/N] progress line per file (OK / EMPTY / SKIP / ERROR)
	  and writes the same lines to interf_merge_log.txt in datarun_dir
	  (fixed filename, overwritten per run, flushed per line).
	- Returns {datarun_path: status_string}.
write_attribute(datarun_path)
	- Fallback that fills in attributes for any subgroup that has none.
	- Useful for source files predating 2024-06-07 which lack attrs.
	- Attributes include description, unit, microwave frequency, and calibration factor.


How to access interferometer data in datarun file:
- Data groups are under "diagnostics/interferometer/"
- As of 2026, the groups are "phase_p20", "phase_p29", "phase_p40", "time_array", and "time_array_p40".
- Older interferometer files only contain "phase_p20", "phase_p29", and "time_array"; this script handles both formats.
- Per-channel attributes (description, microwave frequency, calibration
  factor, unit) live on each subgroup, e.g.
  diagnostics/interferometer/phase_p29.attrs['Microwave frequency (Hz)'].
- Datasets under each group are named by the real shot number from the
  datarun sequence (starting from 1).
- If a shot number is absent, it means no interferometer data was matched
  for that shot (no SIS row, no timestamp match, or the run was skipped).
- phase_p40 datasets may carry per-shot attributes 'rigol_missing' (bool)
  and 'rigol_missing_reason' (str) when the Rigol scope was unavailable
  for that shot; the array is then a zero-filled placeholder.

How to use this script:

Single file:
- Set datarun_path and interf_path and call
    init_datarun_groups(datarun_path, interf_path)
    merge_interferometer_data(datarun_path, interf_path, all_shots=True)
- Timestamps are matched with ±1 s tolerance. If a same-day interferometer
  file does not contain the shots, try the previous or next day's file.
- Only shots whose timestamps match interferometer traces are copied.
- The default merges only the first and last shot. Pass all_shots=True
  to merge every shot.

Whole folder (batch):
- Call merge_folder(datarun_dir, interf_dir, all_shots=True).
- Each datarun .hdf5 is paired with interferometer_data_<date>.hdf5 by
  date (filename or ctime), with prev/next-day fallback.
- Progress prints to terminal and to interf_merge_log.txt in datarun_dir.

'''

import os
import re
import bisect
import h5py
import numpy as np
import time
import datetime

from read_hdf5 import unpack_datarun_sequence
from interf_raw import get_calibration_factor
# interf_raw is the script used to analyze raw interferometer data and computes the phase
# Future todo: save raw data as well
#===============================================================================================================================================

def get_start_timestamp(datarun_path): # NOT USED

	# Extract date and time from datarun hdf5 file using path name
	datarun_name = os.path.basename(datarun_path)
	datarun_name_parts = datarun_name.split("_")

	start_date = datarun_name_parts[2]
	start_time = datarun_name_parts[3][:-5]
	start_time = start_time.replace(".", ":")

	# Convert start_date and start_time to seconds since epoch
	start_datetime = datetime.datetime.strptime(start_date + " " + start_time, "%Y-%m-%d %H:%M:%S")
	start_timestamp = int(start_datetime.timestamp())

	print(start_datetime)
	return start_timestamp

def get_shot_timestamps(datarun_path, verbose=True):
	'''
	Get the shot numbers and timestamps of the completed shots in the datarun file.

	Parameters:
	datarun_path (str): The path to the datarun hdf5 file.
	verbose (bool): If True (default), forwarded to unpack_datarun_sequence so
		the "reading data run sequence" line is printed.

	Returns:
	(numpy.ndarray, numpy.ndarray): Parallel arrays of shot numbers (int) and
		timestamps in seconds since epoch, sorted by shot number.
	'''
	# Extract the sequence of shots from the datarun file
	f = h5py.File(datarun_path, "r")
	message_array, status_array, all_timestamp_array = unpack_datarun_sequence(f, verbose=verbose)
	f.close()

	# Some datarun sequences log multiple "Shot number = N" entries per shot
	# from different subsystems (e.g. SIS DAQ + bmotion), which would otherwise
	# produce duplicate timestamps. Only the SIS DAQ row marks actual data
	# acquisition, so match strictly on that: "SIS crate" (older) or
	# "SIS 3302" (newer). Shots without a SIS row are skipped.
	shot_re = re.compile(r'Shot number\s*=\s*(\d+)')
	by_shot = {}
	for i, timestamp in enumerate(all_timestamp_array):
		if status_array[i] != 'Completed':
			continue
		msg = message_array[i]
		if ('SIS crate' not in msg) and ('SIS 3302' not in msg):
			continue
		m = shot_re.search(msg)
		if m is None:
			continue
		shot_n = int(m.group(1))
		# A given shot should only have one SIS row; if duplicates exist,
		# keep the first occurrence.
		if shot_n not in by_shot:
			by_shot[shot_n] = timestamp - 2082844800

	shot_numbers = sorted(by_shot)
	return (np.array(shot_numbers, dtype=int),
	        np.array([by_shot[n] for n in shot_numbers]))

#===============================================================================================================================================

DATA_GROUPS = ("phase_p20", "phase_p29", "phase_p40", "time_array", "time_array_p40")


def init_datarun_groups(datarun_path, interf_path, verbose=True):
	'''
	Initialize the interferometer groups in the datarun file.

	Creates groups for whichever interferometer datasets are present in the
	source file, so old files (phase_p20, phase_p29, time_array only) and new
	files (with phase_p40 and time_array_p40) both work.

	Parameters:
	datarun_path (str): The path to the datarun hdf5 file.
	interf_path (str): The path to the interferometer hdf5 file.
	verbose (bool): If True (default), print a completion line.
	'''
	with h5py.File(datarun_path, "a") as f_datarun:
		with h5py.File(interf_path, "r") as f_interf:

			parent = f_datarun.require_group("diagnostics/interferometer")
			if 'description' in f_interf.attrs and 'description' not in parent.attrs:
				parent.attrs['description'] = f_interf.attrs['description']

			for name in DATA_GROUPS:
				if name not in f_interf:
					continue
				sub = f_datarun.require_group(f"diagnostics/interferometer/{name}")
				for attr_name, attr_value in f_interf[name].attrs.items():
					if attr_name not in sub.attrs:
						sub.attrs[attr_name] = attr_value

	if verbose:
		print('Interferometer groups created/loaded in datarun file')

def write_attribute(ifn):
	'''
	Write attributes for interferometer data.
	(interferometer data before 2024-06-07 does not have attributes, so should skip this function)

	Only writes attributes for groups that already exist in the file, so this
	is a no-op for the channels that the source file did not provide.

	Parameters:
	ifn (str): The path to the interferometer hdf5 file.
	'''
	phase_specs = {
		"phase_p20": ("Phase data for interferometer at port 20. Attribute calibration factor assumes 40cm plasma length.", 288e9),
		"phase_p29": ("Phase data for interferometer at port 29. Attribute calibration factor assumes 40cm plasma length.", 282e9),
		"phase_p40": ("Phase data for interferometer at port 40 (Rigol DHO scope). Attribute calibration factor assumes 40cm plasma length.", 288e9),
	}
	time_specs = {
		"time_array": "Time array for interferometer data in milliseconds.",
		"time_array_p40": "Time array for phase_p40 (Rigol). Independent of time_array which is LeCroy.",
	}

	with h5py.File(ifn, "a") as f:
		for name, (description, freq) in phase_specs.items():
			path = f"diagnostics/interferometer/{name}"
			if path not in f:
				continue
			grp = f[path]
			if len(grp.attrs) == 0:
				grp.attrs['description'] = description
				grp.attrs['unit'] = "rad"
				grp.attrs['Microwave frequency (Hz)'] = freq
				grp.attrs['calibration factor (m^-3/rad)'] = get_calibration_factor(freq)

		for name, description in time_specs.items():
			path = f"diagnostics/interferometer/{name}"
			if path not in f:
				continue
			grp = f[path]
			if len(grp.attrs) == 0:
				grp.attrs['description'] = description
				grp.attrs['unit'] = "ms"

#===============================================================================================================================================

def find_interf_file(datarun_path, interf_path): # NOT USED

	ifn_ls = []

	date = datarun_path[-24:-14]

	prev_date = datetime.datetime.strptime(date, "%Y-%m-%d") - datetime.timedelta(days=1)
	prev_date_str = prev_date.strftime("%Y-%m-%d")
	ifn = os.path.join(interf_path, f"interferometer_data_{prev_date_str}.hdf5")
	ifn_ls.append(ifn)

	interf_files = os.listdir(interf_path)
	for file in interf_files:
		if date in file:
			ifn = os.path.join(interf_path, file)
			ifn_ls.append(ifn)
	
	next_date = datetime.datetime.strptime(date, "%Y-%m-%d") + datetime.timedelta(days=1)
	next_date_str = next_date.strftime("%Y-%m-%d")
	ifn = os.path.join(interf_path, f"interferometer_data_{next_date_str}.hdf5")
	ifn_ls.append(ifn)

	return ifn_ls

def merge_interferometer_data(datarun_path, interf_path, all_shots=False, verbose=True):
	'''
	Merge the interferometer data into the datarun file.

	By default only the first and last completed shots of the datarun are
	merged, which is enough for sanity-checking density at the start and end
	of a run while keeping the merged file small. Set ``all_shots=True`` to
	merge every shot.

	Parameters:
	datarun_path (str): The path to the datarun hdf5 file.
	interf_path (str): The path to the interferometer hdf5 file.
	all_shots (bool): If True, merge every shot; otherwise only the first
		and last shots (default False).
	verbose (bool): If True (default), print per-shot progress. Set False for
		batch contexts where the caller emits its own progress summary.

	Returns:
	int: Number of shots whose data was written to the datarun file.
	'''
	def _log(msg):
		if verbose:
			print(msg)
	shot_numbers, timestamp_array = get_shot_timestamps(datarun_path, verbose=verbose)

	shots_written = 0
	with h5py.File(interf_path, "r") as f_interf:
		# phase_p20 is the canonical reader index in newer files (it is written
		# last so partial shots are skipped). Fall back to whichever known group
		# exists for older formats.
		index_group = next((g for g in DATA_GROUPS if g in f_interf), None)
		if index_group is None:
			raise ValueError(f"No interferometer groups found in {interf_path}")
		# Sort interferometer dataset names by their float timestamp once so each
		# shot lookup is O(log N) via bisect instead of O(N) linear scan.
		sorted_pairs = sorted(((float(name), name) for name in f_interf[index_group].keys()),
		                      key=lambda p: p[0])
		sorted_floats = [p[0] for p in sorted_pairs]

		def find_match(timestamp, tolerance=1.0):
			if not sorted_floats:
				return None
			idx = bisect.bisect_left(sorted_floats, timestamp)
			candidates = []
			if idx < len(sorted_floats):
				candidates.append(idx)
			if idx > 0:
				candidates.append(idx - 1)
			best = min(candidates, key=lambda j: abs(sorted_floats[j] - timestamp))
			if abs(sorted_floats[best] - timestamp) < tolerance:
				return sorted_pairs[best][1]
			return None

		# Only copy groups that exist in both the source file and the datarun file.
		with h5py.File(datarun_path, "r") as f_datarun:
			datarun_groups = set(f_datarun.get("diagnostics/interferometer", {}).keys())
		available_groups = [g for g in DATA_GROUPS if g in f_interf and g in datarun_groups]

		# Build the list of shots to merge. Each entry is (shot_number, matching_set)
		# where shot_number is the real shot number from the datarun sequence, not
		# the positional index — so dataset names line up with shot identity even
		# if the sequence has gaps.
		matched_shots = []
		if all_shots:
			for k, timestamp in enumerate(timestamp_array):
				matching_set = find_match(timestamp)
				if matching_set is None:
					_log(f"Shot {shot_numbers[k]} has no matching interferometer data")
					continue
				matched_shots.append((int(shot_numbers[k]), matching_set))
		else:
			# Walk inward from each end so the common case (matches near the
			# boundaries) short-circuits without scanning the whole run.
			first_idx = None
			for k in range(len(timestamp_array)):
				match = find_match(timestamp_array[k])
				if match is not None:
					first_idx = k
					matched_shots.append((int(shot_numbers[k]), match))
					break

			for k in range(len(timestamp_array) - 1, -1, -1):
				if first_idx is not None and k <= first_idx:
					break
				match = find_match(timestamp_array[k])
				if match is not None:
					matched_shots.append((int(shot_numbers[k]), match))
					break

		if not matched_shots:
			_log('No interferometer traces found for any datarun shot')
			return 0

		# Datarun file is opened/closed per shot so an interruption mid-run leaves
		# already-merged shots safely flushed to disk.
		for shot_num, matching_set in matched_shots:
			shot_n = str(shot_num)

			shot_data = {}
			shot_attrs = {}
			for g in available_groups:
				# phase_p40 may legitimately be absent for a shot when the
				# Rigol scope was unavailable; just skip that channel.
				if matching_set not in f_interf[g]:
					continue
				ds = f_interf[g][matching_set]
				shot_data[g] = ds[:]
				shot_attrs[g] = dict(ds.attrs)

			with h5py.File(datarun_path, "a") as f_datarun:
				wrote_any = False
				for g, data in shot_data.items():
					dest = f_datarun[f"diagnostics/interferometer/{g}"]
					if shot_n in dest:
						continue
					new_ds = dest.create_dataset(shot_n, data=data)
					for attr_name, attr_value in shot_attrs[g].items():
						new_ds.attrs[attr_name] = attr_value
					wrote_any = True
			if wrote_any:
				shots_written += 1

			_log(f"Shot {shot_num} wrote into datarun file")

	_log(f'Interferometer data merged into datarun file ({shots_written} shots written).')
	return shots_written


#===============================================================================================================================================

_DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})')


def _interf_filename_for_date(date_str):
	return f"interferometer_data_{date_str}.hdf5"


def _datarun_date(datarun_path):
	'''
	Resolve the datarun's date as a (date, source) tuple where source is
	"filename" or "ctime". The date is parsed from the filename if possible;
	otherwise it falls back to the file's creation time. On Windows
	os.path.getctime returns actual creation time. Returns (None, None) if
	the file is missing.
	'''
	m = _DATE_RE.search(os.path.basename(datarun_path))
	if m is not None:
		return datetime.datetime.strptime(m.group(1), "%Y-%m-%d"), "filename"
	try:
		ctime = os.path.getctime(datarun_path)
	except OSError:
		return None, None
	return datetime.datetime.fromtimestamp(ctime), "ctime"


def _candidate_interf_files(datarun_path, interf_dir):
	'''
	Return (candidate_paths, date_source) for a datarun file. The date is
	parsed from the filename when possible, or falls back to the file's
	creation time. Candidates are ordered same-day, prev-day, next-day; only
	paths that exist on disk are included. If no date can be resolved at all,
	date_source is None.
	'''
	date, source = _datarun_date(datarun_path)
	if date is None:
		return [], None

	candidates = [date.strftime("%Y-%m-%d"),
	              (date - datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
	              (date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")]
	paths = [os.path.join(interf_dir, _interf_filename_for_date(d)) for d in candidates]
	return [p for p in paths if os.path.isfile(p)], source


def merge_folder(datarun_dir, interf_dir, all_shots=False):
	'''
	Run the interferometer merge for every datarun hdf5 file in a folder.

	For each datarun file, parse the YYYY-MM-DD date out of its filename and
	look for the matching interferometer_data_<date>.hdf5 in interf_dir,
	falling back to the previous or next day if the same-day file is absent.
	Errors per file (missing interf file, no shot matches, corrupt file) are
	logged and the batch continues.

	Parameters:
	datarun_dir (str): Directory containing datarun .hdf5 files.
	interf_dir (str): Directory containing interferometer .hdf5 files.
	all_shots (bool): Passed through to merge_interferometer_data.

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

	# Log file lives alongside the datarun files, fixed name so re-runs
	# overwrite the previous log rather than accumulating.
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

	emit(f"Batch merge started at {datetime.datetime.now().isoformat(timespec='seconds')}")
	emit(f"Batch merge: {total} datarun file(s) from {datarun_dir}")
	emit(f"             interferometer files from {interf_dir}")
	emit(f"             all_shots={all_shots}")
	emit("-" * (idx_w * 2 + name_w + 30))

	try:
		for i, fname in enumerate(datarun_files, start=1):
			datarun_path = os.path.join(datarun_dir, fname)
			prefix = f"[{i:>{idx_w}}/{total}] {fname:<{name_w}}"

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

			interf_path = candidates[0]
			interf_name = os.path.basename(interf_path)
			src_tag = " [ctime]" if date_source == "ctime" else ""

			try:
				init_datarun_groups(datarun_path, interf_path, verbose=False)
				n_written = merge_interferometer_data(datarun_path, interf_path,
				                                      all_shots=all_shots, verbose=False)
				if n_written == 0:
					emit(f"{prefix}  EMPTY {interf_name}{src_tag}  (0 shots matched)")
					results[datarun_path] = f"empty ({interf_name})"
					counts["empty"] += 1
				else:
					emit(f"{prefix}  OK    {interf_name}{src_tag}  ({n_written} shots)")
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
	  
	datarun_path = r"C:\data\LAPD\07_Dipole_plane_p32_Diris7cm_MaskBiasing.hdf5"
	interf_path = r"C:\data\LAPD\interferometer_samples\interferometer_data_2024-07-02.hdf5"

	init_datarun_groups(datarun_path, interf_path)

	merge_interferometer_data(datarun_path, interf_path)

	write_attribute(datarun_path)