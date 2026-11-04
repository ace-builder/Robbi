"""Projects run from their ZIP alone, with no app-specific runtime modules."""
import ast
import io
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from robbi.app_runtime.host import AppHost
from robbi.errors import UnavailableError
from robbi.app_runtime.manifest import read_project


ROOT = Path(__file__).resolve().parents[1]
MIGRATED = [path.name for path in (ROOT / "apps").iterdir() if (path / "implementation.py").is_file()]
FORBIDDEN = {"robbi.app_services.utilities", "robbi.app_services.life_tools", "robbi.app_services.learning",
             "robbi.app_services.daily_planning", "robbi.app_services.creative_games", "robbi.app_services.creative_game_data",
             "robbi.mini_games"}


def archive(project):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as output:
        for path in project.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                output.write(path, path.relative_to(project).as_posix())
    return stream.getvalue()


@pytest.mark.parametrize("app", sorted(MIGRATED))
def test_project_zip_loads_in_an_empty_app_library_without_central_handler(app, tmp_path):
    project = ROOT / "apps" / app
    for path in project.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN for node in ast.walk(tree))
    host = AppHost(tmp_path / "empty-runtime", tmp_path / "data", SimpleNamespace(capture_token=lambda: None), permissions_enabled=False)
    try:
        detail = host.install(archive(project))
        assert detail["app"]["id"] == app
        assert set(host.packages.projects) == {app}
        with pytest.raises(UnavailableError, match="sandbox"):
            host._handler(host.project(app))
        assert host._modules == {}
        assert (host.project(app).root / "implementation.py").exists()
    finally:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("app,tool,arguments,key", [
    ("random-number", "generate", {"minimum": 7, "maximum": 7}, "numbers"),
    ("where-did-i-put-it", "remember", {"name": "Keys", "location": "Drawer"}, "item"),
    ("home-care", "manage", {"name": "Filter"}, "chore"),
    ("teach-me", "study", {"action": "start", "topic": "python"}, "lesson"),
    ("tiny-detective", "play", {"action": "start", "case": 1}, "choices"),
])
def test_representative_installed_projects_execute_with_only_sdk(app, tool, arguments, key, tmp_path, trusted_app_runner):
    host = AppHost(tmp_path / "empty-runtime", tmp_path / "data", SimpleNamespace(capture_token=lambda: None), permissions_enabled=False)
    try:
        host.install(archive(ROOT / "apps" / app))
        host.set_enabled(app, True)
        response = host.call(app, tool, arguments)
        assert response["ok"], response
        assert key in response["data"]
    finally:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


def test_all_bundled_projects_declare_author_and_compatible_sdk():
    for path in (ROOT / "apps").iterdir():
        if not (path / "app.yaml").is_file():
            continue
        manifest = read_project(path).manifest
        assert manifest["author"]["name"] == "Robbi project"
        assert manifest["requires"]["sdk"] == 2
        assert manifest["category"] in {"games", "productivity", "utilities", "home", "learning", "travel", "creative", "robot"}
        assert 2 <= len(manifest["tags"]) <= 4
