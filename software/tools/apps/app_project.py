"""Create, inspect and package an API v2 app without importing its Python code."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import sys
import zipfile

# Also works as `python tools/apps/app_project.py` before an editable installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from robbi.app_runtime.manifest import confined, read_project
from robbi.app_runtime.packages import inspect_archive
from robbi.app_runtime.schema import identifier
from robbi.errors import ValidationError

MAX_FILES = 256
MAX_SCANNED = 4096
MAX_ZIP = 16 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
_EXCLUDED_DIRECTORIES = {".venv", "venv", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                         ".ruff_cache", "node_modules", ".secrets", "secrets", "credentials", "private_keys",
                         ".ssh", ".aws", ".gnupg"}
_SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".p8", ".ppk", ".keystore", ".jks"}
_SECRET_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "authorized_keys", "known_hosts",
                 "credentials", "token", "tokens", "key", "keys", "api_key", "api_keys", "apikey", "private_key",
                 "password", "passwords", "service-account", "service_account", "service-account-key",
                 "service_account_key", ".netrc", ".npmrc", ".pypirc"}


@dataclass(frozen=True)
class ProjectFiles:
    project: object
    files: tuple[tuple[Path, int], ...]
    excluded: tuple[str, ...]
    expanded_bytes: int


def _link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) &
                                            getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _excluded(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    name = parts[-1]
    if any(part in _EXCLUDED_DIRECTORIES or part.startswith(".env") for part in parts):
        return True
    if parts == ["config", "local.yaml"] or parts == ["config", "local.yml"]:
        return True
    stem = Path(name).stem
    return (Path(name).suffix in _SECRET_SUFFIXES or name.endswith((".pyc", ".pyo"))
            or stem in _SECRET_NAMES or "secret" in stem or "credential" in stem
            or name.startswith(("id_rsa.", "id_dsa.", "id_ecdsa.", "id_ed25519.")))


def inspect_project(path, *, output=None) -> ProjectFiles:
    """Validate metadata and a bounded publish inventory; never import app code."""
    original = Path(path).absolute()
    if _link(original):
        raise ValidationError("App project root must not be a link or junction")
    root = original.resolve(strict=True)
    if not root.is_dir():
        raise ValidationError("App project path must be a directory")
    excluded_output = Path(output).absolute() if output is not None else None
    files, excluded = [], []
    scanned = expanded = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                scanned += 1
                if scanned > MAX_SCANNED:
                    raise ValidationError("App project directory scan exceeds 4096 entries")
                candidate = Path(entry.path)
                relative = candidate.relative_to(root)
                if len(relative.as_posix().encode("utf-8")) > 1024:
                    raise ValidationError("App project path exceeds 1024 UTF-8 bytes")
                # Reject links even when their name would normally be excluded.
                if _link(candidate):
                    raise ValidationError(f"Links and junctions cannot be packaged: {relative.as_posix()}")
                confined(root, relative.as_posix())
                if candidate.absolute() == excluded_output or _excluded(relative):
                    excluded.append(relative.as_posix())
                    continue
                info = candidate.stat()
                if stat.S_ISDIR(info.st_mode):
                    pending.append(candidate)
                elif stat.S_ISREG(info.st_mode):
                    expanded += info.st_size
                    if expanded > MAX_EXPANDED:
                        raise ValidationError("App project exceeds 32 MiB expanded")
                    files.append((candidate, info.st_size))
                    if len(files) > MAX_FILES:
                        raise ValidationError("App project exceeds 256 publishable files")
                else:
                    raise ValidationError(f"Special files cannot be packaged: {relative.as_posix()}")
    project = read_project(root)
    included = {item.relative_to(root).as_posix() for item, _ in files}
    required = {"app.yaml", project.manifest["entrypoint"].split(":")[0].replace(".", "/") + ".py"}
    if project.manifest["dashboard"].get("page"):
        required.add(project.manifest["dashboard"]["page"])
    required.update(project.manifest["images"].values())
    if not required <= included:
        raise ValidationError("A required app manifest, entrypoint or dashboard file was excluded; rename it")
    manifests = [name for name in included if Path(name).name == "app.yaml"]
    if len(manifests) != 1:
        raise ValidationError("An app project must contain exactly one app.yaml")
    return ProjectFiles(project, tuple(sorted(files)), tuple(sorted(excluded)), expanded)


class _LimitedOutput:
    def __init__(self, stream, limit):
        self.stream, self.limit = stream, limit

    def write(self, data):
        if self.stream.tell() + len(data) > self.limit:
            raise ValidationError("Packed app ZIP exceeds 16 MiB")
        return self.stream.write(data)

    def __getattr__(self, name):
        return getattr(self.stream, name)


def pack_project(path, output) -> dict:
    destination = Path(output).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValidationError("Output already exists; choose another ZIP filename")
    inventory = inspect_project(path, output=destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also closes the race after the existence check. There
    # is deliberately no --force switch that might overwrite project source.
    created = False
    try:
        with destination.open("xb") as stream:
            created = True
            with zipfile.ZipFile(_LimitedOutput(stream, MAX_ZIP), "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6, allowZip64=False) as archive:
                for path, expected_size in inventory.files:
                    if _link(path):
                        raise ValidationError("Project changed during packaging; try again with files closed")
                    relative = path.relative_to(inventory.project.root).as_posix()
                    confined(inventory.project.root, relative)
                    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                    with os.fdopen(os.open(path, flags), "rb") as source:
                        info = os.fstat(source.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
                            raise ValidationError("Project changed during packaging; try again with files closed")
                        metadata = zipfile.ZipInfo(relative)
                        metadata.compress_type = zipfile.ZIP_DEFLATED
                        metadata.external_attr = (stat.S_IFREG | 0o644) << 16
                        with archive.open(metadata, "w") as target:
                            remaining = expected_size
                            while block := source.read(min(65536, remaining + 1)):
                                remaining -= len(block)
                                if remaining < 0:
                                    raise ValidationError("Project file grew during packaging")
                                target.write(block)
                            if remaining:
                                raise ValidationError("Project file shrank during packaging")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return {"id": inventory.project.id, "output": str(destination), "files": len(inventory.files),
            "zip_bytes": destination.stat().st_size, "expanded_bytes": inventory.expanded_bytes,
            "excluded": list(inventory.excluded)}


def new_project(path, app_id, *, author="Unknown") -> dict:
    identifier(app_id)
    destination = Path(path).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValidationError("Project path already exists; choose a new directory")
    # Every file is opened exclusively; existing projects are never modified.
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {
        "api_version": 2, "id": app_id, "name": app_id.replace("-", " ").replace("_", " ").title(),
        "version": "1.0.0", "description": "A small standalone Robbi app.", "entrypoint": "main:run",
        "author": {"name": author}, "category": "other", "tags": [], "homepage": "", "license": "", "images": {},
        "requires": {"sdk": 2, "runtime": ">=2.0.0,<3.0.0", "python": ">=3.11,<3.13", "dependencies": []},
        "permissions": [], "triggers": [f"Say hello using {app_id}", "Give me a friendly greeting"],
        "tools": [{"name": "hello", "title": "Say hello", "description": "Return a friendly greeting for the supplied name.",
                   "timeout_s": 5, "examples": ["Say hello to a friend"],
                   "input_schema": {"type": "object", "properties": {"name": {"type": "string", "title": "Name", "maxLength": 100, "default": "friend"}}}}],
        "config_schema": {"type": "object", "properties": {"greeting": {"type": "string", "title": "Greeting", "maxLength": 100, "default": "Hello"}}},
    }
    import yaml
    content = {
        "app.yaml": yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        "main.py": '''"""Host passes the SDK context; importing this app has no side effects."""
from .helpers import greeting


def run(context, tool_name, arguments):
    context.checkpoint()
    return {"ok": True, "text": greeting(context.config["greeting"], arguments["name"])}
''',
        "helpers.py": '''"""Keep app logic in ordinary, independently testable modules."""


def greeting(prefix, name):
    return f"{prefix}, {name}!"
''',
        "README.md": f"# {manifest['name']}\n\nEdit app.yaml to describe tools and settings; edit main.py and helpers.py for behavior.\n\nUse relative imports between app modules. Do not store API keys in this project.\n\nFrom the Robbi v2 checkout, run `python tools/apps/app_project.py check PATH` and\n`python tools/apps/app_project.py pack PATH --output {app_id}.zip`. Import the ZIP in\nthe App library and enable it after reviewing its code. See docs/APP_DEVELOPMENT.md.\n",
    }
    for name, body in content.items():
        with (destination / name).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(body)
    return {"id": app_id, "path": str(destination), "files": len(content)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Validate manifest and publish inventory without running app code")
    check.add_argument("path", type=Path)
    pack = commands.add_parser("pack", help="Create a bounded ZIP, excluding local secrets and caches")
    pack.add_argument("path", type=Path)
    pack.add_argument("--output", type=Path, required=True)
    new = commands.add_parser("new", help="Create a minimal app project in a new directory")
    new.add_argument("path", type=Path)
    new.add_argument("--id", required=True)
    new.add_argument("--author", default="Unknown", help="Author or organization shown in app details")
    preview = commands.add_parser("preview", help="Inspect an app ZIP and metadata without installing or running it")
    preview.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "new":
            result = new_project(args.path, args.id, author=args.author)
        elif args.command == "pack":
            result = pack_project(args.path, args.output)
        elif args.command == "preview":
            with args.path.open("rb") as stream:
                result = inspect_archive(stream.read(MAX_ZIP + 1))
        else:
            inventory = inspect_project(args.path)
            result = {"id": inventory.project.id, "valid": True, "files": len(inventory.files),
                      "expanded_bytes": inventory.expanded_bytes, "excluded": list(inventory.excluded)}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        print(f"App project: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
