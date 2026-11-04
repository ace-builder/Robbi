"""Gmail tools share bounded transport and OAuth through the host service."""
from robbi.app_services.http import ProviderError
from robbi.errors import ValidationError


def run(context, tool_name, arguments):
    context.require("network")
    try:
        if tool_name == "search":
            result = context.services.google.gmail_search(context.config, **arguments)
            messages = result["messages"]
            text = "\n".join(f"{item.get('from', 'Unknown sender')}: {item.get('subject') or '(no subject)'} (ID {item['id']})" for item in messages) or "No matching emails found."
            if result["has_more"]:
                text += " More messages match; narrow your search."
        elif tool_name == "read":
            result = context.services.google.gmail_read(context.config, **arguments)
            text = f"From: {result.get('from', 'Unknown sender')}\nSubject: {result.get('subject') or '(no subject)'}\n\n{result['body'][:4000]}"
            if result["truncated"] or len(result["body"]) > 4000:
                text += "\nThis is a shortened preview. Open Gmail for the full message and attachments."
            elif result["body_from_snippet"]:
                text += "\nOnly a snippet is available. Open Gmail for the full message."
        elif tool_name == "draft":
            result = context.services.google.gmail_draft(context.config, **arguments)
            text = f"Saved a draft to {result['to']} with subject {result['subject']}. Review and send it in Gmail: {result['url']}"
        else:
            raise ValidationError("Unknown Gmail tool")
    except (ProviderError, ValidationError) as error:
        return {"ok": False, "text": str(error)}
    return {"ok": True, "text": text, "data": result}
