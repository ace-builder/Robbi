"""Package metadata, no-execution previews and reversible filesystem changes."""
import io
import json
from pathlib import Path
import stat
import zipfile

from PIL import Image
import pytest
import yaml

from robbi.app_runtime import packages as package_module
from robbi.app_runtime.manifest import compatibility, read_project, version_key
from robbi.app_runtime.packages import Packages, inspect_archive
from robbi.errors import ValidationError


def manifest(**changes):
    value = {"api_version": 2, "id": "example", "version": "1.0.0", "permissions": ["state"],
             "tools": [{"name": "run", "description": "Example", "input_schema": {"type": "object", "properties": {}}}]}
    value.update(changes)
    return value


def png(size):
    stream = io.BytesIO()
    Image.new("RGB", size, "blue").save(stream, format="PNG")
    return stream.getvalue()


def archive(*, changes=None, extra=(), code="raise RuntimeError('must never execute during preview')\n"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("project/app.yaml", yaml.safe_dump(manifest(**(changes or {}))))
        output.writestr("project/main.py", code)
        for name, data in extra:
            output.writestr(name, data)
    return stream.getvalue()


def bundled(tmp_path, **changes):
    project = tmp_path / "bundled" / "example"
    project.mkdir(parents=True)
    (project / "app.yaml").write_text(yaml.safe_dump(manifest(**changes)), encoding="utf-8")
    (project / "main.py").write_text("original source", encoding="utf-8")
    return project


def test_legacy_manifest_defaults_and_numeric_version_ordering(tmp_path):
    project = bundled(tmp_path, version="1.0")
    value = read_project(project).manifest
    assert value["author"] == {"name": "Unknown", "url": ""}
    assert value["images"] == {} and value["requires"]["sdk"] == 2
    assert value["version"] == "1.0.0"
    assert version_key("1.10.0") > version_key("1.9.9")
    assert compatibility(value)["compatible"]


def test_preview_validates_full_metadata_and_exact_images_without_import(tmp_path):
    image_paths = {"icon": "assets/icon.png", "screen": "assets/screen.png"}
    data = archive(changes={"author": {"name": "maker", "url": "https://example.org/author"}, "images": image_paths,
        "category": "productivity", "tags": ["notes", "offline"], "homepage": "https://example.org/app", "license": "MIT",
        "requires": {"sdk": 2, "runtime": ">=2.0.0,<3", "python": ">=3.11,<3.13", "dependencies": [{"name": "Pillow", "version": ">=11,<13"}]}},
        extra=[("project/assets/icon.png", png((1024, 1024))), ("project/assets/screen.png", png((280, 240)))])
    preview = inspect_archive(data, include_images=True)
    assert preview["manifest"]["images"] == image_paths
    assert preview["images"]["icon"]["width"] == 1024 and preview["images"]["screen"]["height"] == 240
    assert preview["images"]["screen"]["data"].startswith(b"\x89PNG")
    assert preview["compatibility"]["compatible"]
    assert len(preview["sha256"]) == 64 and preview["zip_bytes"] == len(data)
    assert "data" not in inspect_archive(data)["images"]["icon"]
    json.dumps(inspect_archive(data))
    packages = Packages(tmp_path / "bundled", tmp_path / "installed")
    assert packages.preview(data)["operation"] == "install"
    assert not packages.installed.exists()  # Preview does not alter the library.


@pytest.mark.parametrize("changes,extra", [
    ({"author": "maker"}, []), ({"author": {"name": ""}}, []),
    ({"homepage": "javascript:alert(1)"}, []), ({"author": {"name": "A", "url": "https://user:secret@example.org"}}, []),
    ({"tags": ["Tag", "tag"]}, []), ({"category": "../escape"}, []),
    ({"images": {"icon": "../icon.png"}}, []),
    ({"images": {"icon": "icon.png"}}, [("project/icon.png", png((280, 240)))]),
    ({"images": {"screen": "screen.png"}}, [("project/screen.png", b"not an image")]),
    ({"requires": {"sdk": True}}, []), ({"requires": {"runtime": ">=2; invoke code"}}, []),
    ({"requires": {"dependencies": [{"name": "x @ https://example.org/x.whl"}]}}, []),
    ({"version": "01.2.3"}, []), ({"version": "1.2.3-beta"}, []),
])
def test_invalid_metadata_and_images_are_rejected(changes, extra):
    with pytest.raises(ValidationError):
        inspect_archive(archive(changes=changes, extra=extra))


def test_incompatible_sdk_and_missing_dependency_are_previewable_but_not_installed(tmp_path):
    data = archive(changes={"requires": {"sdk": 99, "dependencies": [{"name": "robbi-definitely-missing-distribution"}]}})
    preview = inspect_archive(data)
    assert not preview["compatibility"]["compatible"] and len(preview["compatibility"]["reasons"]) == 2
    packages = Packages(tmp_path / "bundled", tmp_path / "installed")
    with pytest.raises(ValidationError, match="requirements"):
        packages.install(data)
    assert not packages.installed.exists()


def test_dependency_comparison_understands_local_and_post_versions(monkeypatch):
    from robbi.app_runtime import manifest as module
    monkeypatch.setattr(module.metadata, "version", lambda name: "2.0.0+cpu" if name == "local" else "2.0.0.post1")
    assert compatibility({"requires": {"dependencies": [{"name": "local", "version": ">=2,<3"}, {"name": "post", "version": ">=2,<3"}]}})["compatible"]


def test_bundled_remove_is_a_persistent_tombstone_and_restore_keeps_source(tmp_path):
    project = bundled(tmp_path)
    original = (project / "main.py").read_bytes()
    packages = Packages(project.parent, tmp_path / "installed")
    packages.scan()
    assert packages.remove("example")["source"] == "bundled"
    assert (project / "main.py").read_bytes() == original and "example" not in packages.projects
    restarted = Packages(project.parent, tmp_path / "installed")
    restarted.scan()
    assert "example" not in restarted.projects
    assert restarted.removed()[0]["restore_possible"]
    assert restarted.available_bundled()[0]["removed"]
    assert restarted.restore("example") == "example"
    assert restarted.projects["example"].bundled and restarted.removed() == []


def test_update_overlay_and_removed_import_restore_keep_code_and_separate_data(tmp_path):
    project = bundled(tmp_path)
    packages = Packages(project.parent, tmp_path / "installed")
    packages.scan()
    user_data = tmp_path / "data" / "example" / "state.json"
    user_data.parent.mkdir(parents=True)
    user_data.write_text('{"private":"preserve"}')
    update = archive(changes={"version": "1.1.0", "permissions": ["state", "network"]}, code="updated code")
    preview = packages.preview(update)
    assert preview["operation"] == "update" and preview["permission_changes"] == {"added": ["network"], "removed": [], "unchanged": ["state"]}
    packages.install(update)
    assert not packages.projects["example"].bundled
    assert (project / "main.py").read_text() == "original source"
    packages.remove("example")
    assert "example" not in packages.projects and packages.removed()[0]["source"] == "installed"
    packages.restore("example")
    assert (packages.projects["example"].root / "main.py").read_text() == "updated code"
    assert user_data.read_text() == '{"private":"preserve"}'


def test_updates_work_at_capacity_but_downgrades_do_not(tmp_path, monkeypatch):
    packages = Packages(tmp_path / "bundled", tmp_path / "installed")
    packages.install(archive(changes={"version": "1.10.0"}))
    monkeypatch.setattr(packages, "MAX_APPS", 1)
    packages.install(archive(changes={"version": "1.11.0"}))
    with pytest.raises(ValidationError, match="downgrade"):
        packages.install(archive(changes={"version": "1.9.0"}))
    assert packages.projects["example"].manifest["version"] == "1.11.0"


def test_failed_remove_registry_commit_rolls_back_renamed_project(tmp_path, monkeypatch):
    packages = Packages(tmp_path / "bundled", tmp_path / "installed")
    packages.install(archive(code="original"))
    def fail(*args):
        raise OSError("simulated disk error")
    monkeypatch.setattr(packages, "_save_removed", fail)
    with pytest.raises(OSError, match="disk"):
        packages.remove("example")
    assert (packages.installed / "example" / "main.py").read_text() == "original"
    assert "example" in packages.scan()


def test_failed_restore_registry_commit_keeps_retained_code_restorable(tmp_path, monkeypatch):
    packages = Packages(tmp_path / "bundled", tmp_path / "installed")
    packages.install(archive(code="original"))
    packages.remove("example")
    def fail(*args):
        raise OSError("simulated disk error")
    monkeypatch.setattr(packages, "_save_removed", fail)
    with pytest.raises(OSError):
        packages.restore("example")
    assert packages.removed()[0]["restore_possible"]
    assert "example" not in packages.scan()


def test_failed_install_commit_preserves_removed_receipt_and_original_code(tmp_path, monkeypatch):
    packages = Packages(tmp_path / "bundled", tmp_path / "installed")
    packages.install(archive(code="original"))
    packages.remove("example")
    def fail(*args):
        raise OSError("simulated disk error")
    monkeypatch.setattr(packages, "_save_removed", fail)
    with pytest.raises(OSError):
        packages.install(archive(code="replacement"))
    assert packages.removed()[0]["restore_possible"]
    assert not (packages.installed / "example").exists()


def test_corrupt_tombstones_never_reactivate_bundled_apps(tmp_path):
    project = bundled(tmp_path)
    packages = Packages(project.parent, tmp_path / "installed")
    packages._removed_file.write_text('{"broken":', encoding="utf-8")
    assert packages.scan() == {} and packages.errors
    with pytest.raises(ValidationError, match="registry"):
        packages.install(archive())


@pytest.mark.parametrize("name", ["project/NUL.txt", "project/file. ", "project/./a", "project//a", "outside.txt"])
def test_nonportable_or_outside_project_paths_are_rejected(name):
    with pytest.raises(ValidationError):
        inspect_archive(archive(extra=[(name, b"content")]))


def test_duplicate_casefold_paths_and_symlinks_are_rejected():
    with pytest.raises(ValidationError, match="Duplicate"):
        inspect_archive(archive(extra=[("project/MAIN.py", b"different")]))
    link = zipfile.ZipInfo("project/link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValidationError, match="Links"):
        inspect_archive(archive(extra=[(link, b"../outside")]))


def test_parent_directory_case_alias_is_rejected_on_every_platform():
    with pytest.raises(ValidationError, match="case-insensitive"):
        inspect_archive(archive(extra=[("project/Assets/a.txt", b"a"), ("project/assets/b.txt", b"b")]))


def test_first_install_checks_bundled_versions_without_requiring_manual_scan(tmp_path):
    source = bundled(tmp_path, version="2.0.0")
    packages = Packages(source.parent, tmp_path / "installed")
    assert packages.preview(archive(changes={"version": "2.1.0"}))["operation"] == "update"
    with pytest.raises(ValidationError, match="downgrade"):
        Packages(source.parent, tmp_path / "installed").install(archive(changes={"version": "1.9.0"}))
