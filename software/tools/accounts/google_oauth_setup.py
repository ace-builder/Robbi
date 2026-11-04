#!/usr/bin/env python3
"""Connect a desktop browser to Robbi's Google apps; never prints credentials."""
from __future__ import annotations

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from robbi.app_services.http import HttpClient, ProviderError  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE_ROOT = "https://www.googleapis.com/auth/"


def selected_scopes(calendar, gmail):
    scopes = []
    if calendar != "none":
        scopes.append(SCOPE_ROOT + ("calendar.events" if calendar == "write" else "calendar.events.readonly"))
    if gmail != "none":
        scopes.append(SCOPE_ROOT + "gmail.readonly")
        if gmail == "draft":
            scopes.append(SCOPE_ROOT + "gmail.compose")
    if not scopes:
        raise ValueError("Select at least one Calendar or Gmail capability")
    return scopes


def _secret(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,8192}", value):
        raise ValueError("Google returned an invalid credential value")
    return value


def load_client(path):
    path = Path(path).expanduser()
    if path.stat().st_size > 65536:
        raise ValueError("OAuth client JSON exceeds 64 KiB")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        installed = value.get("installed") if isinstance(value, dict) else None
        if not isinstance(installed, dict):
            raise ValueError("Use a downloaded Desktop app OAuth client JSON file")
        return {"client_id": _secret(installed.get("client_id")), "client_secret": _secret(installed.get("client_secret"))}
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise ValueError("OAuth client file is not valid UTF-8 JSON") from None


def authorization_parameters(client_id, redirect_uri, scopes, *, state, verifier):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": " ".join(scopes), "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"}


def parse_callback(path, expected_state):
    """Validate a callback without reflecting authorization codes or errors."""
    if len(path) > 16384:
        raise ValueError("Invalid OAuth callback")
    parts = urlsplit(path)
    if parts.path != "/oauth2callback" or parts.scheme or parts.netloc or parts.fragment:
        raise ValueError("Invalid OAuth callback path")
    values = parse_qs(parts.query, max_num_fields=20, keep_blank_values=True)
    states = values.get("state", [])
    if len(states) != 1 or not states[0].isascii() or not secrets.compare_digest(states[0], expected_state):
        raise ValueError("OAuth state did not match; use the browser opened by this helper")
    if "error" in values:
        errors = values["error"]
        if "code" in values or len(errors) != 1 or not re.fullmatch(r"[A-Za-z_]{1,100}", errors[0]):
            raise ValueError("OAuth callback must contain one error or one authorization code")
        return None
    codes = values.get("code", [])
    if len(codes) != 1 or not 1 <= len(codes[0]) <= 8192 or any(not 33 <= ord(char) < 127 for char in codes[0]):
        raise ValueError("OAuth callback did not contain one authorization code")
    return codes[0]


def authorize(client, scopes, *, no_browser=False, timeout_s=180, http=None):
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    outcome = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # The default request log would disclose the authorization code.

        def do_GET(self):
            try:
                code = parse_callback(self.path, state)
            except ValueError:
                status, message = 400, "Invalid callback. Return to the original Google sign-in tab."
            else:
                outcome["code"] = code
                status, message = 200, "Google sign-in received. Close this tab and return to the setup terminal."
            content = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(content)

    class LoopbackServer(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(2)
            return connection, address

        def handle_error(self, request, client_address):
            pass  # A disconnected browser must not print callbacks or stack locals.

    with LoopbackServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = .5
        redirect = f"http://127.0.0.1:{server.server_port}/oauth2callback"
        parameters = authorization_parameters(client["client_id"], redirect, scopes, state=state, verifier=verifier)
        url = AUTH_URL + "?" + urlencode(parameters)
        print("Open this Google sign-in link in a browser on THIS computer:")
        print(url)
        if not no_browser:
            webbrowser.open(url)
        deadline = time.monotonic() + timeout_s
        while "code" not in outcome and time.monotonic() < deadline:
            server.handle_request()
    if "code" not in outcome:
        raise ValueError("Google sign-in timed out. Run the helper again on the computer with your browser")
    if outcome["code"] is None:
        raise ValueError("Google sign-in was declined; no credentials were saved")
    value = (http or HttpClient()).request(
        "POST", TOKEN_URL, form_body={**client, "code": outcome["code"], "code_verifier": verifier,
                                      "redirect_uri": redirect, "grant_type": "authorization_code"},
        max_bytes=32768, timeout_s=20, deadline=time.monotonic() + 25,
    ).json()
    if not isinstance(value, dict) or not value.get("refresh_token"):
        raise ValueError("Google did not issue a refresh token. Reconnect with consent; check the OAuth project and account")
    if "scope" in value and (not isinstance(value["scope"], str) or not set(scopes).issubset(value["scope"].split())):
        raise ValueError("Some requested permissions were declined. Run again with fewer capabilities or grant all selected scopes")
    return _secret(value["refresh_token"])


def write_credentials(path, client, refresh_token):
    """Create exclusively, private on POSIX; do not overwrite an existing env file."""
    values = {"ROBBI_GOOGLE_CLIENT_ID": _secret(client["client_id"]),
              "ROBBI_GOOGLE_CLIENT_SECRET": _secret(client["client_secret"]),
              "ROBBI_GOOGLE_REFRESH_TOKEN": _secret(refresh_token)}
    content = "".join(f"{name}={value}\n" for name, value in values.items())
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(content)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-secrets", required=True, type=Path, help="Downloaded Google Desktop app OAuth client JSON")
    parser.add_argument("--output", required=True, type=Path, help="New private env file; existing files are never overwritten")
    parser.add_argument("--calendar", choices=("none", "read", "write"), default="read", help="Calendar capability (default: read)")
    parser.add_argument("--gmail", choices=("none", "read", "draft"), default="read", help="Gmail capability; draft also permits reading (default: read)")
    parser.add_argument("--no-browser", action="store_true", help="Print the sign-in URL without opening a browser")
    args = parser.parse_args(argv)
    try:
        if args.output.expanduser().exists():
            raise ValueError("Output already exists. Choose a new private file; existing credentials are never overwritten")
        scopes = selected_scopes(args.calendar, args.gmail)
        client = load_client(args.client_secrets)
        print("Requesting these Google account permissions:")
        for scope in scopes:
            print("  " + scope)
        token = authorize(client, scopes, no_browser=args.no_browser)
        write_credentials(args.output, client, token)
    except (OSError, ValueError, ProviderError, TimeoutError) as error:
        print("Setup failed: " + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Setup cancelled.", file=sys.stderr)
        return 130
    print("Saved credentials to the requested private file. Merge its three variables into Robbi's .env.secrets and restart Robbi.")
    print("Set a local timezone in Google Calendar settings and approve network access for each Google app in App library.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
