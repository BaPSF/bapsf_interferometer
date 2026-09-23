# coding: utf-8

'''
Schema and writers for the daily interferometer HDF5 file written by the previous (.trc)
acquisition; the new raw acquisition does not use them.

Author: Jia Han
Ver1.0 created on: 2021-06-01
'''

import time
import os
import h5py

from interf_analysis import get_calibration_factor

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

def _set_attr_if_missing(obj, name, value):
	if name not in obj.attrs:
		obj.attrs[name] = value


def _ensure_phase_group(f, group_name, description, frequency_hz):
	grp = f.require_group(group_name)
	_set_attr_if_missing(grp, 'description', description)
	_set_attr_if_missing(grp, 'unit', "rad")
	_set_attr_if_missing(grp, 'Microwave frequency (Hz)', frequency_hz)
	_set_attr_if_missing(grp, 'calibration factor (m^-3/rad)', get_calibration_factor(frequency_hz))
	return grp


def _ensure_time_group(f, group_name, description):
	grp = f.require_group(group_name)
	_set_attr_if_missing(grp, 'description', description)
	_set_attr_if_missing(grp, 'unit', "ms")
	return grp


def init_hdf5_file(file_name):
	file_exists = os.path.exists(file_name)

	with h5py.File(file_name, "a", libver='latest') as f:
		ct = time.localtime()
		if 'created' not in f.attrs:
			f.attrs['created'] = ct
			print("HDF5 file created ", time.strftime("%Y-%m-%d %H:%M:%S", ct))
		elif file_exists:
			print("HDF5 file exist")

		_set_attr_if_missing(f, 'description', "Interferometer data. Datasets in each group are named by timestamp of when data was acquired. Timestamps are saved as seconds since epoch January 1, 1970, 00:00:00 (UTC). See each individual group description and attribute for more info.")

		_ensure_phase_group(f, "phase_p20", "Phase data for interferometer at port 20. Attribute calibration factor assumes 40cm plasma length.", 288e9)
		_ensure_phase_group(f, "phase_p29", "Phase data for interferometer at port 29. Attribute calibration factor assumes 40cm plasma length.", 282e9)
		_ensure_phase_group(f, "phase_p40", "Phase data for interferometer at port 40 (Rigol DHO scope). Attribute calibration factor assumes 40cm plasma length.", 288e9)
		_ensure_time_group(f, "time_array", "Time array for interferometer data in milliseconds.")
		_ensure_time_group(f, "time_array_p40", "Time array for phase_p40 (Rigol). Independent of time_array which is LeCroy.")


def _delete_stale_partial_dataset(groups, dataset_name):
	for grp in groups:
		if dataset_name in grp:
			del grp[dataset_name]


def create_sourcefile_dataset(f, dataA, dataB, dataC, t_ms, t_ms_C, saved_time, rigol_missing=False, rigol_missing_reason=""):
	dataset_name = str(saved_time)

	phase_p20 = f.require_group("phase_p20")
	phase_p29 = f.require_group("phase_p29")
	time_array = f.require_group("time_array")
	phase_p40 = f.require_group("phase_p40")
	time_array_p40 = f.require_group("time_array_p40")

	if dataset_name in phase_p20:
		raise ValueError(f"Shot dataset {dataset_name} already exists")

	# phase_p20 is the reader index; write it last so readers skip partial shots.
	_delete_stale_partial_dataset([phase_p29, time_array, phase_p40, time_array_p40], dataset_name)

	time_array.create_dataset(dataset_name, data=t_ms)
	phase_p29.create_dataset(dataset_name, data=dataB)

	time_array_p40.create_dataset(dataset_name, data=t_ms_C)
	ds_p40 = phase_p40.create_dataset(dataset_name, data=dataC)
	ds_p40.attrs['rigol_missing'] = bool(rigol_missing)
	if rigol_missing_reason:
		ds_p40.attrs['rigol_missing_reason'] = rigol_missing_reason

	phase_p20.create_dataset(dataset_name, data=dataA)
	f.flush()

	print("Saved interferometer shot at", time.ctime(saved_time))
