def run(context, tool_name, arguments):
    return context.robot.timer(tool_name, **arguments)
