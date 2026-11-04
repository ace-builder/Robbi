from __future__ import annotations

import io
import json
import logging
import shutil
import tempfile
import zipfile
import re
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml

from robbi.configuration.settings import APPS_DIR


_log = logging.getLogger(__name__)
APP_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_FILES = 512


def confined(root: Path, relative: str) -> Path:
    text = str(relative)
    if not text or "\\" in text or ":" in text or text.startswith("/"):
        raise ValueError("App paths must be relative POSIX paths")
    parts = Path(text).parts
    if any(part in {"..", "."} for part in parts):
        raise ValueError("App path traversal is not allowed")
    candidate = (root / text).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError("App path must remain inside the package")
    return candidate


def validated_id(value: str) -> str:
    if not isinstance(value, str) or not APP_ID.fullmatch(value):
        raise ValueError("App id must start with a lowercase letter and contain only letters, digits, _ or -")
    return value


@dataclass(slots=True)
class AppPackage:
    app_id: str
    name: str
    version: str
    description: str
    root: Path
    prompt_text: str
    workflow: dict
    triggers: list[str]
    permissions: list[str]
    latency_class: str
    may_run_proactively: bool
    requires_confirmation: bool
    cooldown_s: float
    tools: list[str]
    assets: dict[str, dict[str, Path]]

    def summary(self) -> dict:
        return {
            "id": self.app_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "triggers": list(self.triggers),
            "permissions": list(self.permissions),
            "latency_class": self.latency_class,
            "may_run_proactively": self.may_run_proactively,
            "requires_confirmation": self.requires_confirmation,
            "cooldown_s": self.cooldown_s,
            "tools": list(self.tools),
            "assets": {category: sorted(files.keys()) for category, files in self.assets.items()},
            "has_prompt": bool(self.prompt_text.strip()),
            "has_workflow": bool(self.workflow),
            "root": str(self.root),
        }

    def prompt_context(self) -> str:
        lines = [
            f"App id: {self.app_id}",
            f"App name: {self.name}",
            f"App description: {self.description or 'none'}",
        ]
        if self.triggers:
            lines.append("Suggested triggers: " + ", ".join(self.triggers))
        if self.permissions or self.tools:
            lines.append(
                "Metadata: "
                + json.dumps(
                    {
                        "permissions": self.permissions,
                        "latency_class": self.latency_class,
                        "may_run_proactively": self.may_run_proactively,
                        "requires_confirmation": self.requires_confirmation,
                        "tools": self.tools,
                    },
                    ensure_ascii=False,
                )
            )
        if self.prompt_text.strip():
            lines.append("App-specific instructions:")
            lines.append(self.prompt_text.strip())
        if self.assets:
            lines.append(
                "App assets: "
                + json.dumps({category: sorted(files.keys()) for category, files in self.assets.items()}, ensure_ascii=False)
            )
        return "\n".join(lines).strip()

    def resolve_asset(self, category: str, name: str, extensions: tuple[str, ...] = ()) -> Path | None:
        values = self.assets.get(category, {})
        if name in values:
            return values[name]
        for extension in extensions:
            candidate = name if name.endswith(extension) else f"{name}{extension}"
            if candidate in values:
                return values[candidate]
        return None


