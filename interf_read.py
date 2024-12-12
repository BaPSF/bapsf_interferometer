# coding: utf-8
'''
Created on: 2024-12-12
Author: Jia Han

Functions for reading interferometer data from HDF5 files.

This module provides functions to read interferometer data that has been saved in HDF5 format (see the rest of files in Bapsf_interferometer repository).
The main function get_interf_data() allows retrieving phase data for a specific date and time from the interferometer data files.

The interferometer data consists of:
1. Phase data from port 20 (phase_p20):
   - Phase shift measurements in radians
   - Microwave frequency: 288 GHz
   - Calibration factor for density conversion (assumes 40cm plasma length)

2. Phase data from port 29 (phase_p29):
   - Phase shift measurements in radians
   - Microwave frequency: 282 GHz
   - Calibration factor for density conversion (assumes 40cm plasma length)

3. Time array (time_array):
   - Time points in milliseconds
   - Corresponds to each phase measurement

The data is stored in HDF5 files with naming format: interferometer_data_YYYY-MM-DD.hdf5
'''
import os
import h5py
import numpy as np
import matplotlib.pyplot as plt
import datetime
from interf_raw import get_calibration_factor

def get_interf_data(year, month, day, hour, minute, second, data_path):
    """
    Get interferometer data from HDF5 files for a specific date and time (Pacific Time).
    
    Args:
        year (int): Year (e.g., 2024)
        month (int): Month (1-12)
        day (int): Day (1-31)
        hour (int): Hour (0-23)
        minute (int): Minute (0-59)
        second (int): Second (0-59)
        data_path (str): Path to directory containing interferometer HDF5 files
    
    Returns:
        tuple: (t_ms, phaseA, phaseB, actual_time) or (None, None, None, None) if no data found
            - t_ms: Time array in milliseconds
            - phaseA: Phase data from port 20
            - phaseB: Phase data from port 29
            - actual_time: String representing actual time of data found
    """
    # Convert input time to timestamp (Pacific Time)
    pacific = datetime.timezone(datetime.timedelta(hours=-8))  # PST (UTC-8)
    input_dt = datetime.datetime(year, month, day, hour, minute, second, tzinfo=pacific)
    input_timestamp = input_dt.timestamp()
    
    # Construct filename
    date_str = f"{year:04d}-{month:02d}-{day:02d}"
    filename = f"interferometer_data_{date_str}.hdf5"
    file_path = os.path.join(data_path, filename)
    
    if not os.path.exists(file_path):
        print(f"No interferometer data file found for date {date_str}")
        return None, None, None, None
        
    with h5py.File(file_path, 'r') as f:
        # Get all timestamps in the file
        dataset_names = list(f['phase_p20'].keys())
        timestamps = np.array([float(ts) for ts in dataset_names])
        
        # Find closest timestamp within ±10 seconds
        time_diffs = np.abs(timestamps - input_timestamp)
        closest_idx = np.argmin(time_diffs)
        
        if time_diffs[closest_idx] > 10:  # More than 10 seconds difference
            print(f"No data found within 10 seconds of {input_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            return None, None, None, None
            
        actual_timestamp = str(timestamps[closest_idx])
        
        # Get data
        phaseA = np.array(f['phase_p20'][actual_timestamp])
        phaseB = np.array(f['phase_p29'][actual_timestamp])
        t_ms = np.array(f['time_array'][actual_timestamp])
        
        # Convert actual timestamp to readable time string
        actual_time = datetime.datetime.fromtimestamp(float(actual_timestamp), pacific)
        actual_time_str = actual_time.strftime("%Y-%m-%d %H:%M:%S %Z")
        
    return t_ms, phaseA, phaseB, actual_time_str

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

# Example usage:
if __name__ == "__main__":
    data_path = r"D:\interferometer"
    
    # Example: Get data for June 8, 2024 at 14:30:00 PST
    t_ms, phaseA, phaseB, actual_time = get_interf_data(
        year=2024, 
        month=11, 
        day=2, 
        hour=10, 
        minute=30, 
        second=0, 
        data_path=data_path
    )
    
    if t_ms is not None:
        print(f"Found data at: {actual_time}")
        
        # Convert phase to density if needed
        cal_20 = get_calibration_factor(288e9)  # For port 20
        cal_29 = get_calibration_factor(282e9)  # For port 29
        
        ne_20 = phaseA * cal_20
        ne_29 = phaseB * cal_29
        
        # Plot the data
        plt.figure(figsize=(10,6))
        plt.plot(t_ms, ne_20, label='Port 20')
        plt.plot(t_ms, ne_29, label='Port 29')
        plt.xlabel('Time (ms)')
        plt.ylabel('Density (m^-3)')
        plt.legend()
        plt.grid(True)
        plt.show()
    else:
        print("No data found")