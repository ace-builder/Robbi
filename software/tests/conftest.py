"""Explicit test doubles; production isolation never has an in-process fallback."""

import importlib
import os
from pathlib import Path
import sys
import types
import uuid

import pytest


@pytest.fixture(autouse=True)
def child_processes_use_this_checkout(monkeypatch):
    """A different editable Robbi install must not supply subprocess imports."""
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1]/'src'))


@pytest.fixture
def trusted_app_runner(monkeypatch):
    """Use only for SDK/lifecycle unit tests with test-authored code.

    These tests inject filesystem/robot faults and run on Windows too. Real
    hostile-code isolation is exercised separately by test_app_sandbox_linux.
    """
    from robbi.app_runtime import sandbox

    def execute(project, context, tool, arguments):
        prefix = "_trusted_test_" + uuid.uuid4().hex
        package = types.ModuleType(prefix)
        package.__path__ = [str(project.root)]
        sys.modules[prefix] = package
        try:
            module, name = project.manifest["entrypoint"].split(":")
            return getattr(importlib.import_module(prefix + "." + module), name)(
                context, tool, arguments
            )
        finally:
            for key in tuple(sys.modules):
                if key == prefix or key.startswith(prefix + "."):
                    sys.modules.pop(key, None)

    monkeypatch.setattr(sandbox, "run", execute)
