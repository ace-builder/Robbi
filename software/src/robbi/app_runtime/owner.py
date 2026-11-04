"""Owner decision boundary, reusing the dashboard's existing authentication."""
from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

from flask import current_app, g, request
from werkzeug.exceptions import Forbidden


def _loopback(value):
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def owner_access():
    # With dashboard authentication disabled, the owner has chosen to trust
    # dashboard clients on this network. App capabilities still require a
    # separate, explicit stored approval; disabling sign-in grants none.
    config = current_app.config.get("ROBBI_RUNTIME_CONFIG")
    if config is not None:
        if not config.values.get("server", {}).get("auth_enabled", False):
            return True, ""
        if getattr(g, "robbi_owner_authenticated", False):
            return True, ""
        return False, "Sign in to the dashboard to manage apps and their permissions."
    # A separately mounted permission blueprint has no trusted-network policy.
    # Never trust forwarded client-address headers here.
    hostname = urlsplit(request.host_url).hostname or ""
    if _loopback(request.remote_addr or "") and (hostname == "localhost" or _loopback(hostname)):
        return True, ""
    return False, "Use the parent owner dashboard to manage apps and their permissions."


def require_owner():
    allowed, hint = owner_access()
    if not allowed:
        raise Forbidden(hint)
