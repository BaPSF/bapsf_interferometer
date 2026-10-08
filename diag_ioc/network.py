"""TCP listening and source-address filtering for the shot link.

Copied from streamer/network_access.py, so diag_ioc never imports streamer (docs/ARCHITECTURE.md D6).
"""
import argparse
import ipaddress
import socket


def ipv4_network(value):
	"""An IPv4Network from CIDR text; argparse.ArgumentTypeError otherwise, so it also serves as an argparse `type=`."""
	try:
		network = ipaddress.ip_network(value, strict=False)
	except ValueError as exc:
		raise argparse.ArgumentTypeError(f"invalid IP range: {value}") from exc
	if network.version != 4:
		raise argparse.ArgumentTypeError("only IPv4 ranges are supported")
	return network


def create_tcp_listener(host, port, backlog):
	"""A listening IPv4 TCP socket on host:port; port 0 binds an ephemeral port."""
	listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	try:
		listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		listener.bind((host, port))
		listener.listen(backlog)
	except BaseException:
		listener.close()
		raise
	return listener


def accept_from_allowed_network(listener, allowed_networks):
	"""The next connection from a peer in `allowed_networks`; other peers are closed unread."""
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
