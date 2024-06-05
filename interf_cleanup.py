# coding utf-8

# This module removes interferometer raw data saved on the scope after they have been analyzed and saved to a HDF5 file.

import time
import os
import threading


file_path ="/mnt/smbshare"
ram_path="/mnt/ramdisk"
log_ifn = f"{ram_path}/interferometer_log.bin"

def remove_file(ifn):
	if not os.path.exists(ifn):
		return False

	try:
		if os.path.isfile(ifn) or os.path.islink(ifn):
			os.unlink(ifn)
			print(f"Removed file: {ifn}")
			return True
			
	except Exception as e:
		print(f"Failed to remove {ifn}. Reason: {e}")
		return False


def main():
	# Check if the log file exists
	if os.path.exists(log_ifn):
		log_file_exists = True
	else:
		print("Log file does not exist")
		log_file_exists = False

	while log_file_exists:
		time.sleep(0.1)
		try:
			# Read the log file to get the recorded shot numbers
			with open(log_ifn, 'r+') as log_file:
				log_data = log_file.readlines()
				# skip iteration if log file is empty
				if len(log_data) == 0:
					print('Log file is empty.')
					continue

			# Remove the newline characters from the shot numbers
			recorded_shot_numbers = [int(shot_number.split(',')[0].strip()) for shot_number in log_data]

			# Delete the corresponding interferometer files
			for shot_number in recorded_shot_numbers:
				ifn = f"{file_path}/C1-interf-shot{int(shot_number):05d}.trc"
				is_removed = remove_file(ifn)
				ifn = f"{file_path}/C2-interf-shot{int(shot_number):05d}.trc"
				is_removed = remove_file(ifn)
				ifn = f"{file_path}/C3-interf-shot{int(shot_number):05d}.trc"
				is_removed = remove_file(ifn)
				ifn = f"{file_path}/C4-interf-shot{int(shot_number):05d}.trc"
				is_removed = remove_file(ifn)

				if is_removed:
					# Remove entire line with the shot number from the log file
					with open(log_ifn, 'r+') as log_file:
						log_file.seek(0)
						for line in log_data:
							if int(line.split(',')[0].strip()) != shot_number:
								log_file.write(line)
						log_file.truncate()
					
					# Remove shot
					recorded_shot_numbers.remove(shot_number)
					print(f"Removed shot {shot_number}")


		except KeyboardInterrupt:
			print("Keyboard interrupt")
			break
		except Exception as e:
			if 'Permission denied' in str(e):
				# print('waiting for log file to be written')
				time.sleep(0.5)
				continue
			else:
				print(f"Error: {e}")
				break

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == '__main__':

	main()
