# coding: utf-8
'''
This module reads interferometer raw data acquired from the scope
Raw data are further analyzed using the interf_raw module
The analyzed data are saved to a HDF5 file using the interf_file module
The data are plotted using the interf_plot module

Author: Jia Han
Ver1.0 created on: 2021-06-01

Ver1.1 updated on: 2021-07-11
- Fix create new hdf5 at beginning of each day
- interf_main.py will now run on PC, where the hdf5 file is stored
- interf_plot.py will run on Raspberry Pi, which reads from hdf5 on PC and plots on screen 

Ver1.2 updated on: 2021-07-15
- Adjust multiprocessing for Windows
- previous versions works on Linux
'''
import sys
import signal
import multiprocessing
import threading
from queue import Queue
import h5py
import numpy as np
import time
import datetime
import os

from interf_raw import phase_from_raw, get_calibration_factor
from interf_file import find_latest_shot_number, init_hdf5_file, create_sourcefile_dataset
from read_scope_data import read_trc_data_simplified, read_trc_data_no_header
import interf_cleanup as cleanup


#===============================================================================================================================================
scope_path = r"I:\\"
log_path = r"C:\data\\log"
hdf5_path = r"C:\data\\interferometer"
#===============================================================================================================================================

def get_current_day(timestamp):
	'''
	gets current day from the timestamp
	'''
	ct = time.localtime(timestamp)
	return ct.tm_yday

#===============================================================================================================================================

def read_and_analyze(file_path, shot_number, refch_i, plach_i):
	'''
	read the data from scope network drive
	To save time, only one set of headers are read
	Analyze the data and put the result into the queue
	'''
	# read channel data from scope
	ifn = f"{file_path}\\C{refch_i}-interf-shot{shot_number:05d}.trc"
	refch, tarr, vertical_gain, vertical_offset = read_trc_data_simplified(ifn)
	data_size = len(tarr)
	
	# important note: the vertical gain and offset are assumed to be the same for C1 and C2
	ifn = f"{file_path}\\C{plach_i}-interf-shot{shot_number:05d}.trc"
	plach = read_trc_data_no_header(ifn, data_size, vertical_gain, vertical_offset)
#	plach, tarr, vertical_gain, vertical_offset = read_trc_data_simplified(ifn)
	
	# calculate the phase from interferometer raw data
	t_ms, phase = phase_from_raw(tarr, refch, plach)
	# used for multiprocessing
	# queue.put((t_ms, phase))

	return t_ms, phase

# Multiprocessing functions; DOES NOT WORK on Windows
def multiprocess_analyze(pool, file_path, shot_number): 
	'''
	creates two processes to read and analyze the interferometer data from the scope network drive
	'''

    # Submit task
	result1 = pool.apply_async(read_and_analyze, args=(file_path, shot_number, 1, 2))
	result2 = pool.apply_async(read_and_analyze, args=(file_path, shot_number, 3, 4))

	result1.wait()
	result2.wait()

	if result1.ready():
		t_ms, phaseA = result1.get()
	if result2.ready():
		t_ms, phaseB = result2.get()

	return t_ms, phaseA, phaseB
#===============================================================================================================================================
		
def main(hdf5_path, file_path, ram_path):
	"""
	Main function for the interferometer program.

	Args:
		hdf5_path (str, optional): Path to the HDF5 file directory. Defaults to "/media/interfpi/5C87-20CD".
		file_path (str, optional): Path to the file directory. Defaults to "/mnt/smbshare".
	"""

	# Create a new HDF5 file; if it already exists, do nothing
	date = datetime.date.today()
	hdf5_ifn = f"{hdf5_path}\\interferometer_data_{date}.hdf5"
	init_hdf5_file(hdf5_ifn)

	# Create log file to record the shot number
	log_ifn = f"{ram_path}\\interferometer_log.bin"
	if not os.path.exists(log_ifn):
		open(log_ifn, 'w').close()
		print("Log file created", date)
	else:
		with open(log_ifn, 'w') as log_file:
			log_file.write(" ")
	
	# Find the most recent shot in LeCroy Network drive 
	shot_number = find_latest_shot_number(file_path)

	pool = multiprocessing.Pool(processes=4)
	# Define a handler for SIGINT
	def sigint_handler(signum, frame):
		print("SIGINT (Ctrl-C) detected. Attempting to exit gracefully...")
		pool.terminate()
		pool.join()
		print("Cleanup complete. Exiting.")
		sys.exit(0)  # Exit the program

	# Set the SIGINT handler
	signal.signal(signal.SIGINT, sigint_handler)

	while True:
		try:

			st = time.time() # start time of the loop
			
			# Check if the interferometer data files are available on leCroy scope drive
			# C4 is the last channel to be saved
			ifn = f"{file_path}\\C4-interf-shot{shot_number:05d}.trc"
			if not os.path.exists(ifn):
				time.sleep(0.1)
				continue
			saved_time=os.path.getctime(ifn) # time when the shot data was saved
			td = st - saved_time
			# print("Time difference: ", round(td,2))
			
			# If the operation is too slow, skip the shot to catch up
			if td > 3:
				print("Skip one shot")
				shot_number += 1
				time.sleep(0.01)
				continue

			print("Shot ", shot_number)
			# t_ms, phaseA = read_and_analyze(file_path, shot_number, 1, 2)
			# t_ms, phaseB = read_and_analyze(file_path, shot_number, 3, 4)
			t_ms, phaseA, phaseB = multiprocess_analyze(pool, file_path, shot_number)

			# Save the data to the HDF5 file
			f = h5py.File(hdf5_ifn, 'a', libver='latest')
			
			fc_day = f.attrs['created'][-2] # Check if the day has changed
			cd = get_current_day(st)
			if fc_day != cd: # if so, create a new HDF5 file
				f.close()
				date = datetime.date.today()
				hdf5_ifn = f"{hdf5_path}\\interferometer_data_{date}.hdf5"
				init_hdf5_file(hdf5_ifn)
				f = h5py.File(hdf5_ifn, 'a', libver='latest')

			# save data to HDF5 file as new datasets
			create_sourcefile_dataset(f, phaseA, phaseB, t_ms, saved_time)
			f.close()
			
			print("Time taken: ", time.time() - st)
			if shot_number == 99999: # reset shot number to 0 after reaching the maximum
				shot_number = 0
				continue
			
			# Record shot number and saved_time in log file
			with open(log_ifn, 'a') as log_file:
				log_file.write(f"{shot_number},{saved_time}\n")

			shot_number += 1

	
		except KeyboardInterrupt:
			print("Keyboard interrupt detected. Exit program.")
			pool.terminate()
			pool.join()
			print("Cleanup complete. Exiting.")
			break
			
		except Exception as e:
			print(f"An error occurred: {e}")
			pool.terminate()
			pool.join()
			print("Cleanup complete. Exiting.")

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == '__main__':

	main(hdf5_path, scope_path, log_path)
	
	def run_main_and_cleanup():
		main_thread = threading.Thread(target=main, args=(hdf5_path, scope_path, log_path))
		cleanup_thread = threading.Thread(target=cleanup.main)
		
		main_thread.start()
		cleanup_thread.start()
		
		main_thread.join()
		cleanup_thread.join()

#	run_main_and_cleanup()