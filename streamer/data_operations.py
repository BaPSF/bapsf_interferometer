"""Pluggable array transformations for socket payloads.

An operation owns both sides of a transformation: :meth:`encode` runs before
an array is sent and :meth:`decode` reconstructs the array after it is
received.  Keeping this interface broader than a compressor allows future
implementations to use lossy encodings or scientific-data refactoring.
"""

from abc import ABC, abstractmethod
import configparser
from dataclasses import dataclass
import math
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class EncodedArray:
    """An encoded payload and the metadata needed to decode it.

    ``operation`` is ``None`` when an implementation elects to send the
    original bytes, for example when lossless compression would expand them.
    """

    payload: object
    operation: dict | None


class DataOperation(ABC):
    """Encode and reconstruct one NumPy array payload."""

    @abstractmethod
    def encode(self, array):
        """Return an :class:`EncodedArray` for a contiguous NumPy array."""

    @abstractmethod
    def decode(self, payload, dtype, shape):
        """Reconstruct an array from an encoded bytes-like payload."""


class Blosc2ZstdOperation(DataOperation):
    """Lossless Blosc2 compression using its Zstandard codec."""

    NAME = "blosc2-zstd"
    FORMAT_VERSION = 1
    FILTERS = (
        "none",
        "shuffle",
        "bitshuffle",
        "delta+shuffle",
        "delta+bitshuffle",
    )

    def __init__(self, compression_level=1, filter_name="shuffle", threads=1):
        if not isinstance(compression_level, int) or not 0 <= compression_level <= 9:
            raise ValueError("Blosc2 compression_level must be between 0 and 9")
        if filter_name not in self.FILTERS:
            choices = ", ".join(self.FILTERS)
            raise ValueError(f"Blosc2 filter must be one of: {choices}")
        if not isinstance(threads, int) or threads <= 0:
            raise ValueError("Blosc2 threads must be greater than zero")
        self.compression_level = compression_level
        self.filter_name = filter_name
        self.threads = threads

    @staticmethod
    def _blosc2():
        try:
            import blosc2
        except ImportError as exc:
            raise RuntimeError(
                "Blosc2 compression requires the 'blosc2' Python package on "
                "both the producer and consumer"
            ) from exc
        return blosc2

    def check_available(self):
        """Fail immediately when the selected implementation is unavailable."""
        self._blosc2()

    def _filters(self, blosc2):
        no_filter = blosc2.Filter.NOFILTER
        filters = [no_filter] * 6
        if self.filter_name == "shuffle":
            filters[-1] = blosc2.Filter.SHUFFLE
        elif self.filter_name == "bitshuffle":
            filters[-1] = blosc2.Filter.BITSHUFFLE
        elif self.filter_name == "delta+shuffle":
            filters[0] = blosc2.Filter.DELTA
            filters[-1] = blosc2.Filter.SHUFFLE
        elif self.filter_name == "delta+bitshuffle":
            filters[0] = blosc2.Filter.DELTA
            filters[-1] = blosc2.Filter.BITSHUFFLE
        return filters

    def description(self):
        return {
            "name": self.NAME,
            "format_version": self.FORMAT_VERSION,
            "codec": "zstd",
            "compression_level": self.compression_level,
            "filter": self.filter_name,
            "threads": self.threads,
        }

    def encode(self, array):
        array = np.asarray(array)
        if not array.flags.c_contiguous:
            raise ValueError("Data operations require C-contiguous arrays")
        if array.nbytes == 0 or array.dtype.itemsize > 255:
            return EncodedArray(memoryview(array).cast("B"), None)
        blosc2 = self._blosc2()
        compressed = blosc2.compress2(
            array,
            codec=blosc2.Codec.ZSTD,
            clevel=self.compression_level,
            typesize=array.dtype.itemsize,
            nthreads=self.threads,
            filters=self._filters(blosc2),
        )
        if len(compressed) >= array.nbytes:
            return EncodedArray(memoryview(array).cast("B"), None)
        return EncodedArray(compressed, self.description())

    def decode(self, payload, dtype, shape):
        blosc2 = self._blosc2()
        expected_size = math.prod(shape) * dtype.itemsize
        raw = bytearray(expected_size)
        blosc2.decompress2(payload, dst=raw, nthreads=self.threads)
        return np.frombuffer(raw, dtype=dtype).reshape(shape).copy()

    @classmethod
    def from_description(cls, description):
        if description.get("format_version") != cls.FORMAT_VERSION:
            raise ValueError(
                "Unsupported Blosc2 operation format version: "
                f"{description.get('format_version')!r}"
            )
        if description.get("codec") != "zstd":
            raise ValueError(f"Unsupported Blosc2 codec: {description.get('codec')!r}")
        return cls(
            compression_level=description.get("compression_level"),
            filter_name=description.get("filter"),
            threads=description.get("threads"),
        )


def data_operation_from_description(description):
    """Construct the registered decoder named by wire metadata."""
    if not isinstance(description, dict):
        raise ValueError("Data operation description must be an object")
    name = description.get("name")
    if name == Blosc2ZstdOperation.NAME:
        return Blosc2ZstdOperation.from_description(description)
    raise ValueError(f"Unsupported data operation: {name!r}")


def load_data_operation(path):
    """Load the selected operation from a separate INI configuration file."""
    if path is None:
        return None
    path = Path(path)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error) as exc:
        raise ValueError(
            f"Unable to read compression configuration {path}: {exc}"
        ) from exc
    if not parser.has_section("compression"):
        raise ValueError(
            f"Compression configuration {path} needs a [compression] section"
        )
    values = parser["compression"]
    implementation = values.get("implementation", "").strip().lower()
    if implementation != "blosc2":
        raise ValueError(f"Unsupported compression implementation: {implementation!r}")
    codec = values.get("codec", "zstd").strip().lower()
    if codec != "zstd":
        raise ValueError(f"Unsupported Blosc2 codec: {codec!r}")
    filter_name = values.get("filter", "shuffle").strip().lower()
    try:
        compression_level = values.getint("compression_level", fallback=1)
        threads = values.getint("threads", fallback=1)
    except ValueError as exc:
        raise ValueError(
            "Blosc2 compression_level and threads must be integers"
        ) from exc
    operation = Blosc2ZstdOperation(
        compression_level=compression_level,
        filter_name=filter_name,
        threads=threads,
    )
    operation.check_available()
    return operation
