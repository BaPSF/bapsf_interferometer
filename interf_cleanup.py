# coding utf-8
'''
This module removes interferometer raw data saved on the scope after they have been analyzed and saved to a HDF5 file.

Author: Jia Han
Ver1.0 created on: 2021-06-01
- Basic functionality to remove interferometer raw data files from scope after analysis
- Simple file deletion after log file written by interf_main.py

Ver2.0 created on: 2024-12-12 by AI (TODO: need to be tested)
- Added proper logging and error handling
- Improved file processing with shot number tracking
- Added verbose mode for debugging
- Designed to be called from interf_main.py rather than standalone
- More robust file existence checks and deletion confirmation
'''

import time
import os

#===============================================================================================================================================
file_path = r"I:\\"
log_ifn = r"C:\data\log\interferometer_log.bin"
#===============================================================================================================================================
def remove_file(ifn, verbose=False):
    if not os.path.exists(ifn):
        return False

    try:
        if os.path.isfile(ifn) or os.path.islink(ifn):
            os.unlink(ifn)
            if verbose:
                print(f"Removed file: {ifn}")
            return True
            
    except Exception as e:
        print(f"Failed to remove {ifn}. Reason: {e}")
        return False

def process_single_shot(shot_number, verbose=False):
    """Process and remove files for a single shot number"""
    success = True
    for ch in range(1, 5):
        ifn = f"{file_path}\\C{ch}-interf-shot{shot_number:05d}.trc"
        if not remove_file(ifn, verbose):
            success = False
    return success

def main(verbose=False):
    """
    Main function to process the log file and remove processed files.
    No threading - designed to be called from interf_main.py
    """
    while True:
        try:
            time.sleep(0.1)  # Prevent CPU overuse
            
            if not os.path.exists(log_ifn):
                if verbose:
                    print("Log file does not exist")
                continue
                
            # Read first line only
            try:
                with open(log_ifn, 'r') as log_file:
                    first_line = log_file.readline().strip()
                    if not first_line:
                        continue
            except (IOError, PermissionError):
                time.sleep(0.5)
                continue
                
            try:
                shot_number = int(first_line.split(',')[0])
            except (ValueError, IndexError):
                continue
                
            # Process the shot
            if process_single_shot(shot_number, verbose):
                # Remove the processed line from log
                try:
                    with open(log_ifn, 'r') as log_file:
                        lines = log_file.readlines()
                    with open(log_ifn, 'w') as log_file:
                        log_file.writelines(lines[1:])
                    if verbose:
                        print(f"Removed shot {shot_number}")
                except (IOError, PermissionError):
                    time.sleep(0.5)
                    continue
                    
        except KeyboardInterrupt:
            print("Cleanup: Keyboard interrupt")
            break
        except Exception as e:
            print(f"Cleanup error: {e}")
            time.sleep(1)
            continue

if __name__ == '__main__':
    main(verbose=True)