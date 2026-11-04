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

def clean(value, label, maximum):
    value = value.strip()
    if not value or len(value) > maximum:
        raise InputError(f"Provide {label} of 1 to {maximum} characters.")
    return value

@handler
def recipe_scaler(context, tool, args):
    factor = decimal(args["wanted_portions"]) / decimal(args["original_portions"])
    ingredients = [{"name": clean(item["name"], "an ingredient name", 80),
                    "amount": display(decimal(item["amount"]) * factor), "unit": item["unit"].strip()}
                   for item in args["ingredients"]]
    return result("; ".join(f"{item['amount']} {item['unit']} {item['name']}".strip() for item in ingredients),
                  factor=display(factor), ingredients=ingredients,
                  note="Amounts scale linearly; cooking time and temperature do not.")
