def run(context, tool_name, arguments):
    context.request_permissions('state', *(['robot.display'] if context.config['show_screen'] else []))
    state = context.read_state()
    lists = state.setdefault('lists', {})
    if not isinstance(lists, dict) or len(lists) > 15:
        raise ValueError('Saved checklists are invalid; restore the app state backup.')
    action, name = arguments['action'], arguments.get('name', '').strip()
    if action == 'list':
        return {'ok': True, 'text': ', '.join(lists) if lists else 'No checklists yet.', 'data': {'names': list(lists)}}
    if not name:
        return {'ok': False, 'text': 'Which checklist should I use?'}
    if action == 'create':
        items = arguments.get('items', [])
        if not items or any(not item.strip() for item in items):
            return {'ok': False, 'text': 'Supply at least one nonempty checklist item.'}
        if name in lists:
            return {'ok': False, 'text': 'That checklist already exists. Choose a new name.'}
        if len(lists) >= 15:
            return {'ok': False, 'text': 'Fifteen checklists are saved. Delete an old one before creating another.'}
        lists[name] = [{'text': item.strip(), 'done': False} for item in items]
    elif name not in lists:
        return {'ok': False, 'text': 'That checklist does not exist.'}
    items = lists[name]
    if not isinstance(items, list) or not items or any(not isinstance(item, dict) or not isinstance(item.get('text'), str) or type(item.get('done')) is not bool for item in items):
        raise ValueError('Saved checklist is invalid; restore the app state backup.')
    if action in {'check', 'uncheck'}:
        index = arguments.get('item', 0) - 1
        if not 0 <= index < len(items):
            return {'ok': False, 'text': 'Choose an item number from this checklist.'}
        items[index]['done'] = action == 'check'
    if action == 'reset':
        for item in items:
            item['done'] = False
    if action == 'delete':
        del lists[name]
        context.write_state(state)
        return {'ok': True, 'text': 'Checklist deleted.'}
    context.write_state(state)
    complete = sum(item['done'] for item in items)
    next_item = next((item['text'] for item in items if not item['done']), None)
    text = f'{name}: {complete} of {len(items)} complete. ' + (f'Next: {next_item}' if next_item else 'All done!')
    data = {'name': name, 'items': [{'number': i+1, **item} for i, item in enumerate(items)], 'next': next_item}
    artifacts = []
    if context.config['show_screen']:
        shown = context.robot.display_text(text[:500], hold_s=60)
        data['displayed'] = shown.get('displayed', False)
        artifacts = [shown['artifact']]
    return {'ok': True, 'text': text, 'data': data, 'artifacts': artifacts}
