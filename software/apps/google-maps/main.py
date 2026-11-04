"""Google Maps links need no credentials; API facts use the network service."""
from robbi.app_services.maps import search_url, directions_url


def run(context, tool_name, arguments):
    context.checkpoint()
    if tool_name == "search":
        url = search_url(arguments["query"], arguments.get("place_id", ""))
        result = {"ok": True, "url": url, "link_only": True,
                  "summary": "Google Maps search link ready. Open it to see places; no place results have been retrieved."}
    elif tool_name == "directions":
        url = directions_url(arguments["destination"], arguments.get("origin", ""), arguments.get("mode", "driving"))
        result = {"ok": True, "url": url, "link_only": True,
                  "summary": "Google Maps directions link ready. Open it to calculate the route; no distance or travel time has been retrieved."}
    elif tool_name == "geocode":
        result = context.services.maps.geocode(context.config, arguments["address"],
                                               cancellation=context.cancelled, deadline=context.deadline)
    elif tool_name == "route":
        result = context.services.maps.route(context.config, arguments["origin"], arguments["destination"],
                                             mode=arguments.get("mode", "driving"),
                                             cancellation=context.cancelled, deadline=context.deadline)
    else:
        raise ValueError("Unknown Google Maps tool")
    text = result["summary"]
    if result.get("url"):
        text += "\n" + result["url"]
    return {"ok": result.get("ok", True), "text": text, "data": result}
