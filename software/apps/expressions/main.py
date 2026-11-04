def run(context, tool_name, arguments):
    context.checkpoint()
    if tool_name == 'advent':
        arguments = {**arguments, 'name': f"advent-{arguments.get('day', 1):02d}"}
    if tool_name in {'animate', 'gag', 'meme', 'kindness', 'postcard', 'seasonal', 'advent'}:
        if arguments.get('with_audio', True):
            repeats = arguments.get('loops', 1) if tool_name in {'animate', 'gag'} else 1
            completed = 0
            for _ in range(repeats):
                context.checkpoint()
                result = dict(context.robot.play_video(arguments['name']))
                result['displayed'] = result.get('frames_displayed', 0) > 0
                if result.get('ok') and result.get('audio_state') != 'completed':
                    result.update(ok=False, state='partial',
                                  reason='The animation displayed, but its soundtrack did not complete.')
                if not result.get('ok'):
                    break
                completed += 1
            result['loops_completed'] = completed
        else:
            result = context.robot.face_animation(arguments['name'],
                fps=arguments.get('fps', 10), loops=arguments.get('loops', 1))
    else:
        result = (context.robot.gesture(**arguments) if tool_name == 'perform'
                  else context.robot.face_animation(**arguments))
    if result.get('ok') and result.get('displayed'):
        result['display_hold_s'] = 8
    message = result.get('reason') or {
        'display_unavailable': 'Robbi’s screen is unavailable. Check the display connection.',
        'cancelled': 'Expression stopped.',
        'unavailable': 'The required robot hardware is unavailable.',
    }.get(result.get('state'), 'Expression could not play.')
    return {'ok': result.get('ok', False),
            'text': ('Played ' + str(arguments.get('name', 'expression')).replace('-', ' ') + '.'
                     if result.get('ok') else str(message)),
            'data': result}
