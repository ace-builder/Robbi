"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

from decimal import localcontext

from functools import wraps

import secrets

import string

class InputError(ValueError):
    """An expected, actionable input problem rather than a broken app."""

def handler(function):
    @wraps(function)
    def run(context, tool_name, arguments):
        context.checkpoint()
        try:
            # Keep intermediate conversions accurate before display rounding;
            # never change another app worker's decimal context.
            with localcontext() as settings:
                settings.prec = 60
                return function(context, tool_name, arguments)
        except InputError as error:
            return {"ok": False, "text": str(error)}
    return run

def result(message, **data):
    return {"ok": True, "text": message, "data": data}

@handler
def password_generator(context, tool, args):
    groups = [pool for enabled, pool in [(args["lowercase"], string.ascii_lowercase),
                                         (args["uppercase"], string.ascii_uppercase),
                                         (args["digits"], string.digits),
                                         (args["symbols"], "!@#$%^&*()-_=+[]{}:,.?")] if enabled]
    if args["exclude_ambiguous"]:
        groups = ["".join(char for char in pool if char not in "Il1O0o") for pool in groups]
    if not groups:
        raise InputError("Enable at least one character group.")
    alphabet = "".join(groups)
    passwords = []
    # Rejection sampling is uniform over valid strings and guarantees each group.
    for _ in range(args["count"]):
        while True:
            context.checkpoint()
            password = "".join(secrets.choice(alphabet) for _ in range(args["length"]))
            if all(any(char in pool for char in password) for pool in groups):
                passwords.append(password)
                break
    return result("Generated passwords are visible in this result and may be retained in chat/job history.",
                  passwords=passwords, length=args["length"], source="operating-system randomness",
                  note="This app does not save passwords in its own state and is not a password vault.")
