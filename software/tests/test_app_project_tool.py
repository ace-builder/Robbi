import importlib.util
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pytest

from robbi.app_runtime.packages import Packages
from robbi.errors import ValidationError

_SPEC = importlib.util.spec_from_file_location("robbi_app_project_tool", Path(__file__).resolve().parents[1] / "tools" / "apps" / "app_project.py")
project_tool = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = project_tool
_SPEC.loader.exec_module(project_tool)


def scaffold(tmp_path, name="hello-app"):
    path = tmp_path / name
    project_tool.new_project(path, name)
    return path


def test_scaffold_is_valid_and_uses_relative_imports(tmp_path):
    path = scaffold(tmp_path)
    inventory = project_tool.inspect_project(path)
    assert inventory.project.id == "hello-app"
    assert len(inventory.files) == 4
    assert "from .helpers import greeting" in (path / "main.py").read_text()


def test_scaffold_never_overwrites_existing_directory_or_files(tmp_path):
    path = scaffold(tmp_path)
    original = (path / "main.py").read_bytes()
    with pytest.raises(ValidationError, match="already exists"):
        project_tool.new_project(path, "hello-app")
    assert (path / "main.py").read_bytes() == original


def test_check_and_pack_never_import_app_code(tmp_path):
    path = scaffold(tmp_path)
    (path / "main.py").write_text("raise RuntimeError('Do not execute this code')\n")
    assert project_tool.inspect_project(path).project.id == "hello-app"
    result = project_tool.pack_project(path, tmp_path / "hello.zip")
    assert result["files"] == 4


def test_pack_excludes_secrets_venvs_caches_and_local_configuration(tmp_path):
    path = scaffold(tmp_path)
    hidden = [".env", ".env.secrets", ".env.example", ".venv/huge.bin", ".git/config", "__pycache__/x.pyc",
              "config/local.yaml", "config/local.yml", "credentials.json", "secrets.yaml", "key.pem", "private.key",
              "id_rsa", "id_ed25519.pub", "secrets/token.txt", "private_keys/a.json", "service-account.json", ".netrc"]
    for name in hidden:
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"SENSITIVE")
    output = path / "hello.zip"
    result = project_tool.pack_project(path, output)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"app.yaml", "main.py", "helpers.py", "README.md"}
        assert all(b"SENSITIVE" not in archive.read(name) for name in archive.namelist())
    assert result["excluded"]
    assert not any(name.endswith("hello.zip") for name in archive.namelist())


def test_existing_output_is_unchanged(tmp_path):
    path = scaffold(tmp_path)
    output = tmp_path / "existing.zip"
    output.write_bytes(b"original")
    with pytest.raises(ValidationError, match="already exists"):
        project_tool.pack_project(path, output)
    assert output.read_bytes() == b"original"


def test_exclusive_output_creation_closes_race(tmp_path, monkeypatch):
    path = scaffold(tmp_path)
    output = tmp_path / "existing.zip"
    real_inspect = project_tool.inspect_project
    def racing_inspect(*args, **kwargs):
        result = real_inspect(*args, **kwargs)
        output.write_bytes(b"another writer")
        return result
    monkeypatch.setattr(project_tool, "inspect_project", racing_inspect)
    with pytest.raises(FileExistsError):
        project_tool.pack_project(path, output)
    assert output.read_bytes() == b"another writer"


def test_pack_round_trips_through_actual_importer(tmp_path):
    source = scaffold(tmp_path)
    output = tmp_path / "hello.zip"
    project_tool.pack_project(source, output)
    packages = Packages(tmp_path / "empty-bundled", tmp_path / "installed")
    assert packages.install(output.read_bytes()) == "hello-app"
    assert not packages.projects["hello-app"].bundled
    assert not packages.errors


def test_scaffold_runs_through_actual_host_with_relative_import_and_settings(tmp_path, trusted_app_runner):
    from robbi.app_runtime.host import AppHost
    source = scaffold(tmp_path)
    output = tmp_path / "hello.zip"
    project_tool.pack_project(source, output)
    host = AppHost(tmp_path / "robot", tmp_path / "data", SimpleNamespace(capture_token=lambda: None), permissions_enabled=False)
    try:
        host.install(output.read_bytes())
        assert not host.enabled("hello-app")
        host.configure("hello-app", {"greeting": "Welcome"})
        host.set_enabled("hello-app", True)
        assert host.call("hello-app", "hello", {"name": "maker"})["text"] == "Welcome, maker!"
    finally:
        host.stop()


def test_too_many_files_rejected_before_output_created(tmp_path):
    path = scaffold(tmp_path)
    for index in range(253):
        (path / f"file-{index}.txt").write_text("")
    output = tmp_path / "large.zip"
    with pytest.raises(ValidationError, match="256"):
        project_tool.pack_project(path, output)
    assert not output.exists()


def test_expanded_size_limit_rejects_sparse_file_without_reading_it(tmp_path):
    path = scaffold(tmp_path)
    with (path / "large.dat").open("wb") as output:
        output.truncate(project_tool.MAX_EXPANDED + 1)
    with pytest.raises(ValidationError, match="32 MiB"):
        project_tool.inspect_project(path)


def test_compressed_zip_limit_removes_incomplete_output(tmp_path, monkeypatch):
    path = scaffold(tmp_path)
    (path / "asset.bin").write_bytes(os.urandom(4096))
    monkeypatch.setattr(project_tool, "MAX_ZIP", 1500)
    output = tmp_path / "too-big.zip"
    with pytest.raises(ValidationError, match="16 MiB"):
        project_tool.pack_project(path, output)
    assert not output.exists()


def test_changed_source_size_removes_incomplete_zip(tmp_path, monkeypatch):
    path = scaffold(tmp_path)
    real_inspect = project_tool.inspect_project
    def changing_inspect(*args, **kwargs):
        result = real_inspect(*args, **kwargs)
        with (path / "main.py").open("a") as source:
            source.write("# changed\n")
        return result
    monkeypatch.setattr(project_tool, "inspect_project", changing_inspect)
    output = tmp_path / "changed.zip"
    with pytest.raises(ValidationError, match="changed"):
        project_tool.pack_project(path, output)
    assert not output.exists()


def test_links_rejected_without_following_them(tmp_path, monkeypatch):
    path = scaffold(tmp_path)
    # Mock the platform link probe so this runs without Windows symlink rights.
    real_link = project_tool._link
    monkeypatch.setattr(project_tool, "_link", lambda candidate: candidate.name == "helpers.py" or real_link(candidate))
    with pytest.raises(ValidationError, match="Links"):
        project_tool.inspect_project(path)


def test_excluded_entrypoint_cannot_produce_unusable_archive(tmp_path):
    path = scaffold(tmp_path)
    (path / "main.py").rename(path / "secrets.py")
    manifest = path / "app.yaml"
    manifest.write_text(manifest.read_text().replace("main:run", "secrets:run"))
    with pytest.raises(ValidationError, match="required"):
        project_tool.inspect_project(path)


def test_nested_second_manifest_rejected(tmp_path):
    path = scaffold(tmp_path)
    (path / "other").mkdir()
    (path / "other" / "app.yaml").write_text("api_version: 2")
    with pytest.raises(ValidationError, match="exactly one"):
        project_tool.inspect_project(path)


def test_cli_reports_validation_errors_without_traceback(tmp_path, capsys):
    assert project_tool.main(["check", str(tmp_path / "missing")]) == 1
    assert "Traceback" not in capsys.readouterr().err
    path = scaffold(tmp_path)
    assert project_tool.main(["check", str(path)]) == 0
    assert '"valid": true' in capsys.readouterr().out
