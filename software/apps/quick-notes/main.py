"""Example stateful project: independent of the legacy productivity repository."""
import time
import uuid


def run(context, tool_name, arguments):
    state = context.read_state()
    notes = state.get("notes", [])
    if not isinstance(notes, list) or any(not isinstance(note, dict) for note in notes):
        raise ValueError("Notes state is invalid; repair the app state file")
    if tool_name == "list":
        selected = list(reversed(notes))[:arguments.get("limit", 10)]
        return {"ok": True, "text": f"{len(notes)} saved notes." if notes else "No quick notes yet.", "data": {"notes": selected, "total": len(notes)}}
    if tool_name == "add":
        if len(notes) >= 40:
            raise ValueError("The app holds 40 quick notes. Delete an old note before adding another.")
        note = {"id": uuid.uuid4().hex[:12], "text": arguments["text"], "created_at": time.time()}
        notes.append(note)
        context.write_state({"notes": notes})
        return {"ok": True, "text": "Quick note saved.", "data": note}
    remaining = [note for note in notes if note.get("id") != arguments["id"]]
    if len(remaining) == len(notes):
        return {"ok": False, "text": "That note does not exist."}
    context.write_state({"notes": remaining})
    return {"ok": True, "text": "Quick note deleted."}
