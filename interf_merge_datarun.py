# coding utf-8
'''
Merge the interferometer data from the available channels to datarun hdf5 file.
Author: Jia Han
Last update: 2026-04-29

Functions used in this script:

init_datarun_groups(datarun_path, interf_path)
	- Creates the interferometer groups in the datarun file.
	- Copies per-channel attributes (description, unit, microwave frequency,
	  calibration factor) from the source file onto each
	  diagnostics/interferometer/<name> subgroup.
merge_interferometer_data(datarun_path, interf_path, all_shots=False)
	- Finds the interferometer data for each shot in datarun file by matching the timestamps.
	- Copies interferometer data into the datarun file.
	- By default, only the first and last completed shots of the run are
	  merged (a quick sanity check while keeping the merged file small).
	  Pass all_shots=True to merge every shot.
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
- Datasets under each group are named by shot number (starting from 1).
- If shot number doesn't exist, it means interferometer data is missing for that shot.
- phase_p40 datasets may carry per-shot attributes 'rigol_missing' (bool)
  and 'rigol_missing_reason' (str) when the Rigol scope was unavailable
  for that shot; the array is then a zero-filled placeholder.

How to use this script:
- Change the datarun_path and interf_path to the correct paths.
- Run the script in the terminal or in an IDE.
- The script will try to match the timestamps of the shots in the datarun file with the interferometer data.
- Sometimes you might need to use the interferometer data from the previous or next day.
- Data will only be copied over if a matching timestamp is found.
- The default merges only the first and last shot. To merge every shot,
  call merge_interferometer_data(..., all_shots=True).

'''

import os
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

def get_shot_timestamps(datarun_path):
	'''
	Get the timestamps of the completed shots in the datarun file.

	Parameters:
	datarun_path (str): The path to the datarun hdf5 file.

	Returns:
	numpy.ndarray: An array of timestamps in seconds since epoch.
	'''
	# Extract the sequence of shots from the datarun file
	f = h5py.File(datarun_path, "r")
	message_array, status_array, all_timestamp_array = unpack_datarun_sequence(f)
	f.close()

	# Correct timestamps
	timestamp_array = np.array([])
	for i, timestamp in enumerate(all_timestamp_array):
		
		# Check if the saved message correspond to an actual completed shot
		if ('Shot number' in message_array[i]) and (status_array[i] == 'Completed'):

			t_corrected = timestamp - 2082844800
			timestamp_array = np.append(timestamp_array, t_corrected)

	return timestamp_array

#===============================================================================================================================================

DATA_GROUPS = ("phase_p20", "phase_p29", "phase_p40", "time_array", "time_array_p40")


def init_datarun_groups(datarun_path, interf_path):
	'''
	Initialize the interferometer groups in the datarun file.

	Creates groups for whichever interferometer datasets are present in the
	source file, so old files (phase_p20, phase_p29, time_array only) and new
	files (with phase_p40 and time_array_p40) both work.

	Parameters:
	datarun_path (str): The path to the datarun hdf5 file.
	interf_path (str): The path to the interferometer hdf5 file.
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

def merge_interferometer_data(datarun_path, interf_path, all_shots=False):
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
	'''
	timestamp_array = get_shot_timestamps(datarun_path)

	f_interf = h5py.File(interf_path, "r")

	# phase_p20 is the canonical reader index in newer files (it is written
	# last so partial shots are skipped). Fall back to whichever known group
	# exists for older formats.
	index_group = next((g for g in DATA_GROUPS if g in f_interf), None)
	if index_group is None:
		f_interf.close()
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

	# Build the list of shots to merge.
	matched_shots = []
	if all_shots:
		for i, timestamp in enumerate(timestamp_array):
			matching_set = find_match(timestamp)
			if matching_set is None:
				print(f"Shot {i+1} has no matching interferometer data")
				continue
			matched_shots.append((i, matching_set))
	else:
		# Walk inward from each end so the common case (matches near the
		# boundaries) short-circuits without scanning the whole run.
		first_idx = None
		for i in range(len(timestamp_array)):
			match = find_match(timestamp_array[i])
			if match is not None:
				first_idx = i
				matched_shots.append((i, match))
				break

		for i in range(len(timestamp_array) - 1, -1, -1):
			if first_idx is not None and i <= first_idx:
				break
			match = find_match(timestamp_array[i])
			if match is not None:
				matched_shots.append((i, match))
				break

	if not matched_shots:
		f_interf.close()
		print('No interferometer traces found for any datarun shot')
		return

	# Datarun file is opened/closed per shot so an interruption mid-run leaves
	# already-merged shots safely flushed to disk.
	for i, matching_set in matched_shots:
		shot_n = str(i+1) # Naming convention goes with shot number (starts from index 1)

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
			for g, data in shot_data.items():
				dest = f_datarun[f"diagnostics/interferometer/{g}"]
				if shot_n in dest:
					continue
				new_ds = dest.create_dataset(shot_n, data=data)
				for k, v in shot_attrs[g].items():
					new_ds.attrs[k] = v

		print(f"Shot {i+1} wrote into datarun file")

	f_interf.close()
	print('Interferometer data merged into datarun file. File closed.')


#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == '__main__':
	  
	datarun_path = r"C:\data\LAPD\07_Dipole_plane_p32_Diris7cm_MaskBiasing.hdf5"
	interf_path = r"C:\data\LAPD\interferometer_samples\interferometer_data_2024-07-02.hdf5"

	init_datarun_groups(datarun_path, interf_path)

	merge_interferometer_data(datarun_path, interf_path)

	write_attribute(datarun_path)