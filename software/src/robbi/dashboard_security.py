"""Dashboard host policy independent of the client's Origin/forwarded headers."""

import ipaddress
import socket
from urllib.parse import urlsplit


def allowed_host(authority, configured=()):
    if not isinstance(authority, str) or any(c in authority for c in "/\\@ \r\n\t"):
        return False
    try:
        parsed = urlsplit("//" + authority)
        host = (parsed.hostname or "").lower().rstrip(".")
        parsed.port  # Reject malformed ports too.
    except ValueError:
        return False
    names = {"localhost", "robbi", "robbi.local", socket.gethostname().lower()}
    names |= {name + ".local" for name in tuple(names) if "." not in name}
    names.update(str(name).lower().rstrip(".") for name in configured)
    if host in names:
        return True
    # Literal LAN addresses cannot be controlled by an external DNS answer.
    # Hostnames are exact allowlist entries; never resolve a supplied hostname.
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or address.is_private or address.is_link_local
    except ValueError:
        return False


def password_value(value):
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 10 <= len(value) <= 128
        or any(ord(c) < 32 or c in "\"'" for c in value)
    ):
        raise ValueError(
            "Use a dashboard password of 10–128 characters without quotes or control characters."
        )
    return value
