"""Run the downloader with public-network-only Python DNS resolution."""
import ipaddress
import socket
import sys

_original = socket.getaddrinfo

def public_addresses(*args, **kwargs):
    results = _original(*args, **kwargs)
    if not results or any(not ipaddress.ip_address(row[4][0].split('%')[0]).is_global for row in results):
        raise OSError('Only public internet video sources are allowed.')
    return results

socket.getaddrinfo = public_addresses
from yt_dlp import main
main(sys.argv[1:])
