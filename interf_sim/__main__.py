"""Run interf_main.main() unmodified on recorded .trc shots; no scope is contacted. Run from the repo root."""
import argparse
from pathlib import Path

from interf_sim.scopes import TRC_DIR, ReplayLeCroy, run_main, trc_shots

LOG_DIR = Path(__file__).resolve().parent / "log"  # kept apart from the production log directory


def main():
	parser = argparse.ArgumentParser(prog="python -m interf_sim", description=__doc__)
	parser.add_argument("--trc-dir", type=Path, default=TRC_DIR, help=f"default: {TRC_DIR}")
	parser.add_argument("--period", type=float, default=3.0, help="s between machine triggers; 0 = as fast as files load (default: 3)")
	parser.add_argument("--start-shot", type=int, help="file counter to start at (default: the earliest trigger)")
	parser.add_argument("--limit", type=int, help="number of shots to serve (default: all)")
	args = parser.parse_args()

	shots = trc_shots(args.trc_dir)
	if args.start_shot is not None:
		counters = [counter for counter, _ in shots]
		if args.start_shot not in counters:
			parser.error(f"no shot {args.start_shot} in {args.trc_dir}")
		shots = shots[counters.index(args.start_shot):]
	run_main(ReplayLeCroy(shots[:args.limit], args.period), LOG_DIR)


if __name__ == "__main__":
	main()
