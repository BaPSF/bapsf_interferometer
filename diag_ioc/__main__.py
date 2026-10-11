"""python -m diag_ioc --config FILE.toml [--interactive]: run the diagnostic IOC described by FILE."""
import argparse
import logging
import sys
from pathlib import Path

from diag_ioc.host import load_config, run
from diag_ioc.outage import LOG_FORMAT


def main(argv=None):
	parser = argparse.ArgumentParser(prog="python -m diag_ioc", description=__doc__.split(": ", 1)[1])
	parser.add_argument("--config", type=Path, required=True, help="TOML file: [ioc] and one or more [[module]] tables")
	parser.add_argument("--interactive", action="store_true", help="open the softioc shell (dbl, dbpr, ...) instead of waiting for SIGTERM")
	args = parser.parse_args(argv)
	try:
		config = load_config(args.config)
	except (OSError, ValueError) as e:  # tomllib.TOMLDecodeError is a ValueError
		parser.error(f"{args.config}: {e}")
	base = args.config.resolve().parent  # an absolute path in the config replaces it
	db_path = base / (config.ioc.db_dir or ".") / f"{args.config.stem}.db"
	acf_path = base / config.ioc.acf if config.ioc.acf else None
	if acf_path is not None and not acf_path.is_file():
		parser.error(f"{args.config}: [ioc] acf: no file {acf_path}")  # stop here, not in asInit with whatever access it leaves
	logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)  # under systemd the journal keeps it
	run(config, db_path, acf_path, args.interactive)


if __name__ == "__main__":
	main()
