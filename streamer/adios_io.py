import math

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
        stream_name = output
        if engine.lower() == "wanstreamer":
            private_key = getattr(settings, "private_key", None)
            if not private_key:
                raise ValueError("WANStreamer output requires a private key")
            parameters = {
                "ConnectionFile": output,
                "PrivateKey": private_key,
                "LaunchMode": getattr(settings, "launch_mode", "none"),
                "ReplayBufferSteps": str(
                    max(
                        1,
                        math.ceil(
                            getattr(settings, "buffer_seconds", 600.0)
                            / getattr(settings, "output_interval_seconds", 3.0)
                        ),
                    )
                ),
                "RetryIntervalSeconds": str(
                    getattr(settings, "retry_interval_seconds", 1.0)
                ),
                "ReconnectTimeoutSeconds": str(
                    getattr(settings, "reconnect_timeout_seconds", 0.0)
                ),
            }
            launch_command = getattr(settings, "launch_command", None)
            if launch_command:
                parameters["LaunchCommand"] = launch_command
            self._io.set_parameters(parameters)
            stream_name = "InterferometerRawOutput"
        self._variables = {}
        self._dtypes = {}
        self._io.define_attribute(
            "description", "Raw BaPSF interferometer scope acquisitions"
        )
        mode = "a" if getattr(settings, "append_output", False) else "w"
        self._stream = adios2.Stream(self._io, stream_name, mode)

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

    @property
    def dropped_steps(self):
        # Native WANStreamer uses backpressure instead of dropping queued steps.
        return 0

    def close(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None
