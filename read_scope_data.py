# -*- coding: utf-8 -*-
# created by Jia Han, Sep-16-2019
# Read Lecroy scope data files in .trc (binary) or .txt (ascii)

'''
read_trc_data(file path) => signal, time array
	read binary file with open()
	first 11 bytes includes data_size information
	header information is encoded in the next 346 bytes which is decoded by LeCroy_Scope_Header.py
	data bytes are decoded using struct.unpack(). Each data point uses 2 bytes. Real voltage value is converted using header info

Note: when saving on scope, need to choose binary with word format

--------------------------------------------------------------------

read_txt_data(file path) => signal, time array
	use open() to look at first 5 rows and print out when and which scope data was taken from
	use numpy.loadtxt() to read the data and time array

Note: when saving on scope, need to choose ascii and ',' as deliminator

--------------------------------------------------------------------------
--------------------------------------------------------------------------
Feb.2024 update:
1. Variable Naming: Used snake_case for variable names, following PEP8 conventions.
2. Directly converted the unpacked data to a NumPy array and performed data manipulation in NumPy for efficiency.
3. Ensured consistent use of string formatting.

'''

import numpy as np
import struct

from LeCroy_Scope_Header import LeCroy_Scope_Header

#======================================================================================

def decode_header_info(hdr_bytes):
    try:
        header = LeCroy_Scope_Header(hdr_bytes)
    except Exception as e:
        print("Error decoding LeCroy_Scope_Header info:", e)
        header = None
    return header

#======================================================================================

def read_trc_data(file_path, list_some_header_info=False):

	with open(file_path, mode='rb') as file: # rb -> read binary
		file_content = file.read()
	
	first_11 = file_content[:11].decode()

	if not first_11.startswith('#9'):
		raise SyntaxError('First two bytes are not #9')

	hdr_bytes = file_content[11:11+346]
	header = decode_header_info(hdr_bytes)

	data_size = int( (int(first_11[2:]) - 346) / 2)
	if data_size != len(header.time_array):
		print('Time array length from header %i does not equal %i from first 11 bytes' %(len(header.time_array), data_size))
	data_size = len(header.time_array)

	if list_some_header_info:
		print("dt =", header.dt)
		print("t0 =", header.t0)
		print("vertical_gain =", header.vertical_gain)
		print("timebase =", header.timebase)
		print("Input = ", header.vertical_coupling)

	data_bytes = file_content[11+346:]

	print('Reading data...')
	fmt = f"={data_size}h"
	data = np.frombuffer(data_bytes, dtype=fmt)
	data = data[0,:] * header.hdr.vertical_gain - header.hdr.vertical_offset

	print('Done')

	return data, header.time_array # signal, time array

#======================================================================================
def read_trc_data_simplified(file_path):

	with open(file_path, mode='rb') as file: # rb -> read binary
		file_content = file.read()

	hdr_bytes = file_content[11:11+346]
	header = decode_header_info(hdr_bytes)
	data_size = len(header.time_array)
	
	data_bytes = file_content[11+346:]
	fmt = f"={data_size}h"
	data = np.frombuffer(data_bytes, dtype=fmt)
	data = data[0,:] * header.hdr.vertical_gain - header.hdr.vertical_offset


	return data, header.time_array, header.hdr.vertical_gain, header.hdr.vertical_offset
	
def read_trc_data_no_header(file_path, data_size, vertical_gain, vertical_offset):
	
	with open(file_path, mode='rb') as file: # rb -> read binary
		file_content = file.read()
	
	data_bytes = file_content[11+346:]
	fmt = f"={data_size}h"
	data = np.frombuffer(data_bytes, dtype=fmt)
	data = data[0,:] * vertical_gain - vertical_offset
	
	return data

#======================================================================================

def read_txt_data(ifn):

	with open(ifn, "r") as file:
		file_content = file.read()
		
		if 'Segment' not in file_content[:50]:
			print('First 5 rows might include data. Check on text reader before using this function to read.')

		print(file_content[:15], ' trace saved on', file_content[100:119])


	data = np.loadtxt(ifn,dtype=float, delimiter=',', skiprows=5)

	print('Done')

	return data[:,1], data[:,0] - data[0,0] # signal, time array
