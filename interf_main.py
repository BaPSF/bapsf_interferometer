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
import signal
import multiprocessing
import concurrent.futures
import threading
from queue import Queue, Full as QueueFull
import h5py
import numpy as np
import time
import datetime
import os

from interf_raw import phase_from_raw, get_calibration_factor
from interf_file import find_latest_shot_number, init_hdf5_file, create_sourcefile_dataset
from lab_scopes.io.lecroy_files import read_trc_data_simplified, read_trc_data_no_header
from lab_scopes.rigol import RigolDHO800
from interf_cleanup import process_single_shot


#===============================================================================================================================================
scope_path = r"I:\\"
log_path = r"C:\data\\log"
hdf5_path = r"C:\data\\interferometer"

RIGOL_IP = "192.168.7.63"
RIGOL_REF_CH = "C1"
RIGOL_PLA_CH = "C2"
RIGOL_RETRY_INTERVAL = 100  # shots between reconnect attempts when scope is offline
RIGOL_CONNECT_TIMEOUT = 3.0
# Hard cap on the Rigol stop/read/run, measured from stop(). Lowered from 5.0 so a
# stalled (not merely slow) scope is dropped quickly and the loop stays current.
# Tradeoff: a genuinely slow-but-alive read may be abandoned and that shot's phase_p40
# written as missing.
# DEPTH CONSTRAINT: read_rigol() reads BOTH channels sequentially (ref then pla), so
# the budget covers two records, not one. Each read scales with Rigol memory depth
# (WORD = 2 bytes/sample, ~2.6 MB/s measured on DHO804 fw 00.01.05): 1M ~= 0.77 s each,
# so ~1.5 s for the pair (fits under 2.5 s, with limited headroom). Keep the
# interferometer Rigol at <=1M -- the pair already nears the cap there, and deeper
# records will exceed 2.5 s and be silently dropped every shot. Raise this cap only if
# you also accept a stalled scope freezing the loop that much longer.
RIGOL_OPERATION_TIMEOUT = 2.5
RIGOL_SOCKET_TIMEOUT = 2.0

