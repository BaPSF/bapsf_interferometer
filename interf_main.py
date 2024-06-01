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
	ct = time.localtime(timestamp)
	return (ct.tm_year, ct.tm_mon, ct.tm_mday)

#===============================================================================================================================================
def read_and_analyze_A(file_path, shot_number, queue):
	
	ifn = f"{file_path}/C1-interf-shot{shot_number:05d}.trc"
	refch, tarr, vertical_gain, vertical_offset = read_trc_data_simplified(ifn)
	data_size = len(tarr)

	ifn = f"{file_path}/C2-interf-shot{shot_number:05d}.trc"
	plach = read_trc_data_no_header(ifn, data_size, vertical_gain, vertical_offset)
	
	t_ms, ne = density_from_phase(tarr, refch, plach)
	
	queue.put((t_ms, ne))
	
def read_and_analyze_B(file_path, shot_number, queue):
	
	ifn = f"{file_path}/C3-interf-shot{shot_number:05d}.trc"
	refch, tarr, vertical_gain, vertical_offset = read_trc_data_simplified(ifn)
	data_size = len(tarr)

	ifn = f"{file_path}/C4-interf-shot{shot_number:05d}.trc"
	plach = read_trc_data_no_header(ifn, data_size, vertical_gain, vertical_offset)
	
	t_ms, ne = density_from_phase(tarr, refch, plach)
	
	queue.put((t_ms, ne))

def multiprocess_analyze(file_path, shot_number):
	
	queue = multiprocessing.Queue()
	process1 = multiprocessing.Process(target=read_and_analyze_A, args=(file_path, shot_number, queue))
	process2 = multiprocessing.Process(target=read_and_analyze_B, args=(file_path, shot_number, queue))
	
	process1.start()
	process2.start()
	
	process1.join()
	process2.join()
	
	t_ms, neA = queue.get()
	t_ms, neB = queue.get()
	
	return t_ms, neA, neB
	
#===============================================================================================================================================
		
def main(hdf5_path="/media/interfpi/5C87-20CD", file_path ="/mnt/smbshare"):

	date = datetime.date.today()
	hdf5_ifn = f"{hdf5_path}/interferometer_data_{date}.hdf5"
	init_hdf5_file(hdf5_ifn)

	ax, line_A, line_B = init_plot()
	shot_number = find_latest_shot_number(file_path)
	
	while True:
		st = time.time()
		
		time.sleep(0.001)
		try:
			ifn = f"{file_path}/C4-interf-shot{shot_number:05d}.trc"
			if not os.path.exists(ifn):
				time.sleep(0.05)
				continue
			saved_time=os.path.getmtime(ifn)
			
			
			if st - saved_time > 1.5:
				print("Skip one shot to catch up in time")
				shot_number += 1
				time.sleep(0.01)
				continue

			print("Reading shot", shot_number)

			t_ms, neA, neB = multiprocess_analyze(file_path, shot_number)
			
			create_sourcefile_dataset(hdf5_ifn, neA, neB, t_ms, saved_time)
			update_plot(ax, line_A, line_B, t_ms, neA, neA)
			print("Time taken: ", time.time() - st)
			
			if shot_number == 99999:
				shot_number = 0
				continue
				
			shot_number += 1
			
		
		except KeyboardInterrupt:
			print("Keyboard interrupt detected. Exit program.")
			break
			
		except Exception as e:
			print("Error not specified: ", e)
			break

	end_plot()
#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================
# 

if __name__ == '__main__':
	main()
