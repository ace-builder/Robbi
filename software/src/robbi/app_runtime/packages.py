"""Bounded manifest-only ZIP inspection and reversible project lifecycle."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import stat
import struct
import tempfile
import threading
import time
import uuid
import zipfile

from robbi.errors import ValidationError
from robbi.storage import atomic_write
from .manifest import read_project, confined, compatibility, image_metadata, version_key
from .schema import identifier

MAX_BYTES = 16 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024


def _link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _directory_header(data):
    if not isinstance(data, bytes) or not data or len(data) > MAX_BYTES:
        raise ValidationError("App ZIP must contain at most 16 MiB of bytes")
    marker = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if marker < 0 or marker + 22 > len(data):
        raise ValidationError("Invalid app ZIP directory")
    _, disk, directory_disk, disk_entries, entries, size, offset, comment = struct.unpack_from("<4s4H2LH", data, marker)
    if (disk or directory_disk or disk_entries != entries or entries > 256 or size > 512 * 1024
            or offset == 0xffffffff or offset + size != marker or marker + 22 + comment != len(data)):
        raise ValidationError("App ZIP must be a single-volume non-ZIP64 archive with at most 256 entries")


def _archive_path(name):
    path = name[:-1] if name.endswith("/") else name
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
    if (not path or "\\" in path or ":" in path or any(ord(char) < 32 or ord(char) == 127 for char in path) or len(path.encode("utf-8")) > 1024
            or len(path.split("/")) > 20 or any(part in {"", ".", ".."} or part.endswith((".", " "))
                or part.split(".", 1)[0].upper() in reserved for part in path.split("/"))):
        raise ValidationError("App ZIP contains an unsafe or nonportable path")
    return path


@contextmanager
def _extracted(data):
    _directory_header(data)
    with tempfile.TemporaryDirectory(prefix="robbi-app-inspect-") as directory:
        temporary = Path(directory)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 256 or sum(entry.file_size for entry in entries) > MAX_EXPANDED:
                    raise ValidationError("App ZIP exceeds 256 entries or 32 MiB expanded")
                seen, spellings, files, manifests = set(), {}, [], []
                for entry in entries:
                    relative = _archive_path(entry.filename)
                    target = confined(temporary, relative)
                    parts = relative.split("/")
                    for index in range(1, len(parts) + 1):
                        spelling = "/".join(parts[:index])
                        previous = spellings.setdefault(spelling.casefold(), spelling)
                        if previous != spelling:
                            raise ValidationError("Duplicate or ambiguous case-insensitive archive paths")
                    if relative.casefold() in seen:
                        raise ValidationError("Duplicate archive paths")
                    seen.add(relative.casefold())
                    mode = entry.external_attr >> 16
                    if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                        raise ValidationError("Links and special files are not app project files")
                    if entry.flag_bits & 1 or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                        raise ValidationError("App ZIP must use unencrypted stored or deflated files")
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, target.open("xb") as output:
                        remaining = entry.file_size
                        while block := source.read(min(65536, remaining + 1)):
                            remaining -= len(block)
                            if remaining < 0:
                                raise ValidationError("App ZIP entry exceeds its declared size")
                            output.write(block)
                        if remaining:
                            raise ValidationError("App ZIP entry is truncated")
                    files.append(target)
                    if target.name == "app.yaml":
                        manifests.append(target)
                if len(manifests) != 1:
                    raise ValidationError("App ZIP must contain exactly one app.yaml")
                project = read_project(manifests[0].parent)
                if any(not path.is_relative_to(project.root) for path in files):
                    raise ValidationError("All archive files must belong to the single app project")
        except (zipfile.BadZipFile, EOFError, OSError, RuntimeError, NotImplementedError, UnicodeError) as error:
            raise ValidationError("Invalid or unreadable app ZIP") from error
        yield project, files


def inspect_archive(data, *, include_images=False):
    """Inspect without executing code. include_images adds validated image bytes."""
    with _extracted(data) as (project, files):
        return {"manifest": copy.deepcopy(project.manifest), "sha256": hashlib.sha256(data).hexdigest(),
                "zip_bytes": len(data), "expanded_bytes": sum(path.stat().st_size for path in files),
                "files": sorted(path.relative_to(project.root).as_posix() for path in files),
                "images": image_metadata(project.root, project.manifest["images"], include_data=include_images),
                "compatibility": compatibility(project.manifest)}


class Packages:
    MAX_APPS = 48
    MAX_REMOVED = 48
    MAX_BYTES = MAX_BYTES
    MAX_EXPANDED = MAX_EXPANDED

    def __init__(self, bundled, installed):
        self.bundled, self.installed = Path(bundled), Path(installed)
        self.projects, self.errors = {}, []
        self._lock = threading.RLock()
        self._removed_file = self.installed.parent / "removed-apps.json"
        self._scanned = False

    def _ensure_scanned(self):
        if not self._scanned:
            self.scan()

    def _removed_records(self):
        if not self._removed_file.exists():
            return {}
        try:
            if self._removed_file.stat().st_size > 4 * 1024 * 1024:
                raise ValueError()
            records = json.loads(self._removed_file.read_text(encoding="utf-8"))
            if not isinstance(records, dict) or len(records) > self.MAX_REMOVED:
                raise ValueError()
            for key, record in records.items():
                identifier(key)
                if (not isinstance(record, dict) or set(record) != {"manifest", "source", "retained", "bundled_folder", "removed_at"}
                        or record["source"] not in {"bundled", "installed"} or not isinstance(record["manifest"], dict)
                        or record["manifest"].get("id") != key or not isinstance(record["retained"], str)
                        or (record["retained"] and (len(record["retained"]) != 32 or any(c not in "0123456789abcdef" for c in record["retained"])))
                        or not isinstance(record["bundled_folder"], str) or type(record["removed_at"]) not in {float, int}
                        or not math.isfinite(record["removed_at"]) or not isinstance(record["manifest"].get("permissions"), list)):
                    raise ValueError()
                if record["source"] == "installed" and not record["retained"]:
                    raise ValueError()
                if record["source"] == "bundled":
                    confined(self.bundled, record["bundled_folder"])
            return records
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise ValidationError("Removed-app registry is invalid; restore removed-apps.json from a backup before changing the library") from None

    def _save_removed(self, records):
        encoded = json.dumps(records, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 4 * 1024 * 1024:
            raise ValidationError("Removed-app metadata capacity reached; restore an app first")
        atomic_write(self._removed_file, encoded)

    def _entries(self, base):
        if not base.exists():
            return []
        with os.scandir(base) as entries:
            bounded = []
            for count, entry in enumerate(entries):
                if count >= 128:
                    raise ValidationError("Directory scan limit reached (128 entries)")
                if not entry.name.startswith(".") and entry.is_dir() and not _link(Path(entry.path)):
                    bounded.append(Path(entry.path))
            return sorted(bounded)

    def scan(self):
        with self._lock:
            self._scanned = True
            projects, errors = {}, []
            try:
                removed = self._removed_records()
            except ValidationError as error:
                self.projects, self.errors = {}, [{"id": "catalog", "error": str(error)}]
                return self.projects
            for base, bundled in ((self.bundled, True), (self.installed, False)):
                try:
                    directories = self._entries(base)
                except (OSError, ValueError) as error:
                    errors.append({"id": "catalog", "error": str(error)[:500]})
                    continue
                for path in directories:
                    try:
                        project = read_project(path, bundled=bundled)
                        if project.id in removed:
                            continue
                        if not bundled and project.id != path.name:
                            raise ValidationError("Imported app folder must match its manifest ID")
                        if project.id in projects and (bundled or not projects[project.id].bundled):
                            raise ValidationError("Duplicate app ID")
                        if project.id not in projects and len(projects) >= self.MAX_APPS:
                            raise ValidationError("App limit reached (48 projects)")
                        projects[project.id] = project
                    except (OSError, ValueError, TypeError) as error:
                        errors.append({"id": path.name, "error": str(error)[:500]})
            self.projects, self.errors = projects, errors
            return projects

    def preview(self, data):
        preview = inspect_archive(data)
        with self._lock:
            self._ensure_scanned()
            removed = self._removed_records()
            app_id = preview["manifest"]["id"]
            previous = self.projects[app_id].manifest if app_id in self.projects else removed.get(app_id, {}).get("manifest")
            old_caps = set(previous["permissions"]) if previous else set()
            new_caps = set(preview["manifest"]["permissions"])
            preview.update(operation="update" if app_id in self.projects else "restore" if app_id in removed else "install",
                           previous=copy.deepcopy(previous), permission_changes={"added": sorted(new_caps - old_caps),
                            "removed": sorted(old_caps - new_caps), "unchanged": sorted(old_caps & new_caps)})
        return preview

    def _cleanup(self, path):
        if path.exists() and not _link(path) and path.resolve().is_relative_to(self.installed.resolve()):
            try:
                shutil.rmtree(path)
            except OSError:
                pass  # A locked backup stays hidden; committed changes remain valid.

    def install(self, data):
        with self._lock, _extracted(data) as (project, files):
            self._ensure_scanned()
            records = self._removed_records()
            check = compatibility(project.manifest)
            if not check["compatible"]:
                raise ValidationError("App requirements are not satisfied: " + "; ".join(check["reasons"]))
            old = self.projects.get(project.id)
            if old is None and len(self.projects) >= self.MAX_APPS:
                raise ValidationError("App limit reached; remove a project first")
            if old and version_key(project.manifest["version"]) < version_key(old.manifest["version"]):
                raise ValidationError("An app update cannot downgrade its installed version")
            self.installed.mkdir(parents=True, exist_ok=True)
            raw_destination = self.installed / project.id
            if (raw_destination.exists() or raw_destination.is_symlink()) and _link(raw_destination):
                raise ValidationError("App destination cannot be a link or junction")
            destination = confined(self.installed, project.id)
            staged = self.installed / f".ready-{uuid.uuid4().hex}"
            backup = self.installed / f".previous-{uuid.uuid4().hex}"
            moved_old = placed_new = False
            removed_record = records.get(project.id)
            try:
                shutil.copytree(project.root, staged)
                if destination.exists():
                    destination.rename(backup)
                    moved_old = True
                staged.rename(destination)
                placed_new = True
                if removed_record:
                    updated = dict(records)
                    updated.pop(project.id)
                    self._save_removed(updated)
            except BaseException:
                if placed_new:
                    destination.rename(staged)
                if moved_old:
                    backup.rename(destination)
                raise
            finally:
                self._cleanup(staged)
            self._cleanup(backup)
            if removed_record and removed_record["retained"]:
                self._cleanup(self.installed / ".removed" / removed_record["retained"])
            self.scan()
            return project.id

    def remove(self, app_id):
        identifier(app_id)
        with self._lock:
            self._ensure_scanned()
            project = self.projects.get(app_id)
            if project is None:
                raise ValidationError("Unknown app")
            records = self._removed_records()
            if len(records) >= self.MAX_REMOVED:
                raise ValidationError("Removed-app history is full; restore a project before removing another")
            token = "" if project.bundled else uuid.uuid4().hex
            record = {"manifest": copy.deepcopy(project.manifest), "source": "bundled" if project.bundled else "installed",
                      "retained": token, "bundled_folder": project.root.name if project.bundled else "", "removed_at": time.time()}
            original = retained = None
            if not project.bundled:
                original = confined(self.installed, app_id)
                if _link(self.installed / app_id):
                    raise ValidationError("App project cannot be a link or junction")
                retained = self.installed / ".removed" / token
                retained.parent.mkdir(parents=True, exist_ok=True)
                if _link(retained.parent):
                    raise ValidationError("Removed-app storage cannot be a link or junction")
                original.rename(retained)
            try:
                self._save_removed({**records, app_id: record})
            except BaseException:
                if retained is not None:
                    retained.rename(original)
                raise
            self.scan()
            return {"removed": app_id, "source": record["source"], "restore_possible": True}

    def _restore_path(self, record):
        if record["source"] == "bundled":
            return confined(self.bundled, record["bundled_folder"])
        return confined(self.installed, ".removed/" + record["retained"])

    def removed(self):
        with self._lock:
            return [{"id": app_id, "name": record["manifest"].get("name", app_id), "version": record["manifest"].get("version", ""),
                     "source": record["source"], "manifest": copy.deepcopy(record["manifest"]), "removed_at": record["removed_at"],
                     "restore_possible": self._restore_path(record).is_dir()} for app_id, record in self._removed_records().items()]

    def available_bundled(self):
        with self._lock:
            self._ensure_scanned()
            removed = self._removed_records()
            projects = []
            for path in self._entries(self.bundled):
                try:
                    project = read_project(path, bundled=True)
                    projects.append({**copy.deepcopy(project.manifest), "bundled": True, "removed": project.id in removed,
                                     "installed": project.id in self.projects, "compatibility": compatibility(project.manifest)})
                except (OSError, ValueError, TypeError):
                    continue
            return projects

    def restore(self, app_id):
        identifier(app_id)
        with self._lock:
            self._ensure_scanned()
            records = self._removed_records()
            record = records.get(app_id)
            if record is None:
                raise ValidationError("That app is not in removed-app history")
            if len(self.projects) >= self.MAX_APPS:
                raise ValidationError("App limit reached; remove a project before restoring another")
            source = self._restore_path(record)
            if not source.is_dir() or _link(source):
                raise ValidationError("The retained app files are unavailable")
            project = read_project(source, bundled=record["source"] == "bundled")
            if project.id != app_id:
                raise ValidationError("Retained project identity changed")
            check = compatibility(project.manifest)
            if not check["compatible"]:
                raise ValidationError("App requirements are not satisfied: " + "; ".join(check["reasons"]))
            destination = confined(self.installed, app_id)
            moved = False
            if record["source"] == "installed":
                if destination.exists():
                    raise ValidationError("Another project already occupies the restore destination")
                source.rename(destination)
                moved = True
            updated = dict(records)
            updated.pop(app_id)
            try:
                self._save_removed(updated)
            except BaseException:
                if moved:
                    destination.rename(source)
                raise
            self.scan()
            return app_id
