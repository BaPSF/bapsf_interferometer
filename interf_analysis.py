# -*- coding: utf-8 -*-
"""Phase extraction for the BaPSF ~300 GHz heterodyne interferometers (one reference leg, one plasma leg).

Both phase functions take (tarr [s, uniformly sampled], refch, plach) and return (t_ms, phase_rad),
phase_rad = unwrapped phase(ref) - phase(plasma). The plasma leg (n < 1) accumulates less phase,
so the phase is positive during a shot: the sign get_calibration_factor assumes (n_e = phase * cal).
phase_from_raw is the default; phase_from_hilbert is slower, has edge effects (TODO), and is kept
for cross-checking.

`python interf_analysis.py` runs both on a hard-coded .trc shot and overlays them (smoke test).

analyze_shot() is the live path, raw scope codes of one shot -> per-port phase and density; the same
call reproduces published results offline from the ADIOS archive.

History: Patrick (2018-09) original CSD method, manual 2π fix-ups later automated
(now np.unwrap), last edit 2020-09-13. Jia (2021-07-15) mlab.csd -> scipy.fft.
Steve (2024-05-20) phase_from_hilbert. Jia (2024-05-23) CSD vectorization. Jia (2026-05-04)
Hilbert cleanup and speed-up.
"""
import math
import time
from dataclasses import dataclass

import scipy
import numpy as np
from scipy import constants as const
from scipy import signal

from lab_scopes.lecroy import LeCroyWavedesc

#============================================================================
FT_len = 512  # phase_from_raw window: larger -> finer frequency resolution, coarser time resolution
CSD_SKIP_BINS = 10  # lowest FFT bins excluded from the CSD peak search, so DC is never the peak
OFFSET_WINDOWS = 5  # phase_from_raw subtracts the mean of this many leading windows (taken as pre-plasma)
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
	csd_abs = np.abs(csd)
	adx = np.argmax(csd_abs[:, CSD_SKIP_BINS:], axis=1) + CSD_SKIP_BINS
	row_index = np.arange(valid_segments)
	csd_angle = np.angle(csd[row_index, adx])
	csd_angle = np.where(csd_angle < 0, csd_angle + 2*math.pi, csd_angle)

	csd_ang[:valid_segments] = csd_angle
	csd_mag[:valid_segments] = csd_abs[row_index, adx]
	return ttt[:valid_segments]+tarr[0], -csd_ang[:valid_segments], csd_mag[:valid_segments]

def phase_from_raw(tarr, refch, plach, ft_len=FT_len):
	'''
	CSD phase at the peak bin of each ft_len-point Hanning window, unwrapped, minus the mean of
	the first OFFSET_WINDOWS windows (taken as pre-plasma). One point per window, at the window start.
	'''
	offset_range = range(OFFSET_WINDOWS)

	ttt, csd_ang, csd_mag = correlation_spectrogram(tarr, refch, plach, ft_len)

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


#============================================================================
# Live analysis: one shot's raw scope codes -> per-port phase and density
#============================================================================

def lecroy_trace(samples, wavedesc):
	'''(t_s, volts) of one LeCroy channel from its raw int16 codes and 346-byte WAVEDESC.'''
	wd = LeCroyWavedesc(wavedesc)
	volts = wd.wd.vertical_gain * np.asarray(samples, dtype=np.float64) - wd.wd.vertical_offset
	t_s = wd.time_array
	n = min(len(t_s), len(volts))  # guards a WAVEDESC whose sample count disagrees with the samples read
	return t_s[:n], volts[:n]


def rigol_trace(samples, metadata):
	'''(t_s, volts) of one Rigol channel from its uint16 12-bit codes and RigolDHO800.read_channel() metadata.'''
	codes = np.asarray(samples, dtype=np.float64)
	volts = (codes - metadata["y_origin"] - metadata["y_reference"]) * metadata["y_increment"]
	t_s = metadata["x_origin"] + (np.arange(codes.size) - metadata["x_reference"]) * metadata["x_increment"]
	return t_s, volts


_TRACES = {"lecroy": lecroy_trace, "rigol": rigol_trace}


@dataclass(frozen=True)
class Port:
	name: str
	scope: str  # "lecroy" | "rigol": the shot attribute holding its channels
	ref_ch: str
	plasma_ch: str
	freq_hz: float


# Also the frequencies of interf_file's phase groups. P40's channels repeat the defaults of
# interf_raw.RIGOL_REF_CH / RIGOL_PLA_CH, which are env-configurable there; a caller with other
# channels passes its own `ports`.
PORTS = (
	Port("P20", "lecroy", "C1", "C2", 288e9),
	Port("P29", "lecroy", "C3", "C4", 282e9),
	Port("P40", "rigol", "C1", "C2", 288e9),
)


@dataclass
class PortResult:
	name: str
	t_ms: np.ndarray  # window starts on the scope's time axis
	phase: np.ndarray  # rad
	ne: np.ndarray  # m^-3, path-averaged (see get_calibration_factor): phase * cal
	# Mean of the undecimated ne over analyze_shot's ne_window_ms; nan when the port is missing, no window
	# is set, or the trace does not span the whole window (a partial span would bias the mean).
	ne_mean: float
	cal: float  # m^-3/rad
	freq_hz: float
	decimation: int  # stride applied to t_ms, phase and ne
	missing: str | None  # reason the port has no data; the arrays are then empty


