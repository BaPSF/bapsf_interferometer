import math
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from .adios_io import ADIOS2_AVAILABLE, AdiosIO
from .raw_output_io import IO, unused_path
from .pickle_io import PickleIO
from .output_permissions import output_directory as make_output_directory


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
        timestamp_factory=lambda: datetime.now().astimezone(),
    ):
        self._output_directory = Path(output_directory)
        self._engine = engine
        self._interval_seconds = positive_float(interval_seconds)
        self._clock = clock
        self._timestamp_factory = timestamp_factory
        self._output = None
        self.current_path = None
        self._next_rotation = None
        self._output_day = None

        make_output_directory(self._output_directory)
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

    def _new_path(self, wall_time):
        day_directory = self._output_directory / wall_time.strftime("%Y%m%d")
        make_output_directory(day_directory)
        timestamp = wall_time.strftime("%H%M%S.%f%z")
        return unused_path(day_directory / f"{timestamp}{self._suffix()}")

    def _rotate(self, opened_at, wall_time):
        if self._output is not None:
            self._output.close()
            self._output = None

        destination = self._new_path(wall_time)
        self.current_path = destination
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
        self._output_day = wall_time.date()
        print(f"Opened consumer output {destination}", flush=True)

    def write_data(self, data):
        now = self._clock()
        wall_time = self._timestamp_factory()
        if (self._output is None or now >= self._next_rotation
                or wall_time.date() != self._output_day):
            self._rotate(now, wall_time)
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
