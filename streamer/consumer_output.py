import math
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from .adios_io import ADIOS2_AVAILABLE, AdiosIO
from .raw_output_io import IO
from .pickle_io import PickleIO


DEFAULT_FILE_INTERVAL_SECONDS = 3600.0


def positive_float(value):
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError("must be greater than zero")
    return converted


class RotatingConsumerOutput(IO):
    """Write timestamp-named output files, rotating at a fixed interval."""

    def __init__(
        self,
        output_directory,
        engine,
        interval_seconds=DEFAULT_FILE_INTERVAL_SECONDS,
        clock=time.monotonic,
        timestamp_factory=datetime.now,
    ):
        self._output_directory = Path(output_directory)
        self._engine = engine
        self._interval_seconds = positive_float(interval_seconds)
        self._clock = clock
        self._timestamp_factory = timestamp_factory
        self._output = None
        self._next_rotation = None

        self._output_directory.mkdir(parents=True, exist_ok=True)
        if not self._output_directory.is_dir():
            raise NotADirectoryError(
                f"Consumer output is not a directory: {self._output_directory}"
            )

    def _suffix(self):
        if not ADIOS2_AVAILABLE:
            return ".pkl"
        if self._engine.upper() in ("HDF5", "H5"):
            return ".h5"
        return ".bp"

    def _new_path(self):
        timestamp = self._timestamp_factory().astimezone().strftime(
            "%Y%m%dT%H%M%S.%f%z"
        )
        suffix = self._suffix()
        candidate = self._output_directory / f"{timestamp}{suffix}"
        sequence = 1
        while candidate.exists():
            candidate = self._output_directory / f"{timestamp}-{sequence}{suffix}"
            sequence += 1
        return candidate

    def _rotate(self, opened_at):
        if self._output is not None:
            self._output.close()
            self._output = None

        destination = self._new_path()
        settings = SimpleNamespace(
            destination=str(destination),
            engine=self._engine,
            append_output=False,
        )
        if ADIOS2_AVAILABLE:
            self._output = AdiosIO(settings)
        else:
            self._output = PickleIO(settings)
            print(
                "adios2 is not available; "
                f"writing pickle output to {destination}",
                flush=True,
            )
        self._next_rotation = opened_at + self._interval_seconds
        print(f"Opened consumer output {destination}", flush=True)

    def write_data(self, data):
        now = self._clock()
        if self._output is None or now >= self._next_rotation:
            self._rotate(now)
        self._output.write_data(data)

    def close(self):
        if self._output is not None:
            self._output.close()
            self._output = None


def create_consumer_output(
    output_directory,
    engine,
    interval_seconds=DEFAULT_FILE_INTERVAL_SECONDS,
):
    return RotatingConsumerOutput(
        output_directory,
        engine,
        interval_seconds=interval_seconds,
    )
