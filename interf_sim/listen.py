"""Print one line per shot received on an IocLink address; --analyze adds per-port results. Development tool.

	python -m interf_sim.listen unix:/tmp/interf.sock --analyze
	python -m interf_sim --trc-dir T --ioc-link unix:/tmp/interf.sock

Analysis runs on the link's reader thread, so a slow analysis makes the sender drop shots (latest-wins).
"""
import argparse
import logging
import sys
import time

import numpy as np

from diag_ioc.link import LinkListener, address_arg
from interf_analysis import analyze_shot
from streamer.network_access import ipv4_network
from streamer.payload import decode_json, shot_from_variables


def _shot_line(variables, analyze, ne_window_ms):
	mb = sum(np.asarray(v).nbytes for v in variables.values()) / 1e6
	parts = [f"seq {int(variables['shot_index'])}",
	         time.strftime("host %H:%M:%S", time.localtime(float(variables["host_time"]))),
	         f"{len(variables)} variables {mb:.1f} MB",
	         f"path {float(variables['critical_path_s']):.2f} s"]
	if not analyze:
		missing = decode_json(variables["missing_json"])
		if missing:
			parts.append("missing " + "; ".join(f"{k}: {v}" for k, v in missing.items()))
		return " | ".join(parts)
	result = analyze_shot(shot_from_variables(variables), ne_window_ms=ne_window_ms)
	for port in result.ports.values():
		if port.missing is not None:
			parts.append(f"{port.name} missing: {port.missing}")
		else:
			# max is a sanity print only; the published scalar is ne_mean over the window.
			parts.append(f"{port.name} {port.t_ms.size} pts ne max {port.ne.max():.3g} mean {port.ne_mean:.3g} m^-3")
	parts.append(f"analysis {result.analysis_s * 1e3:.0f} ms")
	return " | ".join(parts)


def main(argv=None):
	parser = argparse.ArgumentParser(prog="python -m interf_sim.listen", description=__doc__.splitlines()[0])
	parser.add_argument("address", type=address_arg, help="unix:/path or tcp:host:port")
	parser.add_argument("--analyze", action="store_true", help="run interf_analysis.analyze_shot on each shot")
	parser.add_argument("--ne-window-ms", type=float, nargs=2, metavar=("START", "STOP"),
		help="window for ne_mean in ms (default: none, mean prints nan)")
	parser.add_argument("--allow", action="append", default=[], type=ipv4_network, metavar="CIDR",
		help="tcp peers to accept; repeatable (default: 127.0.0.1/32)")
	args = parser.parse_args(argv)
	if args.ne_window_ms is not None and not args.ne_window_ms[0] < args.ne_window_ms[1]:
		parser.error("--ne-window-ms: START must be before STOP")
	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)

	def on_message(variables):
		print(_shot_line(variables, args.analyze, args.ne_window_ms), flush=True)

	with LinkListener(args.address, on_message, args.allow).start() as listener:
		print(f"listening on {listener.address}; Ctrl-C to stop", flush=True)
		try:
			while True:
				time.sleep(1.0)
		except KeyboardInterrupt:
			pass


if __name__ == "__main__":
	main()
