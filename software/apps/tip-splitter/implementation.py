"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext

from functools import wraps

import re

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

def decimal(value):
    return Decimal(str(value))

def currency(value):
    value = value.upper().strip()
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise InputError("Use a three-letter currency label such as EUR, USD or GBP.")
    return value

@handler
def tip_splitter(context, tool, args):
    code = currency(args["currency"])
    precision = args["decimal_places"]
    quantum = Decimal(1).scaleb(-precision)
    bill = decimal(args["bill"]).quantize(quantum, rounding=ROUND_HALF_UP)
    tip = (bill * decimal(args["tip_percent"]) / 100).quantize(quantum, rounding=ROUND_HALF_UP)
    total = bill + tip
    minor_units = int(total / quantum)
    share, remainder = divmod(minor_units, args["people"])
    amounts = [format(Decimal(share + (i < remainder)) * quantum, f".{precision}f")
               for i in range(args["people"])]
    return result(f"Total {total:.{precision}f} {code}, including {tip:.{precision}f} tip. "
                  f"Shares: {', '.join(amounts)} {code}.", bill=f"{bill:.{precision}f}",
                  tip=f"{tip:.{precision}f}", total=f"{total:.{precision}f}", currency=code,
                  shares=amounts, rounding="Half up; spare smallest units go to the first people.")
