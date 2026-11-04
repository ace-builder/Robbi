"""Location-aware forecasts through a replaceable shared service."""


def run(context, tool_name, arguments):
    config = {key: value for key, value in context.config.items() if value != ""}
    result = context.services.weather.forecast(
        config, arguments.get("location", ""), arguments.get("day", "tomorrow"),
        latitude=arguments.get("latitude"), longitude=arguments.get("longitude"),
        timezone=arguments.get("timezone") or None, country_code=arguments.get("country_code") or None,
        cancellation=context.cancelled, deadline=context.deadline,
    )
    return {"ok": result.get("ok", True), "text": result.get("summary", "Forecast received."), "data": result}