_IDENTITY = ("shot_date", "shot_number", "shot_time", "time_source")


@dataclass
class ShotResult:
	# Shot identity (docs/ARCHITECTURE.md D14, D15), copied from the shot; None where it has none (a RawShot
	# before interf_main assigns it, or a streamer archive step).
	shot_date: int | None
	shot_number: int | None
	shot_time: float | None
	time_source: str | None
	host_time: float
	critical_path_s: float
	acq_missing: dict[str, str]  # shot.missing as acquired
	ports: dict[str, PortResult]
	analysis_s: float


def analyze_shot(shot, ports=PORTS, plasma_length=0.4, ft_len=FT_len, max_points=None, ne_window_ms=None):
	'''ShotResult of an interf_raw.RawShot, interf_payload.DecodedShot or interf_archive.ArchiveShot.

	Never raises on bad data: a port whose channels are absent, flat or too short, or whose analysis
	fails, is returned with `missing` set, and the other ports are unaffected. Above `max_points`
	per port, arrays are kept every k-th point, k = ceil(n / max_points). ne_window_ms = (start, stop)
	on every port's time axis (trigger-relative on both scopes) sets PortResult.ne_mean; ValueError
	if start >= stop.
	'''
	if ne_window_ms is not None and not ne_window_ms[0] < ne_window_ms[1]:
		raise ValueError(f"ne_window_ms {ne_window_ms}: start must be before stop")
	t0 = time.perf_counter()
	results = {p.name: _analyze_port(shot, p, plasma_length, ft_len, max_points, ne_window_ms) for p in ports}
	return ShotResult(**{name: getattr(shot, name, None) for name in _IDENTITY}, host_time=shot.host_time,
	                  critical_path_s=shot.critical_path_s, acq_missing=dict(shot.missing), ports=results,
	                  analysis_s=time.perf_counter() - t0)


def _analyze_port(shot, port, plasma_length, ft_len, max_points, ne_window_ms):
	cal = get_calibration_factor(port.freq_hz, plasma_length)
	channels = getattr(shot, port.scope)
	absent = [ch for ch in (port.ref_ch, port.plasma_ch) if ch not in channels]
	if absent:
		reason = shot.missing.get(port.scope)
		if reason is None:
			# Data on the scope but not on these channels: a port/channel mapping mismatch, not an outage.
			reason = f"{'/'.join(absent)} not acquired" + (f"; {port.scope} has {', '.join(channels)}" if channels else "")
		return _missing_port(port, cal, reason)
	try:
		trace = _TRACES[port.scope]
		t_s, ref = trace(*channels[port.ref_ch])
		_, pla = trace(*channels[port.plasma_ch])
		n = min(len(ref), len(pla))
		unusable = _unusable(port, ref, pla, n, ft_len)
		if unusable is None:
			# Mean removed per channel as main's (bench-validated) read_lecroy/read_rigol did before phase_from_raw.
			ref, pla = ref - ref.mean(), pla - pla.mean()
			t_ms, phase = phase_from_raw(t_s[:n], ref[:n], pla[:n], ft_len)
	except Exception as e:
		return _missing_port(port, cal, f"analysis error: {type(e).__name__}: {e}")
	if unusable is not None:
		return _missing_port(port, cal, unusable)
	ne = phase * cal
	ne_mean = _window_mean(t_ms, ne, ne_window_ms)
	k = 1
	if max_points is not None and ne.size > max_points:
		k = math.ceil(ne.size / max_points)
		t_ms, phase, ne = t_ms[::k], phase[::k], ne[::k]
	return PortResult(port.name, t_ms, phase, ne, ne_mean, cal, port.freq_hz, k, None)


def _unusable(port, ref, pla, n, ft_len):
	'''Why a decoded channel pair cannot be analyzed, or None. Exact checks only.

	Not caught: a disconnected input that still delivers noise reads as a valid (meaningless) phase.
	That needs a signal-to-noise check, deferred until it can be calibrated on real scope data.
	'''
	# correlation_spectrogram drops the last full window when n is a multiple of ft_len, so the
	# OFFSET_WINDOWS windows phase_from_raw averages need n strictly above their length.
	if n <= OFFSET_WINDOWS * ft_len:
		return f"trace too short: {n} samples, need > {OFFSET_WINDOWS * ft_len}"
	# Identical codes throughout: a dead channel would otherwise give phase 0, published as a valid density of 0.
	flat = [ch for ch, volts in ((port.ref_ch, ref), (port.plasma_ch, pla)) if np.ptp(volts) == 0]
	if flat:
		return f"{'/'.join(flat)} flat (no signal)"
	return None


def _window_mean(t_ms, ne, window_ms):
	if window_ms is None or t_ms.size == 0 or t_ms[0] > window_ms[0] or t_ms[-1] < window_ms[1]:
		return math.nan
	inside = (t_ms >= window_ms[0]) & (t_ms <= window_ms[1])
	return float(ne[inside].mean()) if inside.any() else math.nan  # none inside: window narrower than the point spacing


def _missing_port(port, cal, reason):
	return PortResult(port.name, np.empty(0), np.empty(0), np.empty(0), math.nan, cal, port.freq_hz, 1, reason)


#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == '__main__':
	import matplotlib.pyplot as plt
	from lab_scopes.io.lecroy_files import read_trc_data_simplified

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
