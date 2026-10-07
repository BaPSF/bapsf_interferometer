"""Synthetic heterodyne shots with a known phase, as RawShots or as LeCroy .trc files for interf_sim.

The phase is exact, so analysis can be checked against it. Not an instrument model: ideal
sinusoids of constant amplitude, Gaussian noise only, a Rigol record spanning the LeCroy window, and
scale factors that are plausible rather than the lab scopes'.

	python -m interf_sim.synthetic OUTDIR --shots 20
"""
import argparse
import struct
import sys
import time
from pathlib import Path

import numpy as np
from lab_scopes.lecroy import LeCroyWavedesc
from lab_scopes.lecroy.wavedesc import WAVEDESC_FMT, WAVEDESC_SIZE

from interf_analysis import CSD_SKIP_BINS, FT_len, PORTS
from interf_raw import RawShot

SAMPLES = 1 << 20
DT = 1e-8  # s
IF_HZ = 5e6  # bin ~26 of an FT_len window at DT, clear of the CSD_SKIP_BINS that phase_from_raw skips
PERIOD_S = 3.0
AMPLITUDE_V = 0.4
PEAK_PHASE_RAD = 6.0  # near 2π, so the wrapped CSD angle crosses its branch cut and must be unwrapped
LECROY_GAIN = 2.0 ** -15  # V/code, ±1 V over int16; exact in the WAVEDESC's float32
RIGOL_Y = {"y_increment": 1 / 2048, "y_origin": 0.0, "y_reference": 2048.0}  # 12-bit codes 0..4095 for ±1 V


def make_wavedesc(n, dt, t0=0.0, gain=LECROY_GAIN, offset=0.0, trigger_time=None):
	"""346-byte WAVEDESC for n int16 samples; trigger_time in Unix s, None leaves it unset (tt_year 0).

	horiz_interval, vertical_gain and vertical_offset are float32 fields, so dt reads back rounded.
	"""
	desc = LeCroyWavedesc()
	desc.generate_test_data(n)
	fields = dict(wave_descriptor=WAVEDESC_SIZE, comm_order=1, record_type=0, processing_done=0,
	              sweeps_per_acq=1, horiz_interval=dt, horiz_offset=t0, vertical_gain=gain, vertical_offset=offset)
	if trigger_time is not None:
		# gmtime because wavedesc_trigger_timestamp() reads the fields back with calendar.timegm.
		tm = time.gmtime(trigger_time)
		fields.update(tt_second=tm.tm_sec + trigger_time % 1, tt_minute=tm.tm_min, tt_hours=tm.tm_hour,
		              tt_days=tm.tm_mday, tt_months=tm.tm_mon, tt_year=tm.tm_year)
	return struct.pack(WAVEDESC_FMT, *desc.wd._replace(**fields))


def gaussian_phase(center_s, sigma_s, peak_rad=PEAK_PHASE_RAD):
	"""φ(t) callable of a Gaussian density bump."""
	return lambda t: peak_rad * np.exp(-0.5 * ((np.asarray(t) - center_s) / sigma_s) ** 2)


def default_phase(t):
	"""gaussian_phase centred on the record `t`, sigma 1/8 of its span: ~0 over the first windows,
	which phase_from_raw averages as its pre-plasma offset."""
	return gaussian_phase((t[0] + t[-1]) / 2, (t[-1] - t[0]) / 8)


def heterodyne(t, f_if=IF_HZ, phase=None, noise_v=0.0, rng=None):
	"""(ref, plasma) volts: A·cos(2πft) and A·cos(2πft − φ(t)), A = AMPLITUDE_V, each plus Gaussian noise of rms noise_v.

	phase: callable φ(t) or None for default_phase(t). phase_from_raw recovers +φ.
	"""
	phi = (phase or default_phase(t))(t)
	carrier = 2 * np.pi * f_if * t
	ref = AMPLITUDE_V * np.cos(carrier)
	plasma = AMPLITUDE_V * np.cos(carrier - phi)
	if noise_v:
		rng = rng or np.random.default_rng()
		ref += rng.normal(0.0, noise_v, ref.shape)
		plasma += rng.normal(0.0, noise_v, plasma.shape)
	return ref, plasma


def _lecroy_codes(volts):
	return np.clip(np.rint(volts / LECROY_GAIN), -32768, 32767).astype(np.int16)


def _rigol_codes(volts):
	codes = volts / RIGOL_Y["y_increment"] + RIGOL_Y["y_origin"] + RIGOL_Y["y_reference"]
	return np.clip(np.rint(codes), 0, 4095).astype(np.uint16)


