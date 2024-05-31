# multi threading test
import numpy as np
import os
import time
import threading

from read_scope_data import read_trc_data_simplified, read_trc_data_no_header

# Example function to simulate saving data
def save_data_A(file_path, temp_path, lock):
	shot_number = 0
	while True:
		with lock:
			ifn = f"{file_path}/C1-interf-shot{shot_number:05d}.trc"            
			saved_time=os.path.getmtime(ifn)
			
			refchA, tarr, vertical_gain, vertical_offset = read_trc_data_simplified(ifn)
			data_size = len(tarr)

			ifn = f"{file_path}/C2-interf-shot{shot_number:05d}.trc"
			plachA = read_trc_data_no_header(ifn, data_size, vertical_gain, vertical_offset)
			
			np.savez(f"{temp_path}/shot{shot_number:05d}A.npz", refch=refchA, plach=plachA, tarr=tarr, saved_time=saved_time)
			print(f"Shot {shot_number:05d} A saved to temp folder")

# Example function to simulate processing data
def save_data_B(file_path, temp_path, lock):
	shot_number = 0
	while True:
		with lock:
			ifn = f"{file_path}/C3-interf-shot{shot_number:05d}.trc"            
			saved_time=os.path.getmtime(ifn)
			
			refchB, tarr, vertical_gain, vertical_offset = read_trc_data_simplified(ifn)
			data_size = len(tarr)

			ifn = f"{file_path}/C4-interf-shot{shot_number:05d}.trc"
			plachB = read_trc_data_no_header(ifn, data_size, vertical_gain, vertical_offset)
			
			np.savez(f"{temp_path}/shot{shot_number:05d}B.npz", refch=refchB, plachA=plachB, tarr=tarr, saved_time=saved_time)
			print(f"Shot {shot_number:05d} B saved to temp folder")


#--------------------------------- Main code ---------------------------------

file_path = "/home/smbshare" # Network drive located on LeCroy scope, mounted on RP 
temp_path = "/mnt/ramdisk" 	 # Temporary ramdisk on RP, see readme on desktop

# Lock for synchronizing access to shared resources
lock = threading.Lock()

# Create and start the threads
save_thread_A = threading.Thread(target=save_data_A, args=(file_path, temp_path, lock))
save_thread_B = threading.Thread(target=save_data_B, args=(file_path, temp_path, lock))

save_thread_A.start()
save_thread_B.start()

# Join the threads to the main thread to wait for their completion
save_thread_A.join()
save_thread_B.join()