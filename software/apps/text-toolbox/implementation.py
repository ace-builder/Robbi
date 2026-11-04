"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from __future__ import annotations

from decimal import localcontext

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

@handler
def text_toolbox(context, tool, args):
    text = args["text"]
    if tool == "analyze":
        words = len(re.findall(r"\S+", text))
        lines = len(text.splitlines())
        return result(f"{words} words, {len(text)} characters, {lines} lines.", words=words,
                      characters=len(text), characters_without_whitespace=sum(not char.isspace() for char in text),
                      lines=lines, utf8_bytes=len(text.encode("utf-8")),
                      note="Words are whitespace-separated tokens; characters are Unicode code points.")
    operation = args["operation"]
    transformed = {"lower": str.lower, "upper": str.upper, "title": str.title,
                   "trim": str.strip, "normalize_whitespace": lambda value: " ".join(value.split())}[operation](text)
    # Unicode case mappings can expand a string, so apply a separate output bound.
    if len(transformed) > 6000:
        raise InputError("The transformed text is too long. Use a shorter input.")
    return result(transformed or "The result is empty.", text=transformed)
