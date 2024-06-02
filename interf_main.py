# coding: utf-8
'''
This module reads interferometer raw data acquired from the scope
Raw data are further analyzed using the interf_raw module
The analyzed data are saved to a HDF5 file using the interf_file module
The data are plotted using the interf_plot module

Author: Jia Han
Ver1.0 created on: 2021-06-01
'''
import sys
import multiprocessing
import h5py
import numpy as np
import time
import datetime
import os

from interf_raw import density_from_phase
from interf_plot import init_plot, update_plot, end_plot
from interf_file import find_latest_shot_number, init_hdf5_file, create_sourcefile_dataset
from read_scope_data import read_trc_data_simplified, read_trc_data_no_header

#===============================================================================================================================================
def get_current_day(timestamp):
	'''
	gets current day in year,month,day format from the timestamp
	'''
	ct = time.localtime(timestamp)
	return (ct.tm_year, ct.tm_mon, ct.tm_mday)

#===============================================================================================================================================
# Multiprocessing functions
def read_and_analyze(file_path, shot_number, queue, refch_i, plach_i):
	'''
	read the data from scope network drive
	To save time, only one set of headers are read
	Analyze the data and put the result into the queue
	''' 
	ifn = f"{file_path}/C{refch_i}-interf-shot{shot_number:05d}.trc"
	refch, tarr, vertical_gain, vertical_offset = read_trc_data_simplified(ifn)
	data_size = len(tarr)
	
	# important note: the vertical gain and offset are assumed to be the same for C1 and C2
	ifn = f"{file_path}/C{plach_i}-interf-shot{shot_number:05d}.trc"
	plach = read_trc_data_no_header(ifn, data_size, vertical_gain, vertical_offset)
#	plach, tarr, vertical_gain, vertical_offset = read_trc_data_simplified(ifn)
	
	# calculate the density from interferometer raw data
	t_ms, ne = density_from_phase(tarr, refch, plach)
	
	# put the data into the queue for further processing
	queue.put((t_ms, ne))

def multiprocess_analyze(file_path, shot_number):
	'''
	creates two processes to read and analyze the interferometer data from the scope network drive
	'''
	queue = multiprocessing.Queue()
	
	process1 = multiprocessing.Process(target=read_and_analyze, args=(file_path, shot_number, queue, 1, 2))
	process2 = multiprocessing.Process(target=read_and_analyze, args=(file_path, shot_number, queue, 3, 4))
	
	process1.start()
	process2.start()
	
	process1.join()
	process2.join()
	
	t_ms, neA = queue.get()
	t_ms, neB = queue.get()
	
	return t_ms, neA, neB
	
#===============================================================================================================================================
		
def main(hdf5_path="/media/interfpi/5C87-20CD", file_path ="/mnt/smbshare"):
	"""
	Main function for the interferometer program.

	Args:
		hdf5_path (str, optional): Path to the HDF5 file directory. Defaults to "/media/interfpi/5C87-20CD".
		file_path (str, optional): Path to the file directory. Defaults to "/mnt/smbshare".
	"""

	# Create a new HDF5 file; if it already exists, do nothing
	date = datetime.date.today()
	hdf5_ifn = f"{hdf5_path}/interferometer_data_{date}.hdf5"
	init_hdf5_file(hdf5_ifn)

	# Initialize the plot
	ax, line_A, line_B = init_plot()
	# Find the most recent shot in LeCroy Network drive 
	shot_number = find_latest_shot_number(file_path)
	
	while True:

		# Check if the day has changed; if so, create a new HDF5 file
		st = time.time() # current time
		if get_current_day(st) != get_current_day(os.path.getmtime(hdf5_ifn)):
			date = datetime.date.today()
			hdf5_ifn = f"{hdf5_path}/interferometer_data_{date}.hdf5"
			init_hdf5_file(hdf5_ifn)
		
		time.sleep(0) # sleep for CPU
		try:
			# Check if the interferometer data files are available on leCroy scope drive
			# C4 is the last channel to be saved
			ifn = f"{file_path}/C4-interf-shot{shot_number:05d}.trc"
			if not os.path.exists(ifn):
				time.sleep(0.05)
				continue
			saved_time=os.path.getmtime(ifn) # time when the shot data was saved

			# If the operation is too slow, skip the shot to catch up
			if st - saved_time > 1.5:
				print("Skip one shot to catch up in time")
				shot_number += 1
				time.sleep(0.01)
				continue

			print("Reading shot", shot_number)
			t_ms, neA, neB = multiprocess_analyze(file_path, shot_number)
#			print( np.array_equal(neA, neB) )

			# save data to HDF5 file as new datasets
			create_sourcefile_dataset(hdf5_ifn, neA, neB, t_ms, saved_time)
			
			# update the plot with the new data
			update_plot(ax, line_A, line_B, t_ms, neA, neB)
			
#			print("Time taken: ", time.time() - st)
			if shot_number == 99999: # reset shot number to 0 after reaching the maximum
				shot_number = 0
				continue
				
			shot_number += 1
			
		except KeyboardInterrupt:
			print("Keyboard interrupt detected. Exit program.")
			break
			
		except Exception as e:
			print("Error not specified: ", e)
			break

	end_plot() # plot persist on screen after the program ends

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == '__main__':
	main()