def make_raw_shot(n=SAMPLES, dt=DT, f_if=IF_HZ, phase=None, noise_v=0.0, host_time=None, rigol=True, rng=None,
                  flat=()):
	"""interf_raw.RawShot with every channel of interf_analysis.PORTS, all ports carrying φ.

	phase: callable φ(t_s), or None for default_phase over the LeCroy record, which starts at t = 0.
	The Rigol record (n // 4 points) spans the same window. Without `rigol`, missing["rigol"] is set
	and rigol is {}, as interf_raw reports a failed Rigol. The WAVEDESC trigger time is host_time.
	`flat` names LeCroy channels written as constant code 0, a dead input (ValueError for others).
	"""
	host_time = time.time() if host_time is None else host_time
	rng = rng or np.random.default_rng()
	wavedesc = make_wavedesc(n, dt, trigger_time=host_time)
	t = LeCroyWavedesc(wavedesc).time_array  # the float32-rounded axis lecroy_trace will read back
	phase = phase or default_phase(t)
	rigol_n = n // 4
	metadata = dict(RIGOL_Y, x_increment=n * dt / rigol_n, x_origin=0.0, x_reference=0.0, points=rigol_n)
	t_rigol = np.arange(rigol_n) * metadata["x_increment"]
	lecroy, rigol_data, missing = {}, {}, {}
	for port in PORTS:
		if port.scope == "lecroy":
			ref, pla = heterodyne(t, f_if, phase, noise_v=noise_v, rng=rng)
			lecroy[port.ref_ch] = (_lecroy_codes(ref), wavedesc)
			lecroy[port.plasma_ch] = (_lecroy_codes(pla), wavedesc)
		elif rigol:
			ref, pla = heterodyne(t_rigol, f_if, phase, noise_v=noise_v, rng=rng)
			rigol_data[port.ref_ch] = (_rigol_codes(ref), metadata)
			rigol_data[port.plasma_ch] = (_rigol_codes(pla), dict(metadata))
	for ch in flat:
		if ch not in lecroy:
			raise ValueError(f"flat channel {ch!r} is not one of the LeCroy channels {sorted(lecroy)}")
		lecroy[ch] = (np.zeros(n, dtype=np.int16), wavedesc)
	if not rigol:
		missing["rigol"] = "synthetic shot without Rigol"
	return RawShot(host_time, lecroy, rigol_data, missing, 0.0)


def synthetic_shots(count, period=PERIOD_S, start_time=None, **shot_kw):
	"""Generator of `count` make_raw_shot()s whose host_time, and so trigger time, step by `period` s."""
	start = time.time() if start_time is None else start_time
	for i in range(count):
		yield make_raw_shot(host_time=start + i * period, **shot_kw)


def write_trc_shots(outdir, shots, first_counter=0):
	"""Write each shot's LeCroy channels as C<n>-interf-shot<counter>.trc; return the paths written.

	trc_shots() orders by WAVEDESC trigger time, not counter, so give shots increasing host_time
	(synthetic_shots does).
	"""
	outdir = Path(outdir)
	paths = []
	for counter, shot in enumerate(shots, first_counter):
		for ch, (samples, wavedesc) in shot.lecroy.items():
			payload = wavedesc + np.asarray(samples, dtype="<i2").tobytes()
			path = outdir / f"{ch}-interf-shot{counter}.trc"
			path.write_bytes(b"#9" + f"{len(payload):09d}".encode("ascii") + payload)
			paths.append(path)
	return paths


def main(argv=None):
	parser = argparse.ArgumentParser(prog="python -m interf_sim.synthetic", description=__doc__.splitlines()[0])
	parser.add_argument("outdir", type=Path)
	parser.add_argument("--shots", type=int, default=20)
	parser.add_argument("--samples", type=int, default=SAMPLES, help=f"per channel (default: {SAMPLES})")
	parser.add_argument("--dt", type=float, default=DT, help=f"s per sample (default: {DT:g})")
	parser.add_argument("--if-hz", type=float, default=IF_HZ, help=f"default: {IF_HZ:g}")
	parser.add_argument("--period", type=float, default=PERIOD_S, help=f"s between recorded trigger times (default: {PERIOD_S:g})")
	parser.add_argument("--noise", type=float, default=0.01, help="V rms per channel (default: 0.01)")
	parser.add_argument("--first-counter", type=int, default=0)
	parser.add_argument("--seed", type=int, help="noise seed (default: random)")
	parser.add_argument("--flat", action="append", default=[], metavar="CH",
		choices=[ch for p in PORTS if p.scope == "lecroy" for ch in (p.ref_ch, p.plasma_ch)],
		help="write this LeCroy channel as a constant (dead input), so its port is reported missing; repeatable")
	args = parser.parse_args(argv)
	bin_ = args.if_hz * args.dt * FT_len
	if not CSD_SKIP_BINS < bin_ < FT_len / 2:
		parser.error(f"IF lands in bin {bin_:.1f} of a {FT_len}-point window; phase_from_raw needs {CSD_SKIP_BINS} < bin < {FT_len // 2}")

	args.outdir.mkdir(parents=True, exist_ok=True)
	shots = synthetic_shots(args.shots, args.period, n=args.samples, dt=args.dt, f_if=args.if_hz, noise_v=args.noise,
	                        rigol=False, rng=np.random.default_rng(args.seed), flat=args.flat)  # .trc replay never has a Rigol
	write_trc_shots(args.outdir, _progress(shots, args.shots), args.first_counter)


def _progress(shots, total):
	for i, shot in enumerate(shots, 1):
		yield shot  # resumes once write_trc_shots has written this shot
		print(f"synthetic: {i}/{total} written", file=sys.stderr, flush=True)


if __name__ == "__main__":
	main()
