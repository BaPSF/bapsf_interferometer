# -*- coding: utf-8 -*-
"""Phase extraction for the BaPSF ~300 GHz heterodyne interferometers (one reference leg, one plasma leg).

Both phase functions take (tarr [s, uniformly sampled], refch, plach) and return (t_ms, phase_rad),
phase_rad = unwrapped phase(ref) - phase(plasma). The plasma leg (n < 1) accumulates less phase,
so the phase is positive during a shot: the sign get_calibration_factor assumes (n_e = phase * cal).
phase_from_raw is the default; phase_from_hilbert is slower, has edge effects (TODO), and is kept
for cross-checking.

`python interf_analysis.py` runs both on a hard-coded .trc shot and overlays them (smoke test).

History: Patrick (2018-09) original CSD method, manual 2π fix-ups later automated
(now np.unwrap), last edit 2020-09-13. Jia (2021-07-15) mlab.csd -> scipy.fft.
Steve (2024-05-20) phase_from_hilbert. Jia (2024-05-23) CSD vectorization. Jia (2026-05-04)
Hilbert cleanup and speed-up.
"""
import math
import scipy
import numpy as np
import matplotlib.pyplot as plt
from scipy import constants as const
from scipy import signal

from lab_scopes.io.lecroy_files import read_trc_data_simplified
import time

#============================================================================
FT_len = 512  # phase_from_raw window: larger -> finer frequency resolution, coarser time resolution
#============================================================================

def get_calibration_factor(f_uwave = 288e9, plasma_length = 0.4):
	'''
	cal [m^-3/rad] with n_e = phase * cal; f_uwave in Hz, plasma_length in m.

	plasma_length is the diameter of an equivalent flat density profile (the FWHM is a decent
	guess), so n_e is a path average, not a peak density.
	'''
	e = const.elementary_charge
	m_e = const.electron_mass
	eps0 = const.epsilon_0
	c = const.speed_of_light
	Npass = 2.0 # Number of passes of uwave through plasma (retroreflecting geometry)

	# n_e = phase * cal;  cal = 4π·f·ε₀·m_e·c / (N_pass × e² × L)
	calibration = 1./((Npass/4./np.pi/f_uwave)*(e**2/m_e/c/eps0)*plasma_length)
	return calibration

#============================================================================
# The following functions are from Pat
#============================================================================
def parinterp(x1, x2, x3, y1, y2, y3):
	'''
	Parabolic interpolation of the peak of a function
	'''
	d = - (x1-x2) * (x2-x3) * (x3-x1)
	if d == 0:
		raise ValueError('parinterp:() two abscissae are the same')

	cd = (x1-x2) * (y3-y2) - (x3-x2) * (y1-y2)
	bd = (x3-x2)**2 * (y1-y2) - (x1-x2)**2 * (y3-y2)

	if abs(cd) <= abs(1.e-34*bd) or abs(d*cd) <= abs(1.e-34*bd**2):
		return x2, y2

	x = x2 - .5*bd/cd
	y = y2 - bd**2/(4*d*cd)

	if x < min(x1, min(x3, x2)) or x > max(x1, max(x3, x2)):
		raise UserWarning('parinterp(): max is outside the valid x range')

	return x, y


def fit_peak_index(data):
	'''
	Find the peak of a function
	'''
	i = np.argmax(data)
	if i == 0:
		return 0, data[0]
	elif i == np.size(data)-1:
		return np.size(data)-1, data[-1]

	x, y = parinterp(-1, 0, 1, data[i-1], data[i], data[i+1])

	return i+x, y
#============================================================================

def correlation_spectrogram(tarr, refch, plach, FT_len):
	''' compute a spectrogram-like array of the correlation spectral density
		track the peak as a function of time
		return the phase and magnitude of the peak, along with the times they are computed for
	'''
	NS = len(refch)
	num_FTs = int(NS/FT_len)
	dt = tarr[1] - tarr[0]

	ttt = np.zeros(num_FTs)
	csd_ang = np.zeros(num_FTs)   # computed cross spectral density phase vs time
	csd_mag = np.zeros(num_FTs)   # computed cross spectral density magnitude vs time

	if num_FTs <= 1:
		return ttt+tarr[0], -csd_ang, csd_mag

	# Define a window function (To match same functionality as mlab.csd)
	window = np.hanning(FT_len) # i.e. hanning window
	window_power = np.sum(window**2) * FT_len

	# Keep the legacy behavior of skipping the final full window while vectorizing
	# the FFT work for all earlier windows.
	valid_segments = num_FTs if NS % FT_len != 0 else num_FTs - 1
	usable_points = valid_segments * FT_len
	ttt[:valid_segments] = np.arange(valid_segments) * FT_len * dt

	plach_segments = plach[:usable_points].reshape(valid_segments, FT_len) * window
	refch_segments = refch[:usable_points].reshape(valid_segments, FT_len) * window

	# Compute the cross-spectral density using batched FFTs.
	plach_fft = scipy.fft.fft(plach_segments, axis=1)
	refch_fft = scipy.fft.fft(refch_segments, axis=1)
	csd = plach_fft * np.conj(refch_fft)
	csd /= window_power  # Normalize by the sum of the window squared and FT_len

	# Find the peak of the cross-spectral density
	npts_to_ignore = 10                 # skip 10 initial points to avoid DC offset being the largest value
	csd_abs = np.abs(csd)
	adx = np.argmax(csd_abs[:, npts_to_ignore:], axis=1) + npts_to_ignore
	row_index = np.arange(valid_segments)
	csd_angle = np.angle(csd[row_index, adx])
	csd_angle = np.where(csd_angle < 0, csd_angle + 2*math.pi, csd_angle)

	csd_ang[:valid_segments] = csd_angle
	csd_mag[:valid_segments] = csd_abs[row_index, adx]
	return ttt[:valid_segments]+tarr[0], -csd_ang[:valid_segments], csd_mag[:valid_segments]

