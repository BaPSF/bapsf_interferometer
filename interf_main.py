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

Ver1.3 updated on: 2024-12-12
- Change log writing such that writes every 10 shots
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

#TODO: add path here for import
sys.path.append(r"...\bapsf_dimagnetic")

from interf_raw import phase_from_raw, get_calibration_factor
from interf_file import find_latest_shot_number, init_hdf5_file, create_sourcefile_dataset
from read_scope_data import read_trc_data_simplified, read_trc_data_no_header
from rigol_scope import RigolScope
from rigol_functions import command
import interf_cleanup as cleanup


#===============================================================================================================================================
scope_path = r"I:\\"
log_path = r"C:\data\\log"
hdf5_path = r"C:\data\\interferometer"

RIGOL_IP = "192.168.7.60"
RIGOL_REF_CH = "C1"
RIGOL_PLA_CH = "C2"
RIGOL_RETRY_INTERVAL = 100  # shots between reconnect attempts when scope is offline
#===============================================================================================================================================

def get_current_day(timestamp):
	'''
	gets current day from the timestamp
	'''
	ct = time.localtime(timestamp)
	return ct.tm_yday

#===============================================================================================================================================

def read_lecroy(file_path, shot_number, refch_i, plach_i):
	'''
	Read a LeCroy ref/plasma channel pair from the network share.
	Returns raw arrays (no analysis) so the pool can run reads and analyses
	in two separate parallel phases.
	'''
	ifn = f"{file_path}\\C{refch_i}-interf-shot{shot_number:05d}.trc"
	refch, tarr, _, _ = read_trc_data_simplified(ifn)

	ifn = f"{file_path}\\C{plach_i}-interf-shot{shot_number:05d}.trc"
	plach, _, _, _ = read_trc_data_simplified(ifn)

	return tarr, refch - np.mean(refch), plach - np.mean(plach)


def read_rigol(scope):
	'''
	Read both Rigol channels. Caller is responsible for sending :STOP first.
	:RUN is sent in finally so the scope always resumes auto-triggering.
	'''
	try:
		tarr = scope.time_array(RIGOL_REF_CH)
		ref, _ = scope.acquire(RIGOL_REF_CH)
		pla, _ = scope.acquire(RIGOL_PLA_CH)
	finally:
		try:
			command(scope.tn, ':RUN')
		except Exception:
			pass
	return tarr, ref - np.mean(ref), pla - np.mean(pla)


def try_open_rigol():
	'''
	Open a RigolScope connection, returning None on any failure
	(missing scope, network error, etc.) so the LeCroy pipeline can keep going.
	'''
	try:
		return RigolScope(RIGOL_IP, verbose=False)
	except Exception as e:
		print(f"Rigol unavailable: {e}")
		return None
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

	pool = multiprocessing.Pool()
	# Define a handler for SIGINT
	def sigint_handler(signum, frame):
		print("SIGINT (Ctrl-C) detected. Attempting to exit gracefully...")
		pool.terminate()
		pool.join()
		print("Cleanup complete. Exiting.")
		sys.exit(0)  # Exit the program

	# Set the SIGINT handler
	signal.signal(signal.SIGINT, sigint_handler)

	scope = try_open_rigol()
	shots_since_retry = 0

	try:
		while True:
			try:
				time.sleep(0.01)
				st = time.time() # start time of the loop

				# Check if the interferometer data files are available on leCroy scope drive
				# C4 is the last channel to be saved
				ifn = f"{file_path}\\C4-interf-shot{shot_number:05d}.trc"
				if not os.path.exists(ifn):
					continue
				saved_time=os.path.getctime(ifn) # time when the shot data was saved
				td = st - saved_time
				# print("Time difference: ", round(td,2))

				print("Shot ", shot_number)

				# Periodically retry the Rigol connection while it's offline
				if scope is None:
					shots_since_retry += 1
					if shots_since_retry >= RIGOL_RETRY_INTERVAL:
						shots_since_retry = 0
						scope = try_open_rigol()
						if scope is not None:
							print("Rigol reconnected")

				# Freeze Rigol immediately + read it. Any failure disables the
				# scope; the LeCroy pipeline keeps writing zeros for phase_p40.
				have_rigol = False
				tarr_C, refC, plaC = None, None, None
				if scope is not None:
					try:
						command(scope.tn, ':STOP')
						tarr_C, refC, plaC = read_rigol(scope)
						have_rigol = True
					except Exception as e:
						print(f"Rigol error ({e}); disabling until next retry")
						try:
							scope.disconnect()
						except Exception:
							pass
						scope = None
						shots_since_retry = 0

				# LeCroy reads in parallel via pool
				rA = pool.apply_async(read_lecroy, (file_path, shot_number, 1, 2))
				rB = pool.apply_async(read_lecroy, (file_path, shot_number, 3, 4))
				tarr_A, refA, plaA = rA.get()
				tarr_B, refB, plaB = rB.get()

				# All three phase analyses in parallel
				pA = pool.apply_async(phase_from_raw, (tarr_A, refA, plaA))
				pB = pool.apply_async(phase_from_raw, (tarr_B, refB, plaB))
				if have_rigol:
					pC = pool.apply_async(phase_from_raw, (tarr_C, refC, plaC))
				t_ms, phaseA = pA.get()
				_,    phaseB = pB.get()
				if have_rigol:
					t_ms_C_raw, phaseC_raw = pC.get()
					# Resample Rigol phase onto the LeCroy time grid so all
					# three traces share the same length and time base.
					phaseC = np.interp(t_ms, t_ms_C_raw, phaseC_raw)
					t_ms_C = t_ms
					rigol_missing = False
				else:
					# Save zero-filled placeholder so phase_p40 stays aligned
					# with the other groups; flag it in dataset metadata.
					t_ms_C = t_ms
					phaseC = np.zeros_like(phaseA)
					rigol_missing = True

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
				create_sourcefile_dataset(f, phaseA, phaseB, phaseC, t_ms, t_ms_C, saved_time, rigol_missing)
				f.close()

				# Buffer log entries and write every 10 shots
				try:
					if not hasattr(main, 'log_buffer'):
						main.log_buffer = []
					main.log_buffer.append(f"{shot_number},{saved_time}\n")

					if len(main.log_buffer) >= 10:
						with open(log_ifn, 'a') as log_file:
							log_file.writelines(main.log_buffer)
						main.log_buffer = []
				except Exception as e:
					print(f"Error writing to log: {e}")


				# Update shot number
				if shot_number == 99999:
					shot_number = 0 # reset shot number to 0 after reaching the maximum
				else:
					shot_number += 1

				# If the operation is too slow, skip the shot to catch up
				if td > 3:
					print("Skip one shot")
					shot_number += 1


			except KeyboardInterrupt:
				print("Keyboard interrupt detected. Exit program.")
				pool.terminate()
				pool.join()
				print("Cleanup complete. Exiting.")
				break
			except OSError:
				print("Unable to open hdf5 file. Try again...")
				time.sleep(0.5)
				continue
			except Exception as e:
				print(f"An error occurred: {e}")
				pool.terminate()
				pool.join()
				print("Cleanup complete. Exiting.")
	finally:
		if scope is not None:
			try:
				scope.disconnect()
			except Exception:
				pass

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == '__main__':

	main(hdf5_path, scope_path, log_path)