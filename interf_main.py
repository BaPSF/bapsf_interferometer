import sys
import multiprocessing
import h5py
import numpy as np
import time
import datetime
import os

from interf_raw import density_from_phase
from interf_plot import init_plot, update_plot, end_plot
from interf_save import find_latest_shot_number
from read_scope_data import read_trc_data_simplified, read_trc_data_no_header

#===============================================================================================================================================
def load_shot_data(file_path):
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

def delete_file(file_path):
    try:
        os.remove(file_path)
        print(f"File {file_path} has been deleted.")
    except FileNotFoundError:
        print(f"File {file_path} does not exist.")
    except Exception as e:
        print(f"Error deleting file {file_path}: {e}")
#===============================================================================================================================================
def main(temp_path):
	# Create an HDF5 file to store the data
	today = datetime.date.today()
	hdf5_file_name = f"interf_data_{today}.hdf5"
	init_hdf5_file(hdf5_file_name)

	shot_number = find_latest_shot_number(temp_path)
	while True:
		
		ifn = f"{temp_path}/shot{shot_number:05d}.npz"
		if not os.path.exists(ifn): # Check if the file exists
			print("File not found. Exiting...")
			break

		try:
			st = time.time()

			print("Reading shot", shot_number)
			refchA, plachA, refchB, plachB, tarr, saved_time = load_shot_data(ifn)
			print("Data loaded")

			t_ms, neA = density_from_phase(tarr, refchA, plachA)
			t_ms, neB = density_from_phase(tarr, refchB, plachB)

			create_sourcefile_dataset(hdf5_file_name, neA, neB, tarr, saved_time)     

			dur = time.time() - st
			print("Time taken: ", dur)
			
			delete_file(ifn)

		except KeyboardInterrupt:
			print("Keyboard interrupt detected. Exiting...")
			break

		except Exception as e:
			print("Error: ", e)
			break
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
			
def main_plot(temp_path):

	ax, line_A, line_B = init_plot()
	
	file_path = "/home/smbshare"
	shot_number = 48700 #find_latest_shot_number(file_path)
	
	tls = []
	
	while True:
#		file_ls = os.listdir(temp_path)
#		if not file_ls:
#			time.sleep(0.05)
#			continue
#		else:
#			print("found file")
			
#		full_path_file_ls = [os.path.join(temp_path, file) for file in file_ls]
#		ifn = full_path_file_ls[0]
#		shot_number = ifn[-9:-4]
		time.sleep(0)
		try:
			st = time.time()

			print("Reading shot", shot_number)

			queue = multiprocessing.Queue()
			process1 = multiprocessing.Process(target=read_and_analyze_A, args=(file_path, shot_number, queue))
			process2 = multiprocessing.Process(target=read_and_analyze_B, args=(file_path, shot_number, queue))
			
			process1.start()
			process2.start()
			
			process1.join()
			process2.join()
			
			t_ms, neA = queue.get()
			t_ms, neB = queue.get()

			print("plot")
#			update_plot(ax, line_A, line_B, t_ms, neA, neA)
			dur = time.time() - st
			print("Time taken: ", dur)
			tls.append(dur)
			
			shot_number += 1
			
			if shot_number == 48800:
				print(tls)
				break
		
		except KeyboardInterrupt:
			print("Keyboard interrupt detected. Exiting...")
			break

		except Exception as e:
			if "EOF" in str(e):
				print(e)
				delete_file(ifn)
				continue
			else:
				print("Error: ", e)
				break

	end_plot()
#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================
# 

if __name__ == '__main__':
	main_plot("/mnt/ramdisk")
