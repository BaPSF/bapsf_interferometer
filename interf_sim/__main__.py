"""Run interf_main.main() on recorded .trc shots; no scope is contacted. Run from the repo root."""
import argparse
import contextlib
from pathlib import Path

import interf_payload
from diag_ioc.link import IocLink, address_arg
from interf_sim.trc_replay import MACHINE_PERIOD_S, TRC_DIR, ReplayLeCroy, loop_offset, repeat_trc_shots, run_main, trc_shots
from streamer import RawOutput
from streamer.arguments import add_raw_output_arguments, settings_from_args

LOG_DIR = Path(__file__).resolve().parent / "log"  # kept apart from the production log directory
RAW_OUTPUT_TIMING_LOG = LOG_DIR / "raw_output.jsonl"


def main():
	parser = argparse.ArgumentParser(prog="python -m interf_sim", description=__doc__)
	parser.add_argument("--trc-dir", type=Path, default=TRC_DIR, help=f"default: {TRC_DIR}")
	parser.add_argument("--period", type=float, default=MACHINE_PERIOD_S,
		help=f"s between machine triggers; 0 = as fast as files load (default: {MACHINE_PERIOD_S:g})")
	parser.add_argument("--start-shot", type=int, help="file counter to start at (default: the earliest trigger)")
	parser.add_argument("--limit", type=int, help="number of shots to serve (default: all); with --loop, per pass")
	parser.add_argument("--repeat-traces", action="store_true",
		help="cycle the available trace files for exactly --limit shots with consecutive counters; their trigger times "
		     "repeat each cycle (use --loop for long runs)")
	parser.add_argument("--loop", action="store_true",
		help="serve the shots again and again until Ctrl-C or SIGTERM, each pass as new shots (see --loop-offset); "
		     "not with --repeat-traces or --raw-output")
	parser.add_argument("--loop-offset", type=float, metavar="S",
		help="with --loop: s added to every trigger time per pass (pass k: k*S); more than the shots' trigger-time span. "
		     "Default: the span plus the mean shot spacing. Past midnight (Los Angeles) the shot date advances and "
		     "numbering restarts at 0, as in acquisition")
	parser.add_argument("--ioc-link", metavar="ADDRESS", type=address_arg,
		help="also send each shot to a diag_ioc listener at unix:/path or tcp:host:port (as INTERF_IOC_LINK does live)")
	add_raw_output_arguments(parser, RAW_OUTPUT_TIMING_LOG)
	args = parser.parse_args()
	if args.repeat_traces and (args.limit is None or args.limit < 1):
		parser.error("--repeat-traces requires --limit to be a positive integer")
	if args.loop and args.repeat_traces:
		parser.error("--loop replaces --repeat-traces; use one")
	if args.loop and args.raw_output is not None:
		parser.error("--loop never writes raw output (a stress run must not fill the raw output); drop --raw-output")
	if args.loop_offset is not None and not args.loop:
		parser.error("--loop-offset needs --loop")

	shots = trc_shots(args.trc_dir)
	if args.start_shot is not None:
		counters = [counter for counter, _ in shots]
		if args.start_shot not in counters:
			parser.error(f"no shot {args.start_shot} in {args.trc_dir}")
		shots = shots[counters.index(args.start_shot):]
	if args.repeat_traces:
		shots = repeat_trc_shots(shots, args.limit)
	else:
		shots = shots[:args.limit]
	offset = None
	if args.loop:
		if not shots:
			parser.error("--loop: no shots selected")
		try:
			offset = loop_offset(shots, args.loop_offset)
		except ValueError as e:
			parser.error(str(e))
		print(f"interf_sim: looping {len(shots)} shots, trigger times {offset:g} s later each pass", flush=True)
	raw_output_settings = settings_from_args(args)
	raw_output = link = None
	with contextlib.ExitStack() as stack:
		if raw_output_settings is not None:
			Path(raw_output_settings.timing_log).parent.mkdir(parents=True, exist_ok=True)
			raw_output = stack.enter_context(RawOutput(raw_output_settings))
		if args.ioc_link is not None:
			link = stack.enter_context(IocLink(args.ioc_link, encode=interf_payload.encode))
		run_main(ReplayLeCroy(shots, args.period, offset), LOG_DIR, outputs=[o for o in (raw_output, link) if o is not None])
		if raw_output is not None and raw_output.dropped_shots:
			print(f"Dropped raw output shots: {raw_output.dropped_shots}")
	if link is not None:  # after close(), which delivers what was still queued
		print(f"IOC link: sent {link.sent}, dropped {link.dropped}, failed {link.failed}")


if __name__ == "__main__":
	main()
