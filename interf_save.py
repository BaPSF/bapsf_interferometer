# coding: utf-8

'''
This module reads interferometer data acquired from the scope and saves them to a temporary npz file. (Binary format)
'''

import sys
import numpy as np
import time
import os

from read_scope_data import read_trc_data_simplified, read_trc_data_no_header

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

def write_to_temp(file_path, temp_path):
	'''
	Saves interferometer data to a temporary folder.
	Loops continuously to save the latest interferometer data files to the temporary folder.
	Starts from the most recent shot in the folder.

	Parameters:
	- file_path (str): The path to the folder containing the interferometer data files.
	- temp_path (str): The path to the temporary folder where the data will be saved.
	'''

	file_list = os.listdir(file_path)
	newest_file = max(file_list, key=os.path.getctime)
	shot_number = int(newest_file.split('-')[1].split('.')[0])

	while True:
		try:
			st = time.time()

			ifn = f"{file_path}/C1-interf-shot{shot_number:05d}.trc"            
			
			if not os.path.exists(ifn):
				print(f"Shot {shot_number:05d} does not exist")
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

		except KeyboardInterrupt:
			print("Keyboard interrupt")
			break
		except Exception as e:
			print(f"Error: {e}")
			break

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == '__main__':
	
	if True:
		file_path = "/home/smbshare" # Network drive located on LeCroy scope, mounted on RP 
		temp_path = "/mnt/ramdisk" 	 # Temporary ramdisk on RP, see readme on desktop
		write_to_temp(file_path, temp_path)
	
	if False:
		data = read_shot(0)
		print(data.keys())
