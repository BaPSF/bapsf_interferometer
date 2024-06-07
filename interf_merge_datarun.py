# coding utf-8
'''
Merge the interferometer data from the two channels to datarun hdf5 file
'''

import os
import h5py
import numpy as np
import time
import datetime

#===============================================================================================================================================

def get_start_timestamp(datarun_path):
    # Extract date and time from datarun hdf5 file using path name
    datarun_name = os.path.basename(datarun_path)
    datarun_name_parts = datarun_name.split("_")

    start_date = datarun_name_parts[2]
    start_time = datarun_name_parts[3][:-5]
    start_time = start_time.replace(".", ":")

    # Convert start_date and start_time to seconds since epoch
    start_datetime = datetime.datetime.strptime(start_date + " " + start_time, "%Y-%m-%d %H:%M:%S")
    start_timestamp = int(start_datetime.timestamp())

    print(start_datetime)
    return start_timestamp


def init_datarun_groups(f_datarun, f_interf):
    # Create interferometer groups in datarun file
    grp = f_datarun.require_group("interferometer")
    # grp.attrs['description'] = f_interf.attrs['description']

    grp_p20 = f_datarun.require_group("interferometer/phase_p20")
    for attr_name, attr_value in f_interf['phase_p20'].attrs.items():
        if attr_name not in grp.attrs:
            grp.attrs[attr_name] = attr_value

    grp_p29 = f_datarun.require_group("interferometer/phase_p29")
    for attr_name, attr_value in f_interf['phase_p29'].attrs.items():
        if attr_name not in grp.attrs:
            grp.attrs[attr_name] = attr_value
    
    grp_tarr = f_datarun.require_group("interferometer/time_array")
    for attr_name, attr_value in f_interf['time_array'].attrs.items():
        if attr_name not in grp.attrs:
            grp.attrs[attr_name] = attr_value

    print('Interferometer groups created/loaded in datarun file')


def merge_interferometer_data(f_interf, f_datarun, start_timestamp):
    n = 10  # number of iterations
    duration = 30*60  # duration in seconds
    
    # get the groups and sets from the interferometer data
    groups = list(f_interf.keys())
    sets = list(f_interf[groups[0]].keys())

    # Iterate over the sets for n times over duration seconds
    # Copy data from interferometer hdf5 file into datarun file
    for i in range(n):

        timestamp = start_timestamp + (i * duration)
        closest_set = min(sets, key=lambda x: abs(float(x) - timestamp))

        # get the data from the closest set
        p20 = f_interf['phase_p20'][closest_set][:]
        p29 = f_interf['phase_p29'][closest_set][:]
        time_array = f_interf['time_array'][closest_set][:]

        # create datasets in datarun file if they don't already exist
        if closest_set not in f_datarun['interferometer/phase_p20']:
            f_datarun['interferometer/phase_p20'].create_dataset(closest_set, data=p20)
        if closest_set not in f_datarun['interferometer/phase_p29']:
            f_datarun['interferometer/phase_p29'].create_dataset(closest_set, data=p29)
        if closest_set not in f_datarun['interferometer/time_array']:
            f_datarun['interferometer/time_array'].create_dataset(closest_set, data=time_array)

        print('Data copied for set:', closest_set)

        if closest_set == sets[-1]:
            print('End of interferometer data reached. Exiting.')
            break

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == '__main__':
      
    interf_path = r"C:\data\LAPD\interferometer_data_2024-06-05.hdf5"
    datarun_path = r"C:\data\LAPD\JAN2024_diverging_B\00_test-sequential-motion_2024-01-25_18.56.30.hdf5"
    
    # open hdf5 files
    f_interf = h5py.File(interf_path, "r")
    f_datarun = h5py.File(datarun_path, "a")
    
    init_datarun_groups(f_datarun, f_interf)

    # set name in sets are timestamps in seconds since epoch
    # find the closest set to the start_timestamp
    start_timestamp = get_start_timestamp(datarun_path)
    
    merge_interferometer_data(f_interf, f_datarun, start_timestamp)

    f_interf.close()
    f_datarun.close()

    print('Interferometer data merged into datarun file. File closed.')