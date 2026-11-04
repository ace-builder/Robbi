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

UNITS = {
    "mm": ("length", ".001"), "cm": ("length", ".01"), "m": ("length", "1"),
    "km": ("length", "1000"), "in": ("length", ".0254"), "ft": ("length", ".3048"),
    "yd": ("length", ".9144"), "mi": ("length", "1609.344"),
    "mg": ("mass", ".001"), "g": ("mass", "1"), "kg": ("mass", "1000"),
    "oz": ("mass", "28.349523125"), "lb": ("mass", "453.59237"),
    "ml": ("volume", ".001"), "l": ("volume", "1"), "us_gal": ("volume", "3.785411784"),
    "us_cup": ("volume", ".2365882365"), "us_tbsp": ("volume", ".01478676478125"),
    "us_tsp": ("volume", ".00492892159375"), "imperial_gal": ("volume", "4.54609"),
    "m/s": ("speed", "3.6"), "km/h": ("speed", "1"), "mph": ("speed", "1.609344"),
    "ms": ("time", ".001"), "s": ("time", "1"), "min": ("time", "60"),
    "h": ("time", "3600"), "day": ("time", "86400"), "week": ("time", "604800"),
    "bit": ("data", ".125"), "B": ("data", "1"), "kB": ("data", "1000"),
    "MB": ("data", "1000000"), "GB": ("data", "1000000000"),
    "KiB": ("data", "1024"), "MiB": ("data", "1048576"), "GiB": ("data", "1073741824"),
    "cm2": ("area", ".0001"), "m2": ("area", "1"), "ha": ("area", "10000"),
    "km2": ("area", "1000000"), "ft2": ("area", ".09290304"), "acre": ("area", "4046.8564224"),
    "Pa": ("pressure", "1"), "kPa": ("pressure", "1000"), "bar": ("pressure", "100000"),
    "atm": ("pressure", "101325"), "J": ("energy", "1"), "kJ": ("energy", "1000"),
    "kWh": ("energy", "3600000"), "kcal": ("energy", "4184"),
    "C": ("temperature", "1"), "F": ("temperature", "1"), "K": ("temperature", "1"),
}

@handler
def unit_converter(context, tool, args):
    source, target, value = args["from_unit"], args["to_unit"], decimal(args["value"])
    if UNITS[source][0] != UNITS[target][0]:
        raise InputError("Choose two units from the same category; mass and volume require an ingredient's density.")
    category = UNITS[source][0]
    if category == "temperature":
        kelvin = {"C": lambda: value + Decimal("273.15"), "F": lambda: (value - 32) * 5 / 9 + Decimal("273.15"),
                  "K": lambda: value}[source]()
        if kelvin < 0:
            raise InputError("Temperature cannot be below absolute zero.")
        answer = {"C": lambda: kelvin - Decimal("273.15"), "F": lambda: (kelvin - Decimal("273.15")) * 9 / 5 + 32,
                  "K": lambda: kelvin}[target]()
    else:
        answer = value * decimal(UNITS[source][1]) / decimal(UNITS[target][1])
    return result(f"{display(answer)} {target}", value=display(answer), unit=target, category=category)
