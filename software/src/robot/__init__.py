"""Compatibility namespace for existing V2 apps; implementations live in robbi.

Aliases are lazy and refer to the canonical module object. Importing this package
does not initialize settings, hardware, providers or background threads.
"""

import importlib
from importlib.abc import Loader, MetaPathFinder
from importlib.util import find_spec, spec_from_loader
import sys

# Never discover stale files from an earlier installation in this directory.
__path__ = []
_MOVED = {
    "robot.config": "robbi.configuration.settings",
    "robot.settings_schema": "robbi.configuration.schema",
    "robot.autonomous_settings": "robbi.configuration.autonomy",
}


class _AliasLoader(Loader):
    def __init__(self, target):
        self.target = target

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        # Replace the temporary alias, preserving the real module's __spec__.
        sys.modules[module.__name__] = importlib.import_module(self.target)


class _Aliases(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("robot."):
            return None
        canonical = _MOVED.get(
            fullname, "robbi.engine." + fullname.removeprefix("robot.")
        )
        try:
            original = find_spec(canonical)
        except ModuleNotFoundError:
            return None
        if original is None:
            return None
        return spec_from_loader(
            fullname,
            _AliasLoader(canonical),
            is_package=original.submodule_search_locations is not None,
        )


if not any(
    type(f).__module__ == __name__ and type(f).__name__ == "_Aliases"
    for f in sys.meta_path
):
    sys.meta_path.insert(0, _Aliases())


def __getattr__(name):
    if name.startswith("_"):
        raise AttributeError(name)
    try:
        return importlib.import_module(__name__ + "." + name)
    except ModuleNotFoundError as error:
        if error.name == __name__ + "." + name:
            raise AttributeError(name) from None
        raise
