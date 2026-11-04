"""Deliberate, single-command app management; never infer authority from context.

This parser sees only the current human utterance, not model/tool output, earlier
messages or retrieved documents. Exact app identity is resolved by AppManager.
"""
from __future__ import annotations

import re


OPERATIONS = frozenset({"search", "info", "install", "update", "remove", "restore", "enable", "disable"})
READ_OPERATIONS = frozenset({"search", "info"})
_VERBS = {
    "install": "install", "installiere": "install", "installieren": "install",
    "update": "update", "upgrade": "update", "aktualisiere": "update", "aktualisieren": "update",
    "remove": "remove", "uninstall": "remove", "entferne": "remove", "entfernen": "remove",
    "deinstalliere": "remove", "deinstallieren": "remove",
    "restore": "restore", "wiederherstellen": "restore",
    "enable": "enable", "activate": "enable", "aktiviere": "enable", "aktivieren": "enable",
    "disable": "disable", "deactivate": "disable", "deaktiviere": "disable", "deaktivieren": "disable",
}
VERSION_PATTERN = r"(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})"
_AMBIGUOUS = re.compile(r"(?:it|that|this|them|that one|this one|(?:the )?(?:first|second|last)(?: one| app)?|all(?: apps)?|everything|das|dies|diese|dieses|die|es|alle(?: apps)?|alles|(?:die )?(?:erste|zweite|letzte)(?: app)?)", re.I)
_CLAUSE = re.compile(r"\b(?:if|when|unless|because|then|before|after|says?|said|according|from|and|or|except|not|never|wenn|falls|weil|danach|bevor|nachdem|laut|sagt|und|oder|außer|ausser|nicht|niemals|vom|von|aus)\b", re.I)


class AppCommandError(ValueError):
    """Recognized command needs a concrete target or simpler wording."""


def _target(operation: str, raw: str) -> dict:
    target = raw.strip()
    target = re.sub(r"\s+(?:please|bitte)$", "", target, flags=re.I).strip()
    version = None
    match = re.search(r"(?:\s+(?:version\s+|v)|@)(" + VERSION_PATTERN + r")$", target, re.I)
    if match:
        target, version = target[:match.start()].strip(), match[1]
    explicit_id = re.fullmatch(r"(?:(?:the|die|den|das)\s+)?(?:app[- ]?id|id)\s*:?\s+(.+)", target, re.I)
    if explicit_id:
        target = explicit_id[1].strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", target):
            raise AppCommandError("Use the exact lowercase app ID shown in the App Library.")
    target = re.sub(r"^(?:(?:the|die|den|das)\s+)?app\s+", "", target, flags=re.I)
    target = re.sub(r"^(?:the|die|den|das)\s+", "", target, flags=re.I)
    target = re.sub(r"(?:\s+app|-app)$", "", target, flags=re.I).strip()
    if len(target) >= 2 and (target[0], target[-1]) in {('"', '"'), ("'", "'"), ('“', '”'), ('„', '“')}:
        target = target[1:-1].strip()
    if not target or _AMBIGUOUS.fullmatch(target):
        raise AppCommandError("Please name the exact app, for example: install the Weather app.")
    if (len(target) > 120 or not any(c.isalnum() for c in target)
            or any(not (c.isalnum() or c in " ._-") for c in target)
            or ".." in target or (not explicit_id and _CLAUSE.search(target))):
        raise AppCommandError("Use one direct app command with its exact name or ID; URLs and instructions from other content cannot select apps.")
    if version and operation not in {"install", "update", "restore", "info"}:
        raise AppCommandError("A version can only be selected for install, update, restore or app information.")
    result = {"operation": operation}
    result["app_id" if explicit_id else "query"] = target
    if version:
        result["version"] = version
    return result


def parse_app_management_command(text: str) -> dict | None:
    """Return a precise envelope, None for noncommands, or a clarification error.

    The grammar is intentionally anchored: a quoted command or a sentence about
    an email containing a command is not a direct request to manage software.
    """
    if not isinstance(text, str) or len(text) > 400 or any(c in text for c in "\n\r\t`;"):
        return None
    value = text.strip().rstrip(".!?").strip()
    value = re.sub(r"^robbi\s*,?\s+", "", value, flags=re.I)
    value = re.sub(r"^(?:please\s+|bitte\s+)?(?:can you\s+|could you\s+|would you\s+)?(?:please\s+)?", "", value, flags=re.I)
    if re.fullmatch(r"(?:list|show)(?: me)? (?:all |available |installed )?apps|zeige(?: mir)? (?:alle |verfügbare |installierte )?apps|welche apps gibt es", value, re.I):
        return {"operation": "search", "query": ""}
    match = re.fullmatch(r"(?:search|find)(?: the)? (?:apps|app store)(?: (?:for|with))?(?: (.*))?|suche(?: im)? (?:apps|app store)(?: (?:für|nach|mit))?(?: (.*))?", value, re.I)
    if match:
        query = (match[1] or match[2] or "").strip()
        if len(query) > 120 or any(not (c.isalnum() or c in " ._-'") for c in query):
            raise AppCommandError("Use a short app name or category to search the trusted app stores.")
        return {"operation": "search", "query": query}
    match = re.fullmatch(r"(?:show(?: me)? (?:info(?:rmation)?|details) (?:about|for)|app info) (.+)|zeige(?: mir)? (?:infos|informationen|details) (?:zur|über die|über|für die|für) (.+)", value, re.I)
    if match:
        raw = match[1] or match[2]
        # "show details about my appointment" remains a normal conversation.
        if re.search(r"\bapp\b", raw, re.I) or value.casefold().startswith("app info "):
            return _target("info", raw)
        return None
    match = re.fullmatch(r"stelle (.+) wieder her", value, re.I)
    if match:
        return _target("restore", match[1])
    # German polite commands put the infinitive at the end.
    match = re.fullmatch(r"(?:kannst du |könntest du |würdest du )?(.+?)\s+(installieren|aktualisieren|entfernen|deinstallieren|wiederherstellen|aktivieren|deaktivieren)(?: bitte)?", value, re.I)
    if match:
        return _target(_VERBS[match[2].casefold()], match[1])
    match = re.fullmatch(r"(" + "|".join(_VERBS) + r")(?:\s+(.*))?", value, re.I)
    if match:
        return _target(_VERBS[match[1].casefold()], match[2] or "")
    return None