def phase_from_raw(tarr, refch, plach):
	'''
	CSD phase at the peak bin of each FT_len-point Hanning window, unwrapped, minus the mean of
	the first 5 windows (taken as pre-plasma). One point per window, at the window start.
	'''
	offset_range = range(5)

	ttt, csd_ang, csd_mag = correlation_spectrogram(tarr, refch, plach, FT_len)

	t_ms = ttt * 1000


	cum_phase = np.unwrap(csd_ang)
	offset = np.average(cum_phase[offset_range])

	return t_ms, cum_phase-offset


#============================================================================
# The following function is from Steve
#============================================================================

def phase_from_hilbert(tarr, refch, plach):
	'''
	Phase difference of the analytic signals after 10x IIR decimation. No offset is subtracted,
	so unlike phase_from_raw the trace need not start near zero.
	'''
	# Decimate data as we are only interested in the slowly varying phase,
	# not the carrier wave phase variations
	decimate_factor = 10
	dt = tarr[1]-tarr[0]
	ftype='iir'

	r = signal.decimate(refch, decimate_factor, ftype=ftype, zero_phase=True)
	s = signal.decimate(plach, decimate_factor, ftype=ftype, zero_phase=True)
	t = tarr[0] + np.arange(len(r)) * (dt * decimate_factor)
	t_ms = t * 1e3

	# Construct analytic function versions of the reference and the plasma signal
	# Note: scipy's hilbert function actually creates an analytic function using the Hilbert transform, which is what we want in the end anyway
	# So, given real X(t): analytic function = X(t) + i * HX(t), where H is the actual Hilbert transform
	# https://en.wikipedia.org/wiki/Hilbert_transform

	# Pad to a 5-smooth length so scipy.fft picks a fast transform size, then truncate.
	# Use edge-replication padding (not zero-pad) to avoid Gibbs ringing at the boundary.
	n = len(r)
	N = scipy.fft.next_fast_len(n)
	pad = N - n
	if pad:
		r_pad = np.concatenate([r, np.full(pad, r[-1])])
		s_pad = np.concatenate([s, np.full(pad, s[-1])])
	else:
		r_pad, s_pad = r, s
	aref = signal.hilbert(r_pad)[:n]
	asig = signal.hilbert(s_pad)[:n]

	# Remove DC of the analytic signals before taking phase
	aref -= np.mean(aref)
	asig -= np.mean(asig)

	# Phase difference via single complex product: angle(aref) - angle(asig) = angle(aref * conj(asig))
	dphi = np.unwrap(np.angle(aref * np.conj(asig)))

	# Edge effects at both ends, mostly from the decimation filter, are not corrected (TODO).
	return t_ms, dphi


#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == '__main__':

	ifn = r"E:\interferometer\raw data\C1-interf-shot57507.trc"
	refch, tarr, vertical_gain, vertical_offset = read_trc_data_simplified(ifn)

	ifn = r"E:\interferometer\raw data\C2-interf-shot57507.trc"
	plach, tarr, vertical_gain, vertical_offset = read_trc_data_simplified(ifn)

	plt.figure()
	plt.plot(tarr*1e3, refch, label='reference leg')
	plt.plot(tarr*1e3, plach, label='plasma leg')
	plt.legend()
	plt.xlabel('time (ms)')
	plt.ylabel('voltage (V)')

	plt.figure()
	t0 = time.perf_counter()
	t_ms, ne = phase_from_raw(tarr, refch, plach)
	t_csd = time.perf_counter() - t0
	print(f"phase_from_raw (cross correlation): {t_csd:.4f} s")
	plt.plot(t_ms, ne, label='cross correlation')

	t0 = time.perf_counter()
	t_ms, ne = phase_from_hilbert(tarr, refch, plach)
	t_hilbert = time.perf_counter() - t0
	print(f"phase_from_hilbert (hilbert transform): {t_hilbert:.4f} s")
	plt.plot(t_ms, ne, label='hilbert transform')
	plt.legend()
	plt.show()
