"""Search the web and preserve sources as untrusted external content."""


def run(context, tool_name, arguments):
    result = context.services.search.search(context.config, arguments["query"], count=arguments.get("count", 5),
                                            topic=arguments.get("topic", "general"), time_range=arguments.get("time_range"),
                                            cancellation=context.cancelled, deadline=context.deadline)
    lines = [result["summary"]]
    for index, source in enumerate(result["results"][:3], 1):
        lines.append(f"{index}. {source['title']} — {source['snippet'][:200]}\n{source['url']}")
    return {"ok": result.get("ok", True), "text": "\n".join(lines), "data": result}
