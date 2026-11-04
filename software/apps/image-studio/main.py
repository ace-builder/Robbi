"""Image app: provider configuration is separate from the robot display API."""
import json


def run(context, tool_name, arguments):
    if tool_name == "show":
        context.request_permissions("robot.display")
        output = context.robot.show_image(arguments["artifact_id"], hold_s=arguments.get("hold_s", 10))
        return {"ok": bool(output["displayed"]), "text": "Saved image displayed on Robbi." if output["displayed"] else output.get("reason", "Display unavailable"), "data": output}
    # Resolve consent before a potentially chargeable generation request.
    context.request_permissions("network", "robot.display")
    config = dict(context.config)
    for name in ("request_options", "request_template"):
        encoded = config.pop(name + "_json", "")
        if encoded.strip():
            value = json.loads(encoded)
            if not isinstance(value, dict):
                raise ValueError(f"{name} must be a JSON object")
            config[name] = value
    config = {key: value for key, value in config.items() if value != ""}
    generated = context.services.images.generate(config, arguments["prompt"], cancellation=context.cancelled, deadline=context.deadline)
    artifact = context.save_image(generated.data, arguments["prompt"][:100])
    output = context.robot.show_image(artifact["id"], hold_s=arguments.get("hold_s", 10)) if arguments.get("show_on_screen", True) else {"displayed": False, "reason": "Saved as a preview"}
    text = "Image generated and displayed on Robbi." if output["displayed"] else f"Image generated. {output['reason']}."
    return {"ok": True, "text": text, "data": {"provider": generated.provider, "model": generated.model, **output}, "artifacts": [artifact]}
