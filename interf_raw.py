# -*- coding: utf-8 -*-
"""
Process Tony's 300 GHz interferometer signals using cross-spectral density

@author: Patrick, Steve, and Jia

------------Pat's code comment----------------
Expects the the reference leg and plasma leg rf (IF actually) signals to be digitized sufficiently fast to
   determine their instantaneous phase

Uses mlab.csd to generate the phase information

In the unstacked figures the phase should vary from 0 to 2pi.

Created on Thurs Sep 6 2018;  fixups added a year later
	Fixups originally involved manually selecting 2pi phase transitions using mouse clicks on the figure
	Now fixups are done automatically using function auto_find_fixups(), which seems to work very well
Last modified by Pat Sep 13, 2020
Jia modified syntax and optimized speed using Github copilot on May. 23. 2024
--------------------------------------------

------------Steve's code comment----------------
Uses the Hilbert transform to compute the phase of the signal
Hilbert transform using built-in function by scipy.signal
Slower computation time than Pat's code, same result compared using plotting

2021-07-15 Update by Jia
- use scipy.fft instead of mlab.csd; improved computation speed
- TODO: why is the result negative?
	added a negative sign to the phase for the moment
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
from scipy.ndimage import uniform_filter1d
from matplotlib import mlab

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
	carrier_frequency = 760e3
	Npass = 2.0 # Number of passes of uwave through plasma
	# diameter = 0.35 # Plasma diameter if it were flat (m)
	# Note: a decent guess for the diameter is the FWHM

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

    # Define a window function (To match same functionality as mlab.csd)
	window = np.hanning(FT_len) # i.e. hanning window

	# loop over each subset of FT_len points  (note: num_FTs = int(#samples / FT_len))
	for m in range(num_FTs):
		i = m * FT_len
		if i+FT_len >= NS:
			break
		ttt[m] = i*dt

        # Apply window function (To match same functionality as mlab.csd)
		plach_segment = plach[i:i + FT_len] * window
		refch_segment = refch[i:i + FT_len] * window

        # Compute the cross-spectral density using scipy.fft
		plach_fft = scipy.fft.fft(plach_segment)
		refch_fft = scipy.fft.fft(refch_segment)
		csd = plach_fft * np.conj(refch_fft)
		csd /= (np.sum(window**2) * FT_len)  # Normalize by the sum of the window squared and FT_len

		# Old command using mlab.csd
#		csd, _ = mlab.csd(plach[i:i+FT_len], refch[i:i+FT_len], NFFT=FT_len, Fs=1./dt, sides='default', scale_by_freq=False)

		# Find the peak of the cross-spectral density
		npts_to_ignore = 10                 # skip 10 initial points to avoid DC offset being the largest value
		adx = np.argmax(np.abs(csd[npts_to_ignore:]))
		adx += npts_to_ignore

		csd_angle = np.angle(csd)[adx]

		if csd_angle < 0:
			csd_angle += 2*math.pi

		csd_ang[m] = csd_angle
		csd_mag[m] = np.abs(csd[adx])
	return ttt+tarr[0], -csd_ang, csd_mag

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

def phase_from_steve(tarr, refch, plach):
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
	t = signal.decimate(tarr, decimate_factor, ftype=ftype, zero_phase=True)
	dt = t[1]-t[0]
	t_ms = t * 1e3

	# Construct analytic function versions of the reference and the plasma signal
	# Note: scipy's hilbert function actually creates an analytic function using the Hilbert transform, which is what we want in the end anyway
	# So, given real X(t): analytic function = X(t) + i * HX(t), where H is the actual Hilbert transform
	# https://en.wikipedia.org/wiki/Hilbert_transform

	aref = signal.hilbert(r) # The analytic reference signal
	asig = signal.hilbert(s) # The analytic data signal

	# Subtract the mean values (you want to keep this code)
	aref -= np.mean(aref)
	asig -= np.mean(asig)

	# Compute the phase angles and unwrap specified phase jumps
	pref = np.unwrap(np.angle(aref))
	psig = np.unwrap(np.angle(asig), discont=1.0e-8*np.pi)

	# Compute the phase difference (delta phi)
	dphi = (pref-psig)

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

	ifn = r"C:\data\LAPD\interferometer_samples\C1-interf-shot57673.trc"
	refch, tarr, vertical_gain, vertical_offset = read_trc_data_simplified(ifn)

	ifn = r"C:\data\LAPD\interferometer_samples\C2-interf-shot57673.trc"
	plach, tarr, vertical_gain, vertical_offset = read_trc_data_simplified(ifn)

	plt.figure()

	t_ms, ne = phase_from_raw(tarr, refch, plach)
	plt.plot(t_ms, ne, label='cross correlation')

	t_ms, ne = phase_from_steve(tarr, refch, plach)
	plt.plot(t_ms, ne, label='hilbert transform')

	plt.legend()
	plt.show()
