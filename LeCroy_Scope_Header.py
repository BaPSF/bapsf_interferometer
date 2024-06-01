# -*- coding: utf-8 -*-
"""
Lecroy Scope Header access class
PP, derived from LeCroy_Scope.py

todo: refactor LeCroy_Scope to use this module

Feb.2024 update:
-- Use f-strings for string formatting instead of concatenation for better readability.
-- change numpy use to np
"""
import numpy as np
import collections
import struct
import sys

# the header recorded for each trace
# 63 entries, 346 bytes
WAVEDESC = collections.namedtuple('WAVEDESC',
['descriptor_name', 'template_name', 'comm_type', 'comm_order',
 'wave_descriptor', 'user_text', 'res_desc1', 'trigtime_array', 'ris_time_array',
 'res_array1', 'wave_array_1', 'wave_array_2', 'res_array2', 'res_array3',
 'instrument_name', 'instrument_number', 'trace_label', 'reserved1', 'reserved2',
 'wave_array_count', 'pnts_per_screen', 'first_valid_pnt', 'last_valid_pnt',
 'first_point', 'sparsing_factor', 'segment_index', 'subarray_count', 'sweeps_per_acq',
 'points_per_pair', 'pair_offset', 'vertical_gain', 'vertical_offset', 'max_value',
 'min_value', 'nominal_bits', 'nom_subarray_count', 'horiz_interval', 'horiz_offset',
 'pixel_offset', 'vertunit', 'horunit', 'horiz_uncertainty',
 'tt_second', 'tt_minute', 'tt_hours', 'tt_days', 'tt_months', 'tt_year', 'tt_unused',
 'acq_duration', 'record_type', 'processing_done', 'reserved5', 'ris_sweeps',
 'timebase', 'vert_coupling', 'probe_att', 'fixed_vert_gain', 'bandwidth_limit',
 'vertical_vernier', 'acq_vert_offset', 'wave_source'])

WAVEDESC_SIZE = 346
"""
 The header should be 346 bytes (with correct packing); it is preceded by 15 bytes for the def9header etc.
 note: for simplicity I expanded the leCroy_time struct into explicit fields above, labeled tt_xxx

 To get floating values from the stored raw data: y[i] = vertical_gain * data[i] - vertical_offset

 some entries:
   horiz_offset: Seconds between trigger and first data point (note this is <= 0 if trigger is visible on screen)
   bandwidth_limit: 0 = off, 1 = on
   record_type:  see below
   processing_done: see below
   timebase: see below
   fixed_vert_gain: see below
   vert_coupling: see below
   wave_source:  0=CH1, 1=CH2, 2=CH3, 3=CH4, 9=Unknown
"""
WAVEDESC_FMT = '=16s16shhllllllllll16sl16shhlllllllllhhffffhhfdd48s48sfdBBBBhhfhhhhhhfhhffh'    # for struct...
#    The initial '=' character specifies native byte order, with standard (C) alignment

RECORD_TYPES = ['single_sweep', 'interleaved', 'histogram', 'graph', 'filter_coefficient',
                'complex', 'extrema', 'sequence_obsolete', 'centered_RIS', 'peak_detect']

PROCESSING_TYPES = ['no_processing', 'fir_filter', 'interpolated', 'sparsed',
                    'autoscaled', 'no_result', 'rolling', 'cumulative']

TIMEBASE_IDS = ['1 ps', '2 ps', '5 ps', '10 ps', '20 ps', '50 ps', '100 ps', '200 ps', '500 ps',
                '1 ns', '2 ns', '5 ns', '10 ns', '20 ns', '50 ns', '100 ns', '200 ns', '500 ns',
                '1 us', '2 us', '5 us', '10 us', '20 us', '50 us', '100 us', '200 us', '500 us',
                '1 ms', '2 ms', '5 ms', '10 ms', '20 ms', '50 ms', '100 ms', '200 ms', '500 ms',
                '1 s',  '2 s',  '5 s',  '10 s',  '20 s',  '50 s',  '100 s',  '200 s',  '500 s',
                '1 ks', '2 ks', '5 ks']   # these are per division; ALSO: 100 corresponds to EXTERNAL

VERT_GAIN_IDS = ['1 uV', '2 uV', '5 uV', '10 uV', '20 uV', '50 uV', '100 uV', '200 uV', '500 uV',
                 '1 mV', '2 mV', '5 mV', '10 mV', '20 mV', '50 mV', '100 mV', '200 mV', '500 mV',
                 '1 V',  '2 V',  '5 V',  '10 V',  '20 V',  '50 V',  '100 V',  '200 V',  '500 V',
                 '1 kV', '2 kV', '5 kV', '10 kV']   # these are per division; pp added the last 3

VERT_COUPLINGS = ['DC 50 Ohms', 'ground', 'DC 1 MOhm', 'ground', 'AC 1 MOhm']

EXPANDED_TRACE_NAMES = {'F1': 'Math1'   , 'F2': 'Math2'   , 'F3': 'Math3'   , 'F4': 'Math4'   ,  # documentation indicates these are possible, but some of them result in errors
                        'F5': 'Math5'   , 'F6': 'Math6'   , 'F7': 'Math7'   , 'F8': 'Math8'   ,
					  'TA': 'ChannelA', 'TB': 'ChannelB', 'TC': 'ChannelC', 'TD': 'ChannelD',
					  'M1': 'Memory1' , 'M2': 'Memory2' , 'M3': 'Memory3' , 'M4': 'Memory4' ,
					  'C1': 'Channel1', 'C2': 'Channel2', 'C3': 'Channel3', 'C4': 'Channel4' }
