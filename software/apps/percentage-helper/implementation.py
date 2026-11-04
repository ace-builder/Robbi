"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext

from functools import wraps

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

def display(value, places=8):
    value = decimal(value)
    with localcontext() as settings:
        # A tiny nonzero percentage denominator can produce hundreds of integer
        # digits even though both inputs are within the manifest's finite range.
        settings.prec = max(60, value.adjusted() + places + 2)
        value = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if not value:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text

@handler
def percentage_helper(context, tool, args):
    value = decimal(args["value"])
    if tool == "percent_of":
        answer = value * decimal(args["percent"]) / 100
        return result(f"{display(answer)}", value=display(answer))
    if tool == "change":
        new = decimal(args["new_value"])
        if value == 0:
            raise InputError("Percentage change from zero is undefined.")
        change = (new - value) / abs(value) * 100
        return result(f"Change: {display(change)}%.", percent_change=display(change),
                      difference=display(new - value), convention="Change divided by the absolute original value.")
    percent = decimal(args["percent"])
    if args["operation"] == "discount" and percent > 100:
        raise InputError("A discount must be between 0 and 100 percent.")
    adjustment = value * percent / 100
    answer = value + adjustment * (-1 if args["operation"] == "discount" else 1)
    return result(f"Adjusted value: {display(answer)}; adjustment: {display(adjustment)}.",
                  value=display(answer), adjustment=display(adjustment))
