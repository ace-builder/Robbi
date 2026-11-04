"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

from decimal import localcontext

from functools import wraps

import secrets

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
def random_numbers(context, tool, args):
    low, high, count = args["minimum"], args["maximum"], args["count"]
    if low > high:
        raise InputError("Minimum must be no greater than maximum.")
    if args["unique"] and count > high - low + 1:
        raise InputError("There are not enough integers in that range for distinct results.")
    generator = secrets.SystemRandom()
    values = (generator.sample(range(low, high + 1), count) if args["unique"] else
              [low + secrets.randbelow(high - low + 1) for _ in range(count)])
    return result(", ".join(map(str, values)), numbers=values, minimum=low, maximum=high,
                  unique=args["unique"], source="operating-system randomness")
