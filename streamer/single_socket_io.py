import socket

from .raw_output_io import IO
from .socket_protocol import (
    PROTOCOL_VERSION,
    prove_private_key,
    receive_ack,
    send_arrays,
    send_end,
    send_hello,
)

class SingleSocketIO(IO):
    def __init__(
        self,
        settings,
        connection_info,
        private_key,
        connector=None,
        operation=None,
    ):
        self._socket = None
        self._operation = operation
        self._consumer_id = None
        self._hello_sent = False
        version = connection_info.get("protocol_version")
        if version != PROTOCOL_VERSION:
            raise ValueError(
                "Single-socket connection information uses unsupported "
                f"protocol version {version!r}; expected {PROTOCOL_VERSION}"
            )
        host = connection_info.get("host")
        if not isinstance(host, str) or not host:
            raise ValueError("Single-socket connection info has no valid host")
        port = connection_info.get("port")
        if not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError(f"Invalid single-socket connection port: {port}")
        consumer_id = connection_info.get("consumer_id")
        if not isinstance(consumer_id, str) or not consumer_id:
            raise ValueError("Single-socket connection info has no valid consumer_id")

        timeout = getattr(settings, "socket_timeout_seconds", None)
        connect = connector or socket.create_connection
        connection = connect((host, port), timeout=timeout)
        try:
            if connection.family in (socket.AF_INET, socket.AF_INET6):
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            prove_private_key(connection, private_key)
        except Exception:
            connection.close()
            raise
        self._socket = connection
        self._consumer_id = consumer_id

    def write_data(self, data):
        if not self._hello_sent:
            send_hello(self._socket, data, self._consumer_id)
            self._hello_sent = True
        send_arrays(
            self._socket,
            data.items(),
            operation=self._operation,
        )
        receive_ack(self._socket)

    def close(self):
        if self._socket is not None:
            try:
                send_end(self._socket)
            except OSError:
                pass
            self.abort()

    def abort(self):
        """Close a failed channel without attempting a protocol shutdown."""
        if self._socket is not None:
            self._socket.close()
            self._socket = None
