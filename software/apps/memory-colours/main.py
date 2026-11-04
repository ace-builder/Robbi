from .implementation import run_game

def run(context, tool_name, arguments):
    return run_game(context, 'memory-colours', arguments)
