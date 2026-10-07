import numpy as np

from .raw_output_io import IO

try:
    import adios2
except ImportError:
    adios2 = None


ADIOS2_AVAILABLE = adios2 is not None


class AdiosIO(IO):
    def __init__(self, settings, connection_info=None):
        if not ADIOS2_AVAILABLE:
            raise RuntimeError("adios2 is not available")

        connection_info = connection_info or {}
        output = connection_info.get("output", settings.destination)
        engine = connection_info.get("engine", settings.engine)

        self._adios = adios2.Adios()
        self._io = self._adios.declare_io("InterferometerRawOutput")
        self._io.set_engine(engine)
        self._variables = {}
        self._dtypes = {}
        self._io.define_attribute(
            "description", "Raw BaPSF interferometer scope acquisitions"
        )
        mode = "a" if getattr(settings, "append_output", False) else "w"
        self._stream = adios2.Stream(self._io, output, mode)

    def _variable(self, name, value):
        array = np.asarray(value)
        variable = self._variables.get(name)
        if variable is None:
            if array.ndim == 0:
                variable = self._io.define_variable(name, array)
            else:
                shape = list(array.shape)
                variable = self._io.define_variable(
                    name, array, shape, [0] * array.ndim, shape, False
                )
            self._variables[name] = variable
            self._dtypes[name] = array.dtype
        elif array.dtype != self._dtypes[name]:
            raise TypeError(
                f"Variable {name} changed dtype from {self._dtypes[name]} to {array.dtype}"
            )
        elif array.ndim:
            shape = list(array.shape)
            variable.set_shape(shape)
            variable.set_selection([[0] * array.ndim, shape])
        return variable, array

    def write_data(self, data):
        self._stream.begin_step()
        for name, value in data.items():
            variable, array = self._variable(name, value)
            self._stream.write(variable, array)
        self._stream.end_step()

    def close(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None


def iter_steps(path):
    """Yield each step of a raw-output BP file as {name: array}, as that step was written.

    A step holds only its own variables (a channel or scope that was not read is absent) at its
    own shapes (missing_json and record lengths change between steps). Prefer this to
    adios2.FileReader.read(name, step_selection=[i, 1]): ADIOS2 counts step_selection per
    variable, so a variable absent from earlier steps returns a later step's data, and a read
    without an explicit count takes the variable's first shape.
    """
    if not ADIOS2_AVAILABLE:
        raise RuntimeError("adios2 is not available")
    with adios2.Stream(str(path), "r") as stream:
        for _ in stream.steps():
            yield {name: stream.read(name) for name in stream.available_variables()}


def read_step(path, index):
    """Step `index` (from 0) as {name: array}, like iter_steps; earlier steps are skipped unread."""
    if not ADIOS2_AVAILABLE:
        raise RuntimeError("adios2 is not available")
    if index < 0:
        raise IndexError(f"step {index}: index must be >= 0")
    with adios2.Stream(str(path), "r") as stream:
        for current, _ in enumerate(stream.steps()):
            if current == index:
                return {name: stream.read(name) for name in stream.available_variables()}
    raise IndexError(f"{path} has no step {index}")
