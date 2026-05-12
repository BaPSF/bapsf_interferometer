# -*- coding: utf-8 -*-
"""
Phase extraction for the BaPSF ~300 GHz microwave interferometer.

Overview
--------
Recovers the line-integrated plasma phase shift from the digitized IF signals
of a heterodyne interferometer (one reference leg, one plasma leg). The phase
can then be converted to line-integrated electron density via the calibration
factor returned by `get_calibration_factor()`.

Public API
----------
- `get_calibration_factor(f_uwave, plasma_length)` -> float
      Calibration constant `cal` such that  n_e = phase * cal  [m^-3 / rad].
      Assumes a retro-reflecting (double-pass) geometry.

- `phase_from_raw(tarr, refch, plach)` -> (t_ms, phase_rad)
      **Default method, used in production.** Cross-spectral-density (CSD) over
      `FT_len`-point Hanning windows; tracks the peak frequency bin per window
      and returns its unwrapped phase. Fast and robust.

- `phase_from_hilbert(tarr, refch, plach)` -> (t_ms, phase_rad)
      Alternative method using the analytic signal from `scipy.signal.hilbert`.
      Gives the same answer as `phase_from_raw` but is slower and currently
      shows minor edge effects; kept for cross-checking.

Inputs (both phase functions)
-----------------------------
- `tarr`  : 1-D time array in seconds, uniformly sampled.
- `refch` : reference-leg IF samples (same length as `tarr`).
- `plach` : plasma-leg IF samples (same length as `tarr`).

Sampling must be fast enough to resolve the IF carrier.

Outputs
-------
- `t_ms`     : time axis in milliseconds (downsampled relative to input —
               one point per FFT window for `phase_from_raw`, one per
               decimated sample for `phase_from_hilbert`).
- `phase_rad`: cumulative (unwrapped) plasma phase shift in radians, with the
               first few samples used to subtract the pre-plasma offset so the
               trace starts near zero.

Sign convention
---------------
The cross-spectral density `CSD = FFT(plasma) * conj(FFT(ref))` has angle
`phase(plasma) - phase(ref)`. Because the plasma refractive index `n < 1`, the
plasma leg accumulates *less* phase than the reference, so the raw CSD angle is
negative when plasma is present. `correlation_spectrogram()` negates it so that
the returned phase is positive during a shot, matching the convention assumed
by `get_calibration_factor()`.

Tunable parameter
-----------------
- `FT_len` (module-level, default 512): FFT window length for `phase_from_raw`.
  Larger -> better frequency resolution, coarser time resolution.

Quick example
-------------
    from read_scope_data import read_trc_data_simplified
    from interf_raw import phase_from_raw, get_calibration_factor

    refch, tarr, *_ = read_trc_data_simplified("C1-interf-shot00001.trc")
    plach, _,   *_ = read_trc_data_simplified("C2-interf-shot00001.trc")

    t_ms, phase = phase_from_raw(tarr, refch, plach)
    n_e = phase * get_calibration_factor(f_uwave=288e9, plasma_length=0.4)

Running this module directly (`python interf_raw.py`) loads a hard-coded sample
shot, runs both methods, prints their wall-clock times, and overlays the
results — useful as a smoke test.

Authors and history
-------------------
- Patrick (2018-09): original CSD method; manual 2π fix-ups, later replaced by
  automatic `auto_find_fixups()`. Last edit by Pat: 2020-09-13.
- Jia (2021-07-15): switched from `matplotlib.mlab.csd` to `scipy.fft`; added
  notes on the sign convention.
- Steve (2024-05-20): contributed the Hilbert-transform variant `phase_from_hilbert`.
- Jia (2024-05-23): syntax cleanup and vectorization of the CSD path.
- Jia (2026-05-04): Hilbert path cleanup and speed-up (edge effects still present — TODO).
"""
import sys
sys.path.append(r"C:\Users\hjia9\Documents\GitHub\data-analysis")
sys.path.append(r"C:\Users\hjia9\Documents\GitHub\data-analysis\read")

import math
import scipy
import numpy as np
import matplotlib.pyplot as plt
from scipy import constants as const
from scipy import signal

from read_scope_data import read_trc_data, read_trc_data_simplified
import time

