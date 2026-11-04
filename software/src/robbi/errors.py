"""Actionable failures shared across HTTP, plugins and device boundaries."""

class UnavailableError(RuntimeError):
    pass

class BusyError(RuntimeError):
    pass

class ValidationError(ValueError):
    pass

