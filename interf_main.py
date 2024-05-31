import sys

import h5py
import numpy as np
import time
import datetime
import os

from interf_raw import density_from_phase
from interf_plot import init_plot, update_plot, end_plot

#===============================================================================================================================================
def load_shot_data(temp_path, shot_number):
    # Load the .npz file
    file_path = f"{temp_path}/shot{shot_number:05d}.npz"
    with np.load(file_path) as data:
        refchA = data['refchA']
        plachA = data['plachA']
        refchB = data['refchB']
        plachB = data['plachB']
        tarr = data['tarr']
        saved_time = data['saved_time']
    return refchA, plachA, refchB, plachB, tarr, saved_time
#===============================================================================================================================================

def init_hdf5_file(file_name):
	if os.path.exists(file_name):
		print("HDF5 file exist")
		return None
	
	with h5py.File(file_name, "w") as f:
		ct = time.localtime()
		f.attrs['created'] = ct
		print("HDF5 file created ", time.strftime("%Y-%m-%d %H:%M:%S", ct))


def create_sourcefile_dataset(file_name, neA, neB, t_ms, saved_time):
	with h5py.File(file_name, "a") as f:
		grp = f.require_group("ne_p20")
		fds = grp.create_dataset(str(saved_time), data=neA)
		fds.attrs['modified'] = time.ctime(saved_time)

		grp = f.require_group("ne_p29")
		fds = grp.create_dataset(str(saved_time), data=neB)
		fds.attrs['modified'] = time.ctime(saved_time)

		grp = f.require_group("time_array")
		fds = grp.create_dataset(str(saved_time), data=t_ms)
		fds.attrs['modified'] = time.ctime(saved_time)

	print("Saved interferometer shot at", time.ctime(saved_time))
	
#===============================================================================================================================================
def main(temp_path):
	# Create an HDF5 file to store the data
	today = datetime.date.today()
	hdf5_file_name = f"interf_data_{today}.hdf5"
	init_hdf5_file(hdf5_file_name)

	shot_number = 0
	while True:
		
		ifn = f"{temp_path}/shot{shot_number:05d}.npz"
		if not os.path.exists(ifn): # Check if the file exists
			print("File not found. Exiting...")
			break

		try:
			st = time.time()
			
			print("Reading shot", shot_number)
			refchA, plachA, refchB, plachB, tarr, saved_time = load_shot_data(temp_path, shot_number)
			print("Data loaded")

			t_ms, neA = density_from_phase(tarr, refchA, plachA)
			t_ms, neB = density_from_phase(tarr, refchB, plachB)

			create_sourcefile_dataset(hdf5_file_name, neA, neB, tarr, saved_time)     

			dur = time.time() - st
			print("Time taken: ", dur)

			shot_number += 1

		except KeyboardInterrupt:
			print("Keyboard interrupt detected. Exiting...")
			break

		except Exception as e:
			print("Error: ", e)
			break

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================
# sudo mount.cifs //192.168.7.61/interf /home/smbshare -o username=LECROYUSER_2

if __name__ == '__main__':
	main()