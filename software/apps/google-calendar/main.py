"""Voice and generated dashboard entrypoint for Google Calendar."""
from robbi.app_services.http import ProviderError
from robbi.errors import ValidationError


def run(context, tool_name, arguments):
    context.require("network")
    try:
        if tool_name == "events":
            result = context.services.google.calendar_events(context.config, **arguments)
            events = result["events"]
            if events:
                lines = [f"{item['title']}: {item['start']} to {item['end']}" + (f", {item['location']}" if item['location'] else "") for item in events]
                text = "Google Calendar (" + result["timezone"] + "):\n" + "\n".join(lines)
            else:
                text = "No appointments found in the selected date range."
            if result["has_more"]:
                text += " More results exist; narrow the date range or search words."
        elif tool_name == "create":
            result = context.services.google.calendar_create(context.config, **arguments)
            event = result["event"]
            text = f"Created {event['title']} in Google Calendar, {event['start']} to {event['end']}."
        else:
            raise ValidationError("Unknown Calendar tool")
    except (ProviderError, ValidationError) as error:
        return {"ok": False, "text": str(error)}
    return {"ok": True, "text": text, "data": result}