class AppRegistry:
    def __init__(self, *, bundled_root=None) -> None:
        self._lock = threading.RLock()
        self._bundled_root = Path(bundled_root) if bundled_root is not None else None
        self._apps: dict[str, AppPackage] = {}
        self.errors = []
        self.scan()

    def scan(self) -> None:
        found = {}
        errors = []
        APPS_DIR.mkdir(parents=True, exist_ok=True)
        bundled = (sorted(path for path in self._bundled_root.iterdir() if path.is_dir())
                   if self._bundled_root is not None and self._bundled_root.is_dir() else [])
        for root in bundled + sorted(path for path in APPS_DIR.iterdir() if path.is_dir()):
            if root.name.startswith('.') or root.is_symlink():
                continue
            try:
                app = self._load_app(root)
            except Exception as exc:
                _log.warning("Skipping app %s because its manifest could not be loaded: %s", root.name, exc)
                errors.append({'id': root.name, 'error': str(exc)})
                continue
            if app is not None:
                found[app.app_id] = app
        with self._lock:
            self._apps = found
            self.errors = errors

    def status(self):
        with self._lock:
            return {'available': True, 'count': len(self._apps), 'errors': list(self.errors),
                    'last_error': '; '.join(f"{item['id']}: {item['error']}" for item in self.errors)}

    def list_apps(self) -> list[dict]:
        return [self._apps[app_id].summary() for app_id in sorted(self._apps)]

    def prompt_catalog(self) -> str:
        if not self._apps:
            return "Available apps: none."
        lines = ["Available apps:"]
        for app_id in sorted(self._apps):
            app = self._apps[app_id]
            trigger_text = f" triggers={', '.join(app.triggers)}" if app.triggers else ""
            proactive = " proactive" if app.may_run_proactively else ""
            lines.append(f"- {app.app_id}: {app.name} | {app.description}{trigger_text} latency={app.latency_class}{proactive}")
        return "\n".join(lines)

    def get(self, app_id: str) -> AppPackage | None:
        return self._apps.get(str(app_id or "").strip())

    def install_zip(self, filename: str, data: bytes) -> dict:
        if len(data) > MAX_ARCHIVE_BYTES:
            raise ValueError("App archive exceeds 32 MiB")
        APPS_DIR.mkdir(parents=True, exist_ok=True)
        with self._lock, tempfile.TemporaryDirectory(prefix=".install-", dir=APPS_DIR) as temp_dir:
            temp_root = Path(temp_dir)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if len(members) > MAX_FILES or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
                    raise ValueError("App archive exceeds file count or expanded-size limit")
                seen = set()
                for item in members:
                    target = confined(temp_root, item.filename.rstrip('/'))
                    if target in seen:
                        raise ValueError("Duplicate app archive path")
                    seen.add(target)
                    mode = item.external_attr >> 16
                    if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                        raise ValueError("App archive contains a link or special file")
                    if item.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item) as source, target.open('wb') as output:
                        remaining = item.file_size
                        while chunk := source.read(min(65536, remaining + 1)):
                            remaining -= len(chunk)
                            if remaining < 0:
                                raise ValueError("Archive member exceeds declared size")
                            output.write(chunk)
            candidate_root = self._find_app_root(temp_root)
            app = self._load_app(candidate_root)
            if app is None:
                raise ValueError("missing valid app.yaml in uploaded zip")
            target_root = confined(APPS_DIR, validated_id(app.app_id))
            if (APPS_DIR / app.app_id).is_symlink():
                raise ValueError('App installation target cannot be a symbolic link')
            backup = APPS_DIR / f'.backup-{uuid.uuid4().hex}'
            staged = APPS_DIR / f'.ready-{uuid.uuid4().hex}'
            shutil.copytree(candidate_root, staged)
            had_previous = target_root.exists()
            try:
                if had_previous:
                    target_root.rename(backup)
                staged.rename(target_root)
            except Exception:
                if had_previous and backup.exists() and not target_root.exists():
                    backup.rename(target_root)
                raise
            finally:
                if staged.exists():
                    shutil.rmtree(staged)
            if backup.exists():
                shutil.rmtree(backup)
        self.scan()
        installed = self.get(app.app_id)
        return {
            "installed": installed.summary() if installed else {"id": app.app_id},
            "apps": self.list_apps(),
            "filename": filename,
        }

    def remove(self, app_id: str) -> dict:
        app_id = validated_id(app_id)
        if (APPS_DIR / app_id).is_symlink():
            raise ValueError('Cannot remove an app through a symbolic link')
        target = confined(APPS_DIR, app_id)
        if not target.exists():
            raise FileNotFoundError(app_id)
        with self._lock:
            shutil.rmtree(target)
            self.scan()
        return {"removed": str(app_id), "apps": self.list_apps()}

    def _find_app_root(self, temp_root: Path) -> Path:
        manifests = list(temp_root.rglob('app.yaml'))
        if len(manifests) != 1:
            raise ValueError("An app archive must contain exactly one app.yaml")
        return manifests[0].parent

    def _load_app(self, root: Path) -> AppPackage | None:
        manifest_file = root / "app.yaml"
        if not manifest_file.exists():
            return None
        try:
            if manifest_file.stat().st_size > 65536:
                raise ValueError('App manifest exceeds 64 KiB')
            manifest = yaml.safe_load(manifest_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid app.yaml: {exc}") from exc
        if not isinstance(manifest, dict):
            return None
        app_id = validated_id(str(manifest.get("id") or root.name).strip())
        prompt_text = self._load_prompt_text(root, manifest)
        workflow = self._load_workflow(root, manifest)
        triggers = [str(item).strip() for item in manifest.get("triggers") or [] if str(item).strip()]
        permissions = [str(item).strip() for item in manifest.get("permissions") or [] if str(item).strip()]
        tools = [str(item).strip() for item in manifest.get("tools") or [] if str(item).strip()]
        assets = self._load_assets(root, manifest)
        return AppPackage(
            app_id=app_id,
            name=str(manifest.get("name") or app_id).strip(),
            version=str(manifest.get("version") or "0.1.0").strip(),
            description=str(manifest.get("description") or "").strip(),
            root=root,
            prompt_text=prompt_text,
            workflow=workflow,
            triggers=triggers,
            permissions=permissions,
            latency_class=str(manifest.get("latency_class") or "fast").strip(),
            may_run_proactively=bool(manifest.get("may_run_proactively", False)),
            requires_confirmation=bool(manifest.get("requires_confirmation", False)),
            cooldown_s=float(manifest.get("cooldown_s") or 0.0),
            tools=tools,
            assets=assets,
        )

    def _load_prompt_text(self, root: Path, manifest: dict) -> str:
        inline = str(manifest.get("prompt") or "").strip()
        if inline:
            return inline
        prompt_file = str(manifest.get("prompt_file") or "").strip()
        if not prompt_file:
            return ""
        path = confined(root, prompt_file)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def _load_workflow(self, root: Path, manifest: dict) -> dict:
        inline_action = manifest.get("action")
        inline_sequence = manifest.get("sequence")
        if isinstance(inline_action, dict):
            return inline_action
        if isinstance(inline_sequence, list):
            return {"sequence": inline_sequence}
        workflow_file = str(manifest.get("workflow_file") or "").strip()
        if not workflow_file:
            return {}
        path = confined(root, workflow_file)
        if not path.exists():
            return {}
        if path.suffix.lower() == ".json":
            loaded = json.loads(path.read_text(encoding="utf-8"))
        else:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return loaded if isinstance(loaded, dict) else {}

    def _load_assets(self, root: Path, manifest: dict) -> dict[str, dict[str, Path]]:
        assets: dict[str, dict[str, Path]] = {}
        declared = manifest.get("assets")
        if isinstance(declared, dict):
            sources = {str(category): confined(root, str(relative)) for category, relative in declared.items()}
        else:
            sources = {
                "faces": root / "assets" / "faces",
                "sounds": root / "assets" / "sounds",
                "videos": root / "assets" / "videos",
                "animations": root / "assets" / "animations",
                "filming_sequences": root / "assets" / "filming_sequences",
            }
        for category, directory in sources.items():
            if not directory.exists():
                continue
            assets[category] = {}
            for path in sorted(directory.glob("*")):
                if path.is_file():
                    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                        raise ValueError("App asset escapes package")
                    assets[category][path.name] = path
        return assets