#============================================================================
# Parameters for analyzing raw signal of interferometer
FT_len = 512
#============================================================================

def get_calibration_factor(f_uwave = 288e9, plasma_length = 0.4):
	'''
	Convert phase to density with:
	f_uwave --> Microwave frequency (Hz)
	plasma_length --> Plasma length (m)
	Note: SI units for physical constants
	'''
	e = const.elementary_charge
	m_e = const.electron_mass
	eps0 = const.epsilon_0
	c = const.speed_of_light
	Npass = 2.0 # Number of passes of uwave through plasma (retroreflecting geometry)
	# diameter = 0.35 # Plasma diameter if it were flat (m)
	# Note: a decent guess for the diameter is the FWHM

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

#	print("computing %i FTs"%(num_FTs), flush=True)

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

	# Old command using mlab.csd
#	csd, _ = mlab.csd(plach[i:i+FT_len], refch[i:i+FT_len], NFFT=FT_len, Fs=1./dt, sides='default', scale_by_freq=False)

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

def auto_find_fixups(t_ms, csd_ang, threshold=5.):
	d = np.diff(csd_ang)
	p = t_ms[:-1][d > threshold] # len(diff) is one less than len(csd_ang)
	n = t_ms[:-1][d < -threshold]
	f = np.ones((p.size+n.size, 2))
	f[:p.size, 0] = p
	f[:p.size, 1] = -1
	f[p.size:, 0] = n
	return f

def do_fixups(t_ms, csd_ang):
	cum_phase = csd_ang.copy()
	fixups = auto_find_fixups(t_ms-t_ms[0], cum_phase)
	dt = t_ms[1]-t_ms[0]
	for t,s in fixups:
		n = int(t/dt)
		# every time there is a 2pi jump, add or subtract 2pi to the entire rest of the time series
		cum_phase[n+1:] += s*2*np.pi
	return cum_phase

def phase_from_raw(tarr, refch, plach):
	'''
	1. Divide data into segments of length FT_len (512 points)
	2. For each segment:
		- Apply Hanning window
		- Compute FFT of both reference and plasma signals
		- Calculate Cross-Spectral Density (CSD): CSD = FFT(plasma) * conj(FFT(ref))
		- Find peak in CSD spectrum (skipping first 10 points to avoid DC)
		- Extract phase angle at peak frequency
	3. Unwrap phase to handle 2π jumps
	4. Subtract initial offset
	'''
	offset_range = range(5)
	
	ttt, csd_ang, csd_mag = correlation_spectrogram(tarr, refch, plach, FT_len)

	t_ms = ttt * 1000


	cum_phase = np.unwrap(csd_ang)
#	cum_phase = do_fixups(t_ms, csd_ang)
	offset = np.average(cum_phase[offset_range])

	return t_ms, cum_phase-offset


#============================================================================
# The following function is from Steve
#============================================================================

def phase_from_hilbert(tarr, refch, plach):
	'''
	1. Decimate data by factor of 10 (reduce sampling rate)
	2. Create analytic signals using Hilbert transform analytic signal = original + i*Hilbert(original)
	3. Subtract mean values
	4. Calculate phase angles of both signals
	5. Unwrap phases
	6. Take difference between reference and plasma phases
	'''
	# Decimate data as we are only interested in the slowly varying phase,
	# not the carrier wave phase variations
	decimate_factor = 10
	dt = tarr[1]-tarr[0]
	# carrier_period_nt = int((1./carrier_frequency)/dt)
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

	# Flatten out minor but inelegant edge effects due to the Hilbert transforms,
	# and mostly the filter
	#mindex = int(2048 / np.sqrt(decimate_factor))
	#dphi[0:mindex-1] = dphi[mindex+1:2*mindex+1].mean()
	#dphi[-(mindex+1):] = dphi[-2*mindex:-(mindex+1)].mean()

	# Subtract the mean of the first 100 samples as it is not meaningful to us
	#dphi -= dphi[mindex:4*mindex].mean()

	# filter out carrier frequency
	#dphi = uniform_filter1d(dphi, carrier_period_nt)

	return t_ms, dphi


#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================
# sudo mount.cifs //192.168.7.61/interf /home/smbshare -o username=LECROYUSER_2

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
