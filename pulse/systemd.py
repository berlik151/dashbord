"""Minimal sd_notify: readiness and watchdog pings, no third-party dependency."""
import os
import socket


def notify(state):
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return False
    if address.startswith('@'):
        address = '\0' + address[1:]  # abstract namespace socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
            sock.connect(address)
            sock.sendall(state.encode())
        return True
    except OSError:
        return False
