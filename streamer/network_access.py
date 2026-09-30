"""Low-cost source-address filtering for consumer sockets."""

import argparse
import errno
import ipaddress
import socket


def ipv4_network(value):
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid IP range: {value}") from exc
    if network.version != 4:
        raise argparse.ArgumentTypeError("only IPv4 ranges are supported")
    return network


def tcp_port_range(value):
    parts = value.split("-")
    if len(parts) not in (1, 2):
        raise argparse.ArgumentTypeError("port must be PORT or START-END")
    try:
        start = int(parts[0], 10)
        end = int(parts[1], 10) if len(parts) == 2 else 65535
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be PORT or START-END") from exc
    if not 1 <= start <= 65535 or not 1 <= end <= 65535:
        raise argparse.ArgumentTypeError("ports must be between 1 and 65535")
    if end < start:
        raise argparse.ArgumentTypeError("port range end must not precede start")
    return start, end


def create_tcp_listener(bind_host, ports, backlog):
    """Bind and listen on an ephemeral port or the first available requested port."""
    candidates = (0,) if ports is None else range(ports[0], ports[1] + 1)
    last_in_use = None
    for port in candidates:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((bind_host, port))
            listener.listen(backlog)
            return listener
        except OSError as exc:
            listener.close()
            if ports is not None and exc.errno == errno.EADDRINUSE:
                last_in_use = exc
                continue
            raise

    start, end = ports
    raise OSError(
        errno.EADDRINUSE,
        f"No available TCP port in {start}-{end} on {bind_host}",
    ) from last_in_use


def accept_from_allowed_network(listener, allowed_networks):
    """Return the next allowed connection, silently closing other peers."""
    while True:
        connection, address = listener.accept()
        try:
            peer_address = ipaddress.ip_address(address[0])
        except ValueError:
            connection.close()
            continue
        if any(peer_address in network for network in allowed_networks):
            return connection, address
        connection.close()
