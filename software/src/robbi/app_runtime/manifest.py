"""Manifest-only discovery, separate from importing any app code."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version, InvalidVersion

from robbi.errors import ValidationError
from robbi import __version__
from .schema import identifier, bounded_json, check_schema

PERMISSIONS = {"network", "state", "apps.read", "account.gmail", "account.calendar", "robot.microphone", "robot.status", "robot.speak", "robot.display", "robot.camera", "robot.motion",
               "robot.tof", "robot.imu", "robot.audio", "robot.assets", "robot.reminders"}
EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
SDK_VERSION = 2
IMAGE_SIZES = {"icon": (1024, 1024), "screen": (280, 240)}


def version_key(value):
    """Release ordering shared with the app store; never compare version text."""
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})", value):
        raise ValidationError("App versions must be numeric major.minor.patch releases, such as 1.2.0")
    return tuple(int(part) for part in value.split("."))


def version_spec(value):
    if not isinstance(value, str) or len(value) > 160:
        raise ValidationError("Version requirements must be bounded text")
    if not value:
        return ""
    for rule in value.split(","):
        if not re.fullmatch(r"\s*(?:==|>=|<=|>|<)(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){0,2}\s*", rule):
            raise ValidationError("Use comma-separated numeric version rules, such as >=2.0.0,<3.0.0")
    return ",".join(rule.strip() for rule in value.split(","))


def _satisfies(actual, spec):
    try:
        return Version(actual) in SpecifierSet(spec)
    except InvalidVersion:
        return False


def compatibility(manifest):
    requires = manifest.get("requires", {})
    reasons = []
    if requires.get("sdk", SDK_VERSION) != SDK_VERSION:
        reasons.append(f"Requires app SDK {requires['sdk']}; this runtime supports {SDK_VERSION}")
    for label, installed, spec in (("Robbi runtime", __version__, requires.get("runtime", "")),
                                    ("Python", ".".join(map(str, sys.version_info[:3])), requires.get("python", ""))):
        if spec and not _satisfies(installed, spec):
            reasons.append(f"Requires {label} {spec}; installed {installed}")
    for dependency in requires.get("dependencies", []):
        try:
            installed = metadata.version(dependency["name"])
        except metadata.PackageNotFoundError:
            reasons.append(f"Missing Python dependency: {dependency['name']} {dependency['version']}".strip())
            continue
        if dependency["version"] and not _satisfies(installed, dependency["version"]):
            reasons.append(f"Requires {dependency['name']} {dependency['version']}; installed {installed}")
    return {"compatible": not reasons, "reasons": reasons}


def url(value, name):
    value = text(value, name, 500)
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or any(character.isspace() or ord(character) < 32 for character in value)):
            raise ValueError()
        parsed.port
    except ValueError:
        raise ValidationError(f"{name} must be an HTTP(S) URL without embedded credentials") from None
    return value


def image_metadata(root, images, *, include_data=False):
    from PIL import Image, UnidentifiedImageError
    result = {}
    for kind, relative in images.items():
        path = confined(root, relative)
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ValidationError(f"App {kind} image must be a local file of at most 4 MiB")
        try:
            with Image.open(path) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"} or image.size != IMAGE_SIZES[kind] or getattr(image, "n_frames", 1) != 1:
                    raise ValidationError(f"App {kind} image must be a static PNG/JPEG/WebP of {IMAGE_SIZES[kind][0]}×{IMAGE_SIZES[kind][1]} pixels")
                mime = Image.MIME[image.format]
                image.verify()
            with Image.open(path) as image:
                image.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as error:
            raise ValidationError(f"App {kind} image is not a valid image") from error
        result[kind] = {"path": relative, "mime": mime, "width": IMAGE_SIZES[kind][0], "height": IMAGE_SIZES[kind][1]}
        if include_data:
            result[kind]["data"] = path.read_bytes()
    return result


def project_metadata(root, value):
    author = value.get("author", {"name": "Unknown"})
    if not isinstance(author, dict) or set(author) - {"name", "url"}:
        raise ValidationError("Author must contain a name and optional URL")
    author = {"name": text(author.get("name", ""), "author.name", 120, required=True), "url": url(author.get("url", ""), "author.url")}
    images = value.get("images", {})
    if not isinstance(images, dict) or set(images) - IMAGE_SIZES.keys():
        raise ValidationError("Images may contain local icon and screen paths")
    images = {kind: text(relative, f"images.{kind}", 500, required=True) for kind, relative in images.items()}
    image_metadata(root, images)
    category = text(value.get("category", "other"), "category", 40, required=True)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", category):
        raise ValidationError("Category must be a lowercase name using letters, numbers and hyphens")
    tags = strings(value.get("tags", []), "tags", 12)
    if any(len(tag) > 40 for tag in tags) or len({tag.casefold() for tag in tags}) != len(tags):
        raise ValidationError("Tags must be distinct and at most 40 characters each")
    requires = value.get("requires", {})
    if not isinstance(requires, dict) or set(requires) - {"sdk", "runtime", "python", "dependencies"}:
        raise ValidationError("Requires supports sdk, runtime, python and declared dependencies")
    sdk = requires.get("sdk", SDK_VERSION)
    if type(sdk) is not int or not 1 <= sdk <= 100:
        raise ValidationError("Required SDK must be an integer version")
    dependencies = requires.get("dependencies", [])
    if not isinstance(dependencies, list) or len(dependencies) > 20:
        raise ValidationError("Declare at most 20 Python dependencies")
    cleaned, seen = [], set()
    for dependency in dependencies:
        if not isinstance(dependency, dict) or set(dependency) - {"name", "version"}:
            raise ValidationError("Dependencies need a distribution name and optional numeric version rule")
        name = dependency.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name):
            raise ValidationError("Invalid Python dependency name")
        key = re.sub(r"[-_.]+", "-", name).casefold()
        if key in seen:
            raise ValidationError("Duplicate dependency name")
        seen.add(key)
        cleaned.append({"name": name, "version": version_spec(dependency.get("version", ""))})
    return {"author": author, "images": images, "category": category, "tags": tags,
            "homepage": url(value.get("homepage", ""), "homepage"), "license": text(value.get("license", ""), "license", 80),
            "requires": {"sdk": sdk, "runtime": version_spec(requires.get("runtime", "")),
                         "python": version_spec(requires.get("python", "")), "dependencies": cleaned}}


def confined(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValidationError("App paths must be relative POSIX paths")
    path = Path(relative)
    if path.is_absolute() or any(part in {"..", "."} for part in path.parts):
        raise ValidationError("App path escapes its project")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValidationError("App path escapes its project")
    return resolved


def text(value, name, maximum, *, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValidationError(f"{name} must be text of at most {maximum} characters")
    return value.strip()


def strings(value, name, maximum=12):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValidationError(f"{name} must be a list of at most {maximum} strings")
    return [text(item, name, 250, required=True) for item in value]


@dataclass(frozen=True)
class Project:
    root: Path
    manifest: dict
    bundled: bool

    @property
    def id(self):
        return self.manifest["id"]


def read_project(root, *, bundled=False):
    root = Path(root).resolve()
    path = confined(root, "app.yaml")
    if path.stat().st_size > 65536:
        raise ValidationError("App manifest exceeds 64 KiB")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        value = bounded_json(value)
    except (yaml.YAMLError, RecursionError):
        raise ValidationError("Invalid or recursive app manifest") from None
    if not isinstance(value, dict) or value.get("api_version") != 2:
        raise ValidationError("Standalone app projects require api_version: 2")
    app_id = identifier(value.get("id"))
    version = text(value.get("version", "1.0.0"), "version", 50)
    if re.fullmatch(r"(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){0,1}", version):
        version += ".0" * (3 - len(version.split(".")))
    version_key(version)
    metadata_fields = project_metadata(root, value)
    entrypoint = value.get("entrypoint", "main:run")
    if not isinstance(entrypoint, str) or not re.fullmatch(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*:[a-zA-Z_]\w*", entrypoint):
        raise ValidationError("Entrypoint must be module:function")
    module = entrypoint.split(":")[0]
    if not confined(root, module.replace(".", "/") + ".py").is_file():
        raise ValidationError("App entrypoint file is missing")
    permissions = strings(value.get("permissions", []), "permissions")
    if set(permissions) - PERMISSIONS:
        raise ValidationError("App declares unknown robot permissions")
    tools = value.get("tools")
    if not isinstance(tools, list) or not 1 <= len(tools) <= 12:
        raise ValidationError("App needs 1 to 12 tools")
    seen, cleaned_tools = set(), []
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValidationError("Tools must be objects")
        name = identifier(tool.get("name"))
        if name in seen:
            raise ValidationError("Duplicate tool name")
        seen.add(name)
        timeout = tool.get("timeout_s", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 180:
            raise ValidationError("Tool timeout must be 1 to 180 seconds")
        schema = check_schema(tool.get("input_schema", EMPTY_SCHEMA))
        if type(tool.get("read_only", False)) is not bool:
            raise ValidationError("Tool read_only must be true or false")
        if schema["type"] != "object":
            raise ValidationError("Tool input must have an object schema")
        cleaned_tools.append({"name": name, "title": text(tool.get("title", name), "tool title", 120),
                              "description": text(tool.get("description", ""), "tool description", 1200, required=True),
                              "input_schema": schema, "examples": strings(tool.get("examples", []), "examples", 8),
                              "timeout_s": timeout, "read_only": tool.get("read_only", False)})
    config_schema = check_schema(value.get("config_schema", EMPTY_SCHEMA))
    if config_schema["type"] != "object":
        raise ValidationError("App configuration requires an object schema")
    dashboard = value.get("dashboard", {})
    if not isinstance(dashboard, dict):
        raise ValidationError("Dashboard must be an object")
    dashboard = {key: text(val, f"dashboard.{key}", 1000) for key, val in dashboard.items() if key in {"title", "description", "page"}}
    if dashboard.get("page"):
        page = confined(root, dashboard["page"])
        if page.suffix.lower() != ".html" or not page.is_file() or page.stat().st_size > 256000:
            raise ValidationError("Custom dashboard needs a local HTML file of at most 256 KB")
    return Project(root, {"api_version": 2, "id": app_id,
                         "name": text(value.get("name", app_id), "name", 120, required=True),
                         "version": version,
                         "description": text(value.get("description", ""), "description", 2000),
                         "entrypoint": entrypoint, "permissions": permissions,
                         "triggers": strings(value.get("triggers", []), "triggers"),
                         "tools": cleaned_tools, "config_schema": config_schema, "dashboard": dashboard, **metadata_fields}, bundled)
