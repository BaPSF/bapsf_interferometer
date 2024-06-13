# coding utf-8
'''
Merge the interferometer data from the two channels to datarun hdf5 file
'''

import os
import h5py
import numpy as np
import time
import datetime

from read_hdf5 import unpack_datarun_sequence
from interf_raw import get_calibration_factor

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

def get_shot_timestamps(datarun_path):
    # Extract the sequence of shots from the datarun file
    f = h5py.File(datarun_path, "r")
    message_array, status_array, all_timestamp_array = unpack_datarun_sequence(f)
    f.close()

    # Correct timestamps
    timestamp_array = np.array([])
    for i, timestamp in enumerate(all_timestamp_array):
        
        # Check if the saved message correspond to an actual completed shot
        if ('Shot number' in message_array[i]) and (status_array[i] == 'Completed'):

            t_corrected = timestamp - 2082844800
            timestamp_array = np.append(timestamp_array, t_corrected)

#            t = time.gmtime(timestamp)
#            t_corrected = time.struct_time((2024, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, t.tm_wday, t.tm_yday, t.tm_isdst))

    return timestamp_array

#===============================================================================================================================================

def init_datarun_groups(datarun_path, interf_path):

    with h5py.File(datarun_path, "a") as f_datarun:
        with h5py.File(interf_path, "r") as f_interf:

            # Create interferometer groups in datarun file
            grp = f_datarun.require_group("interferometer")
            if 'description' in f_interf.attrs:
                grp.attrs['description'] = f_interf.attrs['description']

            grp_p20 = f_datarun.require_group("diagnostics/interferometer/phase_p20")
            for attr_name, attr_value in f_interf['phase_p20'].attrs.items():
                if attr_name not in grp.attrs:
                    grp.attrs[attr_name] = attr_value

            grp_p29 = f_datarun.require_group("diagnostics/interferometer/phase_p29")
            for attr_name, attr_value in f_interf['phase_p29'].attrs.items():
                if attr_name not in grp.attrs:
                    grp.attrs[attr_name] = attr_value
            
            grp_tarr = f_datarun.require_group("diagnostics/interferometer/time_array")
            for attr_name, attr_value in f_interf['time_array'].attrs.items():
                if attr_name not in grp.attrs:
                    grp.attrs[attr_name] = attr_value

    print('Interferometer groups created/loaded in datarun file')

def write_attribute(ifn):
    # used for interferometer data writen before 2024.06.xx while attributes and descriptions were not included
    with h5py.File(ifn, "a") as f:

        grp = f.require_group("diagnostics/interferometer/phase_p20")
        if len(grp.attrs) == 0:
            grp.attrs['description'] = "Phase data for interferometer at port 20. Attribute calibration factor assumes 40cm plasma length."
            grp.attrs['unit'] = "rad"
            grp.attrs['Microwave frequency (Hz)'] = 288e9
            grp.attrs['calibration factor (m^-3/rad)'] = get_calibration_factor(grp.attrs['Microwave frequency (Hz)'])

        grp = f.require_group("diagnostics/interferometer/phase_p29")
        if len(grp.attrs) == 0:
            grp.attrs['description'] = "Phase data for interferometer at port 29. Attribute calibration factor assumes 40cm plasma length."
            grp.attrs['unit'] = "rad"
            grp.attrs['Microwave frequency (Hz)'] = 282e9
            grp.attrs['calibration factor (m^-3/rad)'] = get_calibration_factor(grp.attrs['Microwave frequency (Hz)'])

        grp = f.require_group("diagnostics/interferometer/time_array")
        if len(grp.attrs) == 0:
            grp.attrs['description'] = "Time array for interferometer data in milliseconds."
            grp.attrs['unit'] = "ms"

#===============================================================================================================================================

def find_interf_file(datarun_path, interf_path):

    date = datarun_path[-24:-14]

    interf_files = os.listdir(interf_path)
    date_files = [file for file in interf_files if date in file]
    if date_files == []:
        next_date = datetime.datetime.strptime(date, "%Y-%m-%d") + datetime.timedelta(days=1)
        next_date_str = next_date.strftime("%Y-%m-%d")
        ifn = os.path.join(interf_path, f"interferometer_data_{next_date_str}.hdf5")
    else:
        ifn = os.path.join(interf_path, date_files[0])

    return ifn

def merge_interferometer_data(datarun_path, interf_path):

    timestamp_array = get_shot_timestamps(datarun_path)

    # get the groups and sets from the interferometer data
    f_interf = h5py.File(interf_path, "r")
    groups = list(f_interf.keys())
    sets = set(f_interf[groups[0]].keys())

    # Copy data from interferometer hdf5 file into datarun file
    for i, timestamp in enumerate(timestamp_array):
        shot_n = str(i+1) # Naming convention goes with shot number (starts from index 1)
        matching_set = next((x for x in sets if abs(float(x) - timestamp) < 1), None)
        if matching_set is None:
            print(f"Shot {i+1} has no matching interferometer data")
        else:
            # get the data from the closest set
            p20 = f_interf['phase_p20'][matching_set][:]
            p29 = f_interf['phase_p29'][matching_set][:]
            time_array = f_interf['time_array'][matching_set][:]

            with h5py.File(datarun_path, "a") as f_datarun:
                # create datasets in datarun file if they don't already exist
                if shot_n not in f_datarun['diagnostics/interferometer/phase_p20']:
                    f_datarun['diagnostics/interferometer/phase_p20'].create_dataset(shot_n, data=p20)
                if shot_n not in f_datarun['diagnostics/interferometer/phase_p29']:
                    f_datarun['diagnostics/interferometer/phase_p29'].create_dataset(shot_n, data=p29)
                if shot_n not in f_datarun['diagnostics/interferometer/time_array']:
                    f_datarun['diagnostics/interferometer/time_array'].create_dataset(shot_n, data=time_array)

            print(f"Shot {i+1} wrote into datarun file")
            pass

    f_interf.close()
    print('Interferometer data merged into datarun file. File closed.')


#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == '__main__':
      
    datarun_path = r"C:\data\LAPD\11_51x1line_L2sweep_173kHz_820G_LH_2024-06-06_19.59.36.hdf5"
    interf_path = r"C:\data\LAPD\interferometer_samples"

    ifn = find_interf_file(datarun_path, interf_path)
    print("Checking interferometer data at", ifn)

    init_datarun_groups(datarun_path, ifn)

    merge_interferometer_data(datarun_path, ifn)

    write_attribute(datarun_path)