# Catch-up: when one shot takes longer than this to process, we've fallen behind, so
# jump shot_number to the newest shot on disk instead of crawling forward one at a time.
CATCHUP_PROC_TIME = 3.0
# Guard against jumping backward across the 99999->0 wraparound: only jump when the
# modular gap to the latest shot is positive and smaller than this.
CATCHUP_GAP_MAX = 50000
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
	Read both Rigol channels. read_channel blocks until scope is STOP.
	:RUN is sent in finally so the scope always resumes normal triggering.
	'''
	try:
		wref = scope.read_channel(RIGOL_REF_CH)
		wpla = scope.read_channel(RIGOL_PLA_CH)
		tarr = wref.time
		ref = wref.voltage.astype(np.float64)
		pla = wpla.voltage.astype(np.float64)
	finally:
		try:
			scope.run()
		except Exception:
			pass
	return tarr, ref - np.mean(ref), pla - np.mean(pla)


def run_with_timeout(func, timeout_s, *args):
	'''
	Run blocking Rigol work in a short-lived worker so a stalled telnet call
	does not hold up the LeCroy acquisition loop forever.
	'''
	executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
	future = executor.submit(func, *args)
	try:
		return future.result(timeout=timeout_s)
	except concurrent.futures.TimeoutError as e:
		future.cancel()
		raise TimeoutError(f"{func.__name__} timed out after {timeout_s:.1f} s") from e
	finally:
		executor.shutdown(wait=False, cancel_futures=True)


def open_rigol():
	# RigolDHO800 passes ``timeout`` straight to socket.create_connection, so it
	# also becomes the per-recv timeout -- no separate set_rigol_timeout needed.
	return RigolDHO800(RIGOL_IP, timeout=RIGOL_SOCKET_TIMEOUT, verbose=False)


def try_open_rigol():
	'''
	Open a RigolDHO800 connection, returning None on any failure
	(missing scope, network error, etc.) so the LeCroy pipeline can keep going.
	'''
	try:
		return run_with_timeout(open_rigol, RIGOL_CONNECT_TIMEOUT)
	except Exception as e:
		print(f"Rigol unavailable: {e}")
		return None


def read_stopped_rigol(scope):
	scope.stop()
	return read_rigol(scope)


def interpolate_rigol_phase(t_ms, t_ms_C_raw, phaseC_raw):
	'''
	Interpolate Rigol phase onto LeCroy time only when the time base is sane.
	This avoids silently clamping, unit-mismatching, or using corrupt Rigol data.
	'''
	target_t = np.asarray(t_ms, dtype=float)
	rigol_t = np.asarray(t_ms_C_raw, dtype=float)
	rigol_phase = np.asarray(phaseC_raw, dtype=float)

	if target_t.size == 0:
		raise ValueError("empty LeCroy time array")
	if rigol_t.size < 2:
		raise ValueError("Rigol time array has fewer than two points")
	if rigol_t.shape != rigol_phase.shape:
		raise ValueError("Rigol time and phase arrays have different lengths")
	if not (np.all(np.isfinite(target_t)) and np.all(np.isfinite(rigol_t)) and np.all(np.isfinite(rigol_phase))):
		raise ValueError("Rigol interpolation input contains NaN or inf")
	if np.any(np.diff(rigol_t) <= 0):
		raise ValueError("Rigol time array is not strictly increasing")

	target_min = np.min(target_t)
	target_max = np.max(target_t)
	rigol_min = rigol_t[0]
	rigol_max = rigol_t[-1]
	eps = max(np.finfo(float).eps, abs(rigol_max - rigol_min) * 1e-9)
	if target_min < rigol_min - eps or target_max > rigol_max + eps:
		raise ValueError(f"Rigol time range [{rigol_min:.6g}, {rigol_max:.6g}] does not cover LeCroy range [{target_min:.6g}, {target_max:.6g}]")

	return np.interp(target_t, rigol_t, rigol_phase)


def restart_pool(pool):
	try:
		pool.terminate()
		pool.join()
	except Exception:
		pass
	return multiprocessing.Pool(initializer=_worker_init)


def _worker_init():
	# Ignore SIGINT in pool workers; only the main process handles Ctrl-C.
	# On Windows, Ctrl-C is broadcast to all console processes — without this,
	# workers raise KeyboardInterrupt mid-task and stall pool.join().
	signal.signal(signal.SIGINT, signal.SIG_IGN)
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

	pool = multiprocessing.Pool(initializer=_worker_init)

	cleanup_queue = Queue(maxsize=1000)

	def _cleanup_worker():
		while True:
			shot = cleanup_queue.get()
			if shot is None:
				return
			try:
				process_single_shot(shot)
			except Exception as e:
				print(f"Cleanup worker: failed to delete shot {shot}: {e}")

	threading.Thread(target=_cleanup_worker, daemon=True).start()

	shutdown_event = threading.Event()

	def sigint_handler(signum, frame):
		print("SIGINT (Ctrl-C) detected. Attempting to exit gracefully...")
		shutdown_event.set()

	signal.signal(signal.SIGINT, sigint_handler)

	scope = try_open_rigol()
	shots_since_retry = 0

	try:
		while not shutdown_event.is_set():
			rigol_executor = None  # bound before any raise so the except handler can clean it up
			try:
				time.sleep(0.01)
				if shutdown_event.is_set():
					break
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

				# Launch the Rigol read in a thread so it overlaps the LeCroy disk
				# reads + phase analysis instead of serializing in front of them.
				# stop() fires near-immediately on the worker thread, freezing the
				# Rigol record for this shot. The Rigol socket is not picklable and
				# not thread-safe across callers, so it can't go in the pool; the
				# thread owns it exclusively between submit() and result().
				# NOTE: this overlap is a throughput change only -- the two scopes
				# remain independently triggered and are NOT clock-synced.
				have_rigol = False
				rigol_missing_reason = "scope offline"
				tarr_C, refC, plaC = None, None, None
				rigol_future = None
				rigol_submit_t = None
				if scope is not None:
					rigol_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
					rigol_future = rigol_executor.submit(read_stopped_rigol, scope)
					rigol_submit_t = time.time()

				# LeCroy reads in parallel via pool (overlapping the Rigol read)
				rA = pool.apply_async(read_lecroy, (file_path, shot_number, 1, 2))
				rB = pool.apply_async(read_lecroy, (file_path, shot_number, 3, 4))
				tarr_A, refA, plaA = rA.get()
				tarr_B, refB, plaB = rB.get()

				# LeCroy phase analyses in parallel
				pA = pool.apply_async(phase_from_raw, (tarr_A, refA, plaA))
				pB = pool.apply_async(phase_from_raw, (tarr_B, refB, plaB))
				t_ms, phaseA = pA.get()
				_,    phaseB = pB.get()

				# Join the Rigol read now (after the LeCroy work it overlapped),
				# preserving the hard timeout cap measured from stop().
				if rigol_future is not None:
					try:
						remaining = max(0.0, RIGOL_OPERATION_TIMEOUT - (time.time() - rigol_submit_t))
						tarr_C, refC, plaC = rigol_future.result(timeout=remaining)
						have_rigol = True
						rigol_missing_reason = ""
					except Exception as e:
						print(f"Rigol error ({e}); disabling until next retry")
						rigol_missing_reason = str(e)
						scope.close()
						scope = None
						shots_since_retry = 0
					finally:
						rigol_executor.shutdown(wait=False, cancel_futures=True)

				# Rigol phase analysis (only once its read succeeded)
				if have_rigol:
					pC = pool.apply_async(phase_from_raw, (tarr_C, refC, plaC))
				INTERPOLATE_RIGOL = False  # set True to resample Rigol onto LeCroy time grid

				if have_rigol:
					t_ms_C_raw, phaseC_raw = pC.get()
					if INTERPOLATE_RIGOL:
						try:
							phaseC = interpolate_rigol_phase(t_ms, t_ms_C_raw, phaseC_raw)
							t_ms_C = t_ms
							rigol_missing = False
						except Exception as e:
							print(f"Rigol interpolation invalid ({e}); saving raw Rigol data")
							t_ms_C = t_ms_C_raw
							phaseC = phaseC_raw
							rigol_missing = False
					else:
						t_ms_C = t_ms_C_raw
						phaseC = phaseC_raw
						rigol_missing = False
				else:
					t_ms_C = np.array([], dtype=float)
					phaseC = np.array([], dtype=float)
					rigol_missing = True

				# Save the data to the HDF5 file
				f = h5py.File(hdf5_ifn, 'a', libver='latest')
				try:
					fc_day = f.attrs['created'][-2] # Check if the day has changed
					cd = get_current_day(st)
					if fc_day != cd: # if so, create a new HDF5 file
						f.close()
						date = datetime.date.today()
						hdf5_ifn = f"{hdf5_path}\\interferometer_data_{date}.hdf5"
						init_hdf5_file(hdf5_ifn)
						f = h5py.File(hdf5_ifn, 'a', libver='latest')

					# save data to HDF5 file as new datasets
					create_sourcefile_dataset(f, phaseA, phaseB, phaseC, t_ms, t_ms_C, saved_time, rigol_missing, rigol_missing_reason)
				finally:
					f.close()

				# Enqueue for background deletion of .trc files on I:\
				try:
					cleanup_queue.put_nowait(shot_number)
				except QueueFull:
					print(f"Cleanup queue full; dropping shot {shot_number} (will remain on I:\\)")
				except Exception as e:
					print(f"Cleanup enqueue error for shot {shot_number}: {e}")

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

				# If this shot was slow to process, we've fallen behind. Jump straight
				# to the newest shot on disk to drain the whole backlog in one step,
				# rather than crawling forward one shot at a time. Only scan the SMB
				# directory when we're measurably slow (keeps the fast path cheap).
				proc_time = time.time() - st
				if proc_time > CATCHUP_PROC_TIME:
					latest = find_latest_shot_number(file_path)
					# Modular gap guards the 99999->0 wraparound and prevents jumping
					# backward (a near-100000 gap means latest is behind us already).
					gap = (latest - shot_number) % 100000
					if 0 < gap < CATCHUP_GAP_MAX:
						print(f"Behind by {gap} shots ({proc_time:.1f}s); jumping to shot {latest}")
						shot_number = latest


			except OSError:
				print("Unable to open hdf5 file. Try again...")
				time.sleep(0.5)
				continue
			except Exception as e:
				if rigol_executor is not None:
					rigol_executor.shutdown(wait=False, cancel_futures=True)
				if shutdown_event.is_set():
					break
				print(f"An error occurred: {e}")
				pool = restart_pool(pool)
				print("Worker pool restarted. Continuing acquisition.")
				time.sleep(0.5)
	finally:
		if scope is not None:
			scope.close()

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == '__main__':

	main(hdf5_path, scope_path, log_path)
