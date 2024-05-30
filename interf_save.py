import sys
import numpy as np
import time
import os

from read_scope_data import read_trc_data_simplified


#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

def read_shot(shot_number):
	save_data = np.load(f"{temp_path}/shot{shot_number:05d}.npy", allow_pickle=True)
	return save_data.item()

def write_to_temp(file_path, temp_path):
	shot_number = 0
	while True:
		try:
			st = time.time()

			ifn = f"{file_path}/C1-topo-22-12-05-shot{shot_number:05d}.trc"
			
			if not os.path.exists(ifn):
				print(f"Shot {shot_number:05d} does not exist")
				break
			
			refchA, tarr = read_trc_data_simplified(ifn)

			ifn = f"{file_path}/C2-topo-22-12-05-shot{shot_number:05d}.trc"
			plachA, tarr = read_trc_data_simplified(ifn)

			ifn = f"{file_path}/C1-topo-22-12-05-shot{shot_number:05d}.trc"
			refchB, tarr = read_trc_data_simplified(ifn)

			ifn = f"{file_path}/C2-topo-22-12-05-shot{shot_number:05d}.trc"
			plachB, tarr = read_trc_data_simplified(ifn)

			save_data = {"refchA": refchA, "plachA": plachA, "refchB": refchB, "plachB": plachB, "tarr": tarr, "saved_time": os.path.getmtime(ifn)}
			np.save(f"{temp_path}/shot{shot_number:05d}.npy", save_data)
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
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == '__main__':
	
	if True:
		file_path = "/home/interfpi"
		temp_path = "/mnt/ramdisk"
		write_to_temp(file_path, temp_path)
	
	if False:
		data = read_shot(0)
		print(data.keys())
