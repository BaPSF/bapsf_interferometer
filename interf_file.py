# coding: utf-8

'''
Schema and writers for the daily interferometer HDF5 file, as the previous (.trc) acquisition wrote it;
interf_store now writes it from the IOC's shots.

Author: Jia Han
Ver1.0 created on: 2021-06-01
'''

import time
import os
import h5py
import numpy as np

from interf_analysis import PORTS, get_calibration_factor

_FREQ_HZ = {port.name: port.freq_hz for port in PORTS}  # one source, so the HDF5 cal attribute matches live ne
_LECROY_PORTS = {port.name for port in PORTS if port.scope == "lecroy"}

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

		_ensure_phase_group(f, "phase_p20", "Phase data for interferometer at port 20. Attribute calibration factor assumes 40cm plasma length.", _FREQ_HZ["P20"])
		_ensure_phase_group(f, "phase_p29", "Phase data for interferometer at port 29. Attribute calibration factor assumes 40cm plasma length.", _FREQ_HZ["P29"])
		_ensure_phase_group(f, "phase_p40", "Phase data for interferometer at port 40 (Rigol DHO scope). Attribute calibration factor assumes 40cm plasma length.", _FREQ_HZ["P40"])
		_ensure_time_group(f, "time_array", "Time array for interferometer data in milliseconds.")
		_ensure_time_group(f, "time_array_p40", "Time array for phase_p40 (Rigol). Independent of time_array which is LeCroy.")


def _delete_stale_partial_dataset(groups, dataset_name):
	for grp in groups:
		if dataset_name in grp:
			del grp[dataset_name]


class ShotExists(ValueError):
	"""create_sourcefile_dataset: the shot's phase_p20 dataset is already in the file."""


def _mark_missing(ds, reason):
	ds.attrs['missing'] = True
	ds.attrs['missing_reason'] = reason


def create_sourcefile_dataset(f, dataA, dataB, dataC, t_ms, t_ms_C, saved_time, rigol_missing=False, rigol_missing_reason="",
                              lecroy_missing=None, shot=None):
	"""lecroy_missing: {"P20" | "P29": reason} for ports without data. Their phase (dataA/dataB, ignored) is written as NaN
	of len(t_ms), with missing/missing_reason attributes. shot: an object with shot_date, shot_number and time_source,
	stored as attributes of the phase_p20 dataset."""
	dataset_name = str(saved_time)
	lecroy_missing = lecroy_missing or {}
	if set(lecroy_missing) - _LECROY_PORTS:
		raise ValueError(f"lecroy_missing names {sorted(lecroy_missing)}; the LeCroy ports are {sorted(_LECROY_PORTS)}")
	# Legacy readers plot phase_p20 and phase_p29 against the one time_array, and every legacy shot has both at its length
	# (main stored no shot with a LeCroy port missing), so NaN keeps that length without a fabricated value. Both
	# missing: t_ms is empty, and so are they. P40 has its own time array and stays empty, as main writes it.
	phases = {"P20": dataA, "P29": dataB}
	for port in lecroy_missing:
		phases[port] = np.full(len(t_ms), np.nan)

	phase_p20 = f.require_group("phase_p20")
	phase_p29 = f.require_group("phase_p29")
	time_array = f.require_group("time_array")
	phase_p40 = f.require_group("phase_p40")
	time_array_p40 = f.require_group("time_array_p40")

	if dataset_name in phase_p20:
		raise ShotExists(f"Shot dataset {dataset_name} already exists")

	# phase_p20 is the reader index; write it last so readers skip partial shots.
	_delete_stale_partial_dataset([phase_p29, time_array, phase_p40, time_array_p40], dataset_name)

	time_array.create_dataset(dataset_name, data=t_ms)
	ds_p29 = phase_p29.create_dataset(dataset_name, data=phases["P29"])
	if "P29" in lecroy_missing:
		_mark_missing(ds_p29, lecroy_missing["P29"])

	time_array_p40.create_dataset(dataset_name, data=t_ms_C)
	ds_p40 = phase_p40.create_dataset(dataset_name, data=dataC)
	ds_p40.attrs['rigol_missing'] = bool(rigol_missing)
	if rigol_missing_reason:
		ds_p40.attrs['rigol_missing_reason'] = rigol_missing_reason

	ds_p20 = phase_p20.create_dataset(dataset_name, data=phases["P20"])
	if "P20" in lecroy_missing:
		_mark_missing(ds_p20, lecroy_missing["P20"])
	if shot is not None:
		ds_p20.attrs['shot_date'] = shot.shot_date
		ds_p20.attrs['shot_number'] = shot.shot_number
		ds_p20.attrs['time_source'] = shot.time_source
	f.flush()
