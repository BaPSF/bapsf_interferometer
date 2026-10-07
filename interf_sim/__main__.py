"""Run interf_main.main() on recorded .trc shots; no scope is contacted. Run from the repo root."""
import argparse
from pathlib import Path

from interf_sim.trc_replay import TRC_DIR, ReplayLeCroy, repeat_trc_shots, run_main, trc_shots
from streamer import RawOutput
from streamer.arguments import add_raw_output_arguments, settings_from_args

LOG_DIR = Path(__file__).resolve().parent / "log"  # kept apart from the production log directory
RAW_OUTPUT_TIMING_LOG = LOG_DIR / "raw_output.jsonl"


def main():
	parser = argparse.ArgumentParser(prog="python -m interf_sim", description=__doc__)
	parser.add_argument("--trc-dir", type=Path, default=TRC_DIR, help=f"default: {TRC_DIR}")
	parser.add_argument("--period", type=float, default=3.0, help="s between machine triggers; 0 = as fast as files load (default: 3)")
	parser.add_argument("--start-shot", type=int, help="file counter to start at (default: the earliest trigger)")
	parser.add_argument("--limit", type=int, help="number of shots to serve (default: all)")
	parser.add_argument("--repeat-traces", action="store_true",
		help="cycle the available trace files for exactly --limit shots with consecutive counters")
	add_raw_output_arguments(parser, RAW_OUTPUT_TIMING_LOG)
	args = parser.parse_args()
	if args.repeat_traces and (args.limit is None or args.limit < 1):
		parser.error("--repeat-traces requires --limit to be a positive integer")

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
	raw_output_settings = settings_from_args(args)
	if raw_output_settings is None:
		run_main(ReplayLeCroy(shots, args.period), LOG_DIR)
		return

	Path(raw_output_settings.timing_log).parent.mkdir(parents=True, exist_ok=True)
	with RawOutput(raw_output_settings) as raw_output:
		run_main(ReplayLeCroy(shots, args.period), LOG_DIR, outputs=[raw_output])
		if raw_output.dropped_shots:
			print(f"Dropped raw output shots: {raw_output.dropped_shots}")


if __name__ == "__main__":
	main()
