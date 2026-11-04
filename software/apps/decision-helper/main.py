import secrets
from robbi.app_runtime.sdk import AppCancelled
from robbi.app_runtime.permissions import AppPermissionRequired


def run(context, tool_name, arguments):
    if context.config['show_screen']:
        context.request_permissions('robot.display')
    action = arguments['action']
    rng = secrets.SystemRandom()
    if action == 'coin':
        values = [rng.choice(['Heads', 'Tails'])]
        text = values[0] + '.'
    elif action == 'dice':
        values = [rng.randint(1, arguments['sides']) for _ in range(arguments['count'])]
        text = ', '.join(map(str, values)) + f'. Total: {sum(values)}.'
    else:
        values = list(arguments.get('choices', []))
        if len(values) < 2 or any(not value.strip() for value in values):
            return {'ok': False, 'text': 'Give me at least two nonempty options.'}
        if action == 'choose':
            values = [rng.choice(values)]
        else:
            rng.shuffle(values)
        text = ', '.join(values)
    data, artifacts = {'values': values, 'action': action, 'presentation': {'kind': action}}, []
    if action == 'dice':
        data.update(total=sum(values), sides=arguments['sides'])
    if context.config['show_screen']:
        try:
            receipt = context.robot.display_card(text[:500], kind=action, values=values, animate=True, hold_s=30)
            data['displayed'] = receipt.get('displayed', False)
            data['display_hold_s'] = receipt.get('display_hold_s', 30) if data['displayed'] else 0
            data['presentation'].update(displayed=data['displayed'], truncated=receipt.get('truncated', False))
            artifacts = [receipt['artifact']]
        except (AppCancelled, AppPermissionRequired):
            raise
        except Exception:
            context.checkpoint()
            data.update(displayed=False)
            data['presentation']['screen_state'] = 'unavailable'
    return {'ok': True, 'text': text, 'data': data, 'artifacts': artifacts}
