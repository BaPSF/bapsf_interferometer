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

from read_scope_data import read_trc_data_simplified, read_trc_data_no_header
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
		print('No file exist in directory; start iteration at shot 0')
		return 0
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

def init_hdf5_file(file_name):
	if os.path.exists(file_name):
		print("HDF5 file exist")
		return None
	
	with h5py.File(file_name, "w",  libver='latest') as f:
		ct = time.localtime()
		f.attrs['created'] = ct
		print("HDF5 file created ", time.strftime("%Y-%m-%d %H:%M:%S", ct))
		f.attrs['description'] = "Interferometer data. See group description and attribute for more info."

		grp = f.require_group("phase_p20")
		grp.attrs['description'] = "Phase data for interferometer at port 20. Attribute calibration factor assumes 40cm plasma length."
		grp.attrs['unit'] = "rad"
		grp.attrs['Microwave frequency (Hz)'] = 288e9
		grp.attrs['calibration factor (m^-3/rad)'] = get_calibration_factor(grp.attrs['Microwave frequency (Hz)'])

		grp = f.require_group("phase_p29")
		grp.attrs['description'] = "Phase data for interferometer at port 29. Attribute calibration factor assumes 40cm plasma length."
		grp.attrs['unit'] = "rad"
		grp.attrs['Microwave frequency (Hz)'] = 282e9
		grp.attrs['calibration factor (m^-3/rad)'] = get_calibration_factor(grp.attrs['Microwave frequency (Hz)'])

		grp = f.require_group("time_array")
		grp.attrs['description'] = "Time array for interferometer data in milliseconds."
		grp.attrs['unit'] = "ms"


def create_sourcefile_dataset(f, dataA, dataB, t_ms, saved_time):

	grp = f.require_group("phase_p20")
	fds = grp.create_dataset(str(saved_time), data=dataA)
	fds.attrs['modified'] = time.ctime(saved_time)

	grp = f.require_group("phase_p29")
	fds = grp.create_dataset(str(saved_time), data=dataB)
	fds.attrs['modified'] = time.ctime(saved_time)

	grp = f.require_group("time_array")
	fds = grp.create_dataset(str(saved_time), data=t_ms)
	fds.attrs['modified'] = time.ctime(saved_time)

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