KNOWN_TRACE_NAMES = sorted(list(EXPANDED_TRACE_NAMES.keys()))


#================================================================================================
class LeCroy_Scope_Header:
	""" LeCroy X-Stream scope header interpretation """
	def __init__(self, hdr_bytes=b'\0'*WAVEDESC_SIZE):
		self.hdr = WAVEDESC._make(struct.unpack(WAVEDESC_FMT, hdr_bytes))

	def __str__(self):
		""" return a string representation: TODO """
		return str(self.hdr)

	def __repr__(self):
		""" return a printable version: TODO """
		return str(self)

	@property
	def descriptor_name(self): return self.hdr.descriptor_name

	@property
	def sweeps_per_acq(self): return self.hdr.sweeps_per_acq

	@property
	def num_samples(self):
		if self.hdr.comm_type == 0:
			# data returned as signed chars
			return self.hdr.wave_array_1
		elif self.hdr.comm_type == 1:
			# data returned as shorts
			return int(self.hdr.wave_array_1/2)
		else:
			# throw an exception if we don't recognize comm_type
			err = '**** hdr.comm_type = ' + str(self.hdr.comm_type) + '; expected value is either 0 or 1'
			raise(RuntimeError(err)).with_traceback(sys.exc_info()[2])

	@property
	def record_type(self):       return RECORD_TYPES[self.hdr.record_type]

	@property
	def timebase(self):          return f"{TIMEBASE_IDS[self.hdr.timebase]} per div"

	@property
	def vertical_gain(self):     return f"{VERT_GAIN_IDS[self.hdr.fixed_vert_gain]} per div"

	@property
	def vertical_coupling(self): return VERT_COUPLINGS[self.hdr.vert_coupling]

	@property
	def processing_type(self):   return PROCESSING_TYPES[self.hdr.processing_done]

	@property
	def num_sweeps(self):        return self.hdr.sweeps_per_acq

	@property
	def nominal_bits(self):      return self.hdr.nominal_bits

	@property
	def vertical_units(self):    return str(self.hdr.vertunit).split('\\x00')[0][2:]    # for whatever reason this prepends "b'" to string

	@property
	def horizontal_units(self):  return str(self.hdr.horunit).split('\\x00')[0][2:]     # so ignore first 2 chars  TODO: fix this

	@property
	def dt(self):                return self.hdr.horiz_interval

	@property
	def t0(self):                return self.hdr.horiz_offset

	@property
	def vertical_offset(self):  return self.hdr.vertical_offset

	@property
	def data_scaling(self):      return f"gain = {self.hdr.vertical_gain}, offset = {self.hdr.vertical_offset} {self.vertical_units}"

	@property
	def sample_timing(self):     return f"dt = {self.hdr.horiz_interval}, offset = {self.hdr.horiz_offset} {self.horizontal_units}"

	@property
	def time_array(self) -> np.array:
		""" return a numpy array containing num_samples times
		"""
		NSamples = self.num_samples
		t0 = self.hdr.horiz_offset
		return np.linspace(t0, t0 + NSamples*self.hdr.horiz_interval, NSamples, endpoint=False)
		#note on linspace construction here: suppose we have 2 samples and the trace is 10ms, the samples should be at 0 and 5 ms,
		#                                    rather than 0 and 10ms as linspace(0,N*dt,N) would return

	def dump(self):
		s = ""
		for n,v in zip(WAVEDESC._fields, list(self.hdr)):
			s += str(type(v)).ljust(20) + str(n.ljust(20)) + str(v) + '\n'
		return s

	def generate_test_data(self, NTimes=1000, verbose=False):
		self.hdr = self.hdr._replace(descriptor_name = b"WAVEDESC\0\0\0\0\0\0\0\0",
		                             comm_type       = 1,    # data returned as shorts
		                             wave_array_1    = 2*NTimes,
		                             record_type     = 3,
		                             timebase        = 3,
		                             fixed_vert_gain = 3,
		                             vert_coupling   = 3,
		                             processing_done = 1,
		                             sweeps_per_acq  = 10,
		                             nominal_bits    = 12,
		                             vertunit        = ('\0'*48).encode('utf8'),   # must be 48 bytes
		                             horunit         = ('\0'*48).encode('utf8'),   # must be 48 bytes
		                             horiz_interval  = 0.001,
		                             horiz_offset    = 0.002,
		                             vertical_gain   = 0.1,
		                             vertical_offset = 0.2)
		if verbose:
			print("new header:")
			print(self.dump())

		hdr_bytes = struct.pack(WAVEDESC_FMT, *list(self.hdr))
		return hdr_bytes



if __name__ == '__main__':
	""" """

	"generate a byte array containing the header - either construction is ok:"
	if False:
		hdr_bytes = numpy.zeros((WAVEDESC_SIZE,), dtype='B')
		lsh = LeCroy_Scope_Header(hdr_bytes)
	else:
		lsh = LeCroy_Scope_Header()

	nhdr_bytes = lsh.generate_test_data()

	print("dt                 ", lsh.dt)
	print("num_samples        ", lsh.num_samples)
	print("sample timing      ", lsh.sample_timing)
	print("nominal_bits       ", lsh.nominal_bits)
	print("time array =", lsh.time_array)

	#lsh2 = LeCroy_Scope_Header(nhdr_bytes)
	#print(lsh2.dump())

	print("\n---- done ----")
