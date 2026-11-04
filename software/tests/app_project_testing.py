"""Patch the actual project modules while preserving lazy host discovery."""
import importlib
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]


def source_module(app, name="implementation"):
    prefix = "_test_source_" + app.replace("-", "_")
    if prefix not in sys.modules:
        package = types.ModuleType(prefix)
        package.__path__ = [str(ROOT / "apps" / app)]
        sys.modules[prefix] = package
    return importlib.import_module(prefix + "." + name)


def project_module(host, app, name="implementation"):
    host._handler(host.project(app))
    return importlib.import_module(host._modules[app][1] + "." + name)


def patch_on_load(monkeypatch, host, apps, **attributes):
    original = host._handler
    patched = set()
    def load(project):
        handler = original(project)
        prefix = host._modules[project.id][1]
        if project.id in apps and prefix not in patched:
            module = importlib.import_module(prefix + ".implementation")
            for key, value in attributes.items():
                if hasattr(module, key):
                    monkeypatch.setattr(module, key, value)
            patched.add(prefix)
        return handler
    monkeypatch.setattr(host, "_handler", load)
