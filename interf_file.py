# coding: utf-8

'''
This module contains functions for hdf5 handeling

Author: Jia Han
Ver1.0 created on: 2021-06-01
'''

import sys
import numpy as np
import time
import os
import h5py

from lab_scopes.io.lecroy_files import read_trc_data_simplified, read_trc_data_no_header
from interf_raw import get_calibration_factor

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================
def find_latest_shot_number(dir_path):
	try:
		file_list = os.listdir(dir_path)
		full_path_file_list = [os.path.join(dir_path, file) for file in file_list]
		newest_file = max(full_path_file_list, key=os.path.getctime)
		shot_number = int(newest_file[-9:-4])
	except ValueError:
		print('No file exist in directory; try again in 10 sec')
		time.sleep(10)
		shot_number = find_latest_shot_number(dir_path)
	return shot_number

def write_to_temp(file_path, temp_path): # NOT USED
	'''
	Saves interferometer data to a temporary folder.
	Loops continuously to save the latest interferometer data files to the temporary folder.
	Starts from the most recent shot in the folder.

	Parameters:
	- file_path (str): The path to the folder containing the interferometer data files.
	- temp_path (str): The path to the temporary folder where the data will be saved.
	'''

	shot_number = find_latest_shot_number(file_path)

	while True:
		try:
			st = time.time()

			ifn = f"{file_path}/C1-interf-shot{shot_number:05d}.trc"
			
			if not os.path.exists(ifn):
				time.sleep(0.05)
				continue
			
			saved_time=os.path.getmtime(ifn)
			if st - saved_time > 5:
				print("Operation too slow; Skip shot to catch up")
				shot_number += 1
				continue
			
			refchA, tarr, vertical_gain, vertical_offset = read_trc_data_simplified(ifn)
			data_size = len(tarr)

			ifn = f"{file_path}/C2-interf-shot{shot_number:05d}.trc"
			plachA = read_trc_data_no_header(ifn, data_size, vertical_gain, vertical_offset)

			ifn = f"{file_path}/C3-interf-shot{shot_number:05d}.trc"
			refchB = read_trc_data_no_header(ifn, data_size, vertical_gain, vertical_offset)

			ifn = f"{file_path}/C4-interf-shot{shot_number:05d}.trc"
			plachB = read_trc_data_no_header(ifn, data_size, vertical_gain, vertical_offset)
			
			
			np.savez(f"{temp_path}/shot{shot_number:05d}.npz", refchA=refchA, plachA=plachA, refchB=refchB, plachB=plachB, tarr=tarr, saved_time=saved_time)
			print(f"Saved shot {shot_number:05d} to temp folder")
			print(f"Time taken: {time.time() - st:.2f} s")

			shot_number += 1
		
		except OSError as e:
			print(e)
			time.sleep(0.05)
			continue

		except KeyboardInterrupt:
			print("Keyboard interrupt")
			break
		except Exception as e:
			print(f"Error: {e}")
			break

def load_shot_data(file_path): # not used Jun-2024
	# Load the .npz file
	with np.load(file_path) as data:
		refchA = data['refchA']
		plachA = data['plachA']
		refchB = data['refchB']
		plachB = data['plachB']
		tarr = data['tarr']
		saved_time = data['saved_time']
	return refchA, plachA, refchB, plachB, tarr, saved_time
	
#===============================================================================================================================================
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

def delete_file(file_path):
	try:
		os.remove(file_path)
		print(f"File {file_path} has been deleted.")
	except FileNotFoundError:
		print(f"File {file_path} does not exist.")
	except Exception as e:
		print(f"Error deleting file {file_path}: {e}")
#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == '__main__':
	
	if False:
		file_path = "/home/smbshare" # Network drive located on LeCroy scope, mounted on RP 
		temp_path = "/mnt/ramdisk" 	 # Temporary ramdisk on RP, see readme on desktop
		write_to_temp(file_path, temp_path)
	
	if False:
		ifn = r"C:\data\LAPD\interferometer_data_2024-06-01.hdf5"
		f = h5py.File(ifn, "r")
		groups = list(f.keys())
		sets = list(f[groups[0]].keys())
		
		time_list = os.path.getctime(ifn), os.path.getmtime(ifn)
