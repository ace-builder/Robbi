"""Compatibility voice routing for installed games; execution lives in app projects."""

import re

def _clean(value):
    return " ".join(re.sub(r"[^\w\s]", " ", str(value).casefold()).split())

def creative_game_request(host, text):
    """Narrow voice shortcut: explicit app names only, never shared active state."""
    text = _clean(text)
    text = re.sub(r"^(?:please )?(?:play |start |lets play |let us play )", "", text)
    names = {"rhythm relay": "rhythm-relay", "tiny detective": "tiny-detective", "robot charades": "robot-charades"}
    for name, app in names.items():
        if text != name and not text.startswith(name + " "):
            continue
        if app not in host.packages.projects or not host.enabled(app):
            return None
        tail = text[len(name):].strip()
        action = {"": "start", "start": "start", "status": "status", "stop": "stop", "hint": "hint",
                  "skip": "skip", "replay": "replay" if app == "rhythm-relay" else "perform", "listen": "listen"}.get(tail)
        valid = {"rhythm-relay": {"start", "status", "stop", "replay", "listen"},
                 "tiny-detective": {"start", "status", "stop", "hint", "skip"},
                 "robot-charades": {"start", "status", "stop", "hint", "skip", "perform"}}
        if action in valid[app]:
            return {"app": app, "tool": "play", "arguments": {"action": action}}
        if tail.startswith("guess ") and app != "rhythm-relay":
            return {"app": app, "tool": "play", "arguments": {"action": "guess", "answer": tail[6:]}}
        if tail.startswith("inspect ") and app == "tiny-detective":
            return {"app": app, "tool": "play", "arguments": {"action": "inspect", "clue": tail[8:]}}
    return None
