import pickle
from pathlib import Path

from .raw_output_io import IO
from .output_permissions import output_directory, share_output


class PickleIO(IO):
    """Write each data step as the next object in a pickle stream."""

    def __init__(self, settings, connection_info=None):
        connection_info = connection_info or {}
        output = connection_info.get("output", settings.destination)
        mode = "ab" if getattr(settings, "append_output", False) else "wb"
        output_directory(Path(output).parent)
        self._stream = open(output, mode)
        share_output(output)

    def write_data(self, data):
        pickle.dump(data, self._stream, protocol=pickle.HIGHEST_PROTOCOL)
        self._stream.flush()

    def close(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None
