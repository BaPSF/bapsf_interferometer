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


def _steps(stream, path):
    """Begin each step written so far in turn; yields the step index while inside the step.

    Not adios2.Stream.steps(): on a file whose writer is still running, or died without closing
    it, that waits forever for the next step, holding the GIL so every thread of the process
    stops. begin_step(timeout=0) returns NotReady there instead, and the reading ends.
    """
    index = 0
    while True:
        status = stream.begin_step(timeout=0.0)
        if status == adios2.bindings.StepStatus.OtherError:
            raise RuntimeError(f"{path}: ADIOS2 error at step {index}")
        if status != adios2.bindings.StepStatus.OK:  # EndOfStream, or NotReady: nothing newer written
            return
        yield index
        stream.end_step()
        index += 1


def _step_variables(stream):
    """{name: array} of the current step, each array read at this step's own shape.

    The explicit count matters with BP4, which keeps a variable's selection from an earlier
    step: a plain read would truncate an array that grew, and fail on one that shrank.
    """
    variables = {}
    for name in stream.available_variables():
        shape = stream.inquire_variable(name).shape()
        variables[name] = stream.read(name, start=[0] * len(shape), count=shape) if shape else stream.read(name)
    return variables


def iter_steps(path):
    """Yield each step of a raw-output BP file as {name: array}, as that step was written.

    A step holds only its own variables (a channel or scope that was not read is absent) at its
    own shapes (missing_json and record lengths change between steps). Prefer this to
    adios2.FileReader.read(name, step_selection=[i, 1]): ADIOS2 counts step_selection per
    variable, so a variable absent from earlier steps returns a later step's data, and a read
    without an explicit count takes the variable's first shape.

    Only the steps written so far are read: on a file still being written, or whose writer died
    without closing it, iteration ends at the last complete step instead of waiting.
    """
    if not ADIOS2_AVAILABLE:
        raise RuntimeError("adios2 is not available")
    with adios2.Stream(str(path), "r") as stream:
        for _ in _steps(stream, path):
            yield _step_variables(stream)


def read_step(path, index):
    """Step `index` (from 0) as {name: array}, like iter_steps; earlier steps are skipped unread."""
    if not ADIOS2_AVAILABLE:
        raise RuntimeError("adios2 is not available")
    if index < 0:
        raise IndexError(f"step {index}: index must be >= 0")
    with adios2.Stream(str(path), "r") as stream:
        for current in _steps(stream, path):
            if current == index:
                return _step_variables(stream)
    raise IndexError(f"{path} has no step {index}")
