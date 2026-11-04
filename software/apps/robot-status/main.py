"""Example app using only the narrow status capability."""


def run(context, tool_name, arguments):
    result = context.robot.status()
    health = result.get("health", {})
    issues = [item for item in health.get("components", []) if item.get("state") in {"failed", "degraded", "retrying"}]
    disabled = [item["name"] for item in health.get("components", []) if item.get("state") == "disabled"]
    text = "No component errors are reported." if not issues else "Components needing attention: " + "; ".join(f"{item['name']}: {item.get('message') or item['state']}" for item in issues)
    if disabled:
        text += " Disabled components: " + ", ".join(disabled) + "."
    return {"ok": True, "text": text, "data": {"state": health.get("state"), "issues": issues, "disabled": disabled, "uptime_s": health.get("uptime_s")}}
