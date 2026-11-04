"""Optional app-owned illustrations and sound through the permissioned SDK.

Never infer a private screen from arbitrary JSON. Only the small allowlist of
public calculation/game/forecast results can use their returned text.
"""
from .permissions import AppPermissionRequired
from .sdk import AppCancelled
from .manifest import confined


PUBLIC_RESULTS = frozenset({
    'calculator', 'date-calculator', 'decision-helper', 'higher-lower',
    'memory-colours', 'percentage-helper', 'random-number', 'recipe-scaler',
    'rhythm-relay', 'robot-charades', 'sensor-check',
    'tic-tac-toe', 'tiny-detective', 'tip-splitter', 'unit-converter', 'weather', 'world-clock',
})
PRIVATE_MESSAGES = {
    'bookmarks': 'Your links are ready in the dashboard.',
    'checklists': 'Your checklist is ready in the dashboard.',
    'decision-notebook': 'Your decision notebook is ready.',
    'expense-tracker': 'Your expense log is ready in the dashboard.',
    'fridge-rescue': 'Your kitchen plan is ready in the dashboard.',
    'gmail': 'Your Gmail result is ready. Open the dashboard to review it.',
    'google-calendar': 'Your calendar result is ready in the dashboard.',
    'google-maps': 'Your map result is ready. Open the dashboard for details.',
    'habit-tracker': 'Your habit log is ready in the dashboard.',
    'home-care': 'Your home care plan is ready in the dashboard.',
    'kitchen-timers': 'Your timer result is ready in the dashboard.',
    'meeting-prep': 'Your meeting preparation is ready in the dashboard.',
    'packing-list': 'Your packing list is ready in the dashboard.',
    'password-generator': 'Password ready in your private dashboard.',
    'quick-notes': 'Your notes are ready in the dashboard.',
    'ready-to-leave': 'Your departure plan is ready in the dashboard.',
    'text-toolbox': 'Your text result is ready in the dashboard.',
    'teach-me': 'Your learning card is ready in the dashboard.',
    'robot-status': 'Robot health details are ready in the dashboard.',
    'web-search': 'Your search results are ready in the dashboard.',
    'where-did-i-put-it': 'Your item result is ready in the dashboard.',
}


def output_settings(context, arguments=None):
    config, arguments = context.config, arguments or {}
    screen = config.get('show_screen', config.get('screen', True))
    sound = config.get('play_sound', config.get('sound', True))
    if 'output' in config:
        screen = screen and config['output'] in {'screen', 'both'}
        sound = sound and config['output'] in {'sound', 'both'}
    if arguments.get('show_on_screen') is False:
        screen = False
    if arguments.get('with_audio') is False:
        sound = False
    return bool(screen), bool(sound)


class AppPresentation:
    def __init__(self, context, manifest, *, trusted=False, arguments=None):
        self.context, self.manifest, self.trusted = context, manifest, trusted
        self.screen = self.sound = False
        context._presentation_title = manifest.get('name', context.app_id)
        if context._read_only or context.app_id in {'expressions', 'image-studio'}:
            return
        if manifest.get('category') == 'games' and (arguments or {}).get('action') in {'status', 'stop'}:
            return
        # Imported apps may use the same SDK and assets, but their unreviewed
        # JSON results never become public display content automatically.
        screen, sound = output_settings(context, arguments)
        root = context._project_root
        self.screen = bool(screen and root is not None and
                           confined(root, 'images/screen.png').is_file() and
                           'robot.display' in context.permissions and hasattr(context.robot._port, 'show_image'))
        self.sound = bool(sound and root is not None and
                          confined(root, 'sounds/reveal.wav').is_file() and
                          'robot.audio' in context.permissions and hasattr(context.robot._port, 'play_sound'))
        requested = (['robot.display'] if self.screen else []) + (['robot.audio'] if self.sound else [])
        try:
            context.request_permissions(*requested)
        except AppPermissionRequired:
            # Decorative output must not prevent an otherwise authorized task.
            # Request consent before any business mutation, then retain only
            # already granted capabilities for this invocation.
            for permission, field in [('robot.display', 'screen'), ('robot.audio', 'sound')]:
                if getattr(self, field):
                    try:
                        context.require(permission)
                    except AppPermissionRequired:
                        setattr(self, field, False)

    def finish(self, result):
        context = self.context
        context.checkpoint()
        if context._read_only or not (self.screen or self.sound or
                                     getattr(context, '_screen_requested', False) or
                                     getattr(context, '_audio_requested', False)):
            return result
        data = result.get('data')
        presentation = {
            'kind': ('numbers' if context.app_id == 'random-number' else
                     'game' if self.manifest.get('category') == 'games' else 'result'),
            'screen_requested': self.screen or getattr(context, '_screen_requested', False),
            'audio_requested': self.sound or getattr(context, '_audio_requested', False),
            'displayed': False, 'audio_played': False,
        }
        if isinstance(data, dict) and isinstance(data.get('presentation'), dict):
            presentation.update(data['presentation'])
        if result.get('cancelled') or result.get('permission_required'):
            return result
        success = bool(result.get('ok', True))
        # A fresh calculation is appropriate on a shared display. Private data,
        # user-authored notes, account messages and provider errors are not.
        text = (str(result.get('text', 'Result ready.')) if self.trusted and context.app_id in PUBLIC_RESULTS
                else PRIVATE_MESSAGES.get(context.app_id, 'Your result is ready in the dashboard.'))
        if not success:
            text = 'This needs another try. Check the dashboard for details.'
        if self.screen and not getattr(context, '_screen_requested', False):
            try:
                numbers = data.get('numbers', []) if context.app_id == 'random-number' and isinstance(data, dict) else []
                receipt = context.robot.display_card(text[:500] or 'Result ready.',
                    kind='numbers' if numbers else 'card', values=numbers,
                    animate=bool(numbers and len(numbers) <= 10), hold_s=30)
                presentation.update(displayed=receipt.get('displayed') is True,
                                    truncated=receipt.get('truncated', False))
                if not isinstance(result.get('artifacts'), list):
                    result['artifacts'] = []
                result['artifacts'].append(receipt['artifact'])
                if 'data' not in result:
                    data = result['data'] = {}
                if isinstance(data, dict):
                    data['displayed'] = presentation['displayed']
                    data['display_hold_s'] = receipt.get('display_hold_s', 30) if presentation['displayed'] else 0
            except AppCancelled:
                raise
            except Exception:
                context.checkpoint()
                presentation['screen_state'] = 'unavailable'
        elif getattr(context, '_screen_requested', False):
            receipt = getattr(context, '_last_screen_receipt', {})
            presentation['displayed'] = bool((isinstance(data, dict) and data.get('displayed')) or
                                              (isinstance(receipt, dict) and receipt.get('displayed') is True))
        if getattr(context, '_audio_requested', False):
            receipt = getattr(context, '_last_audio_receipt', {})
            presentation['audio_played'] = bool((isinstance(data, dict) and data.get('audio_played')) or
                                                (isinstance(receipt, dict) and receipt.get('ok') is True))
        if self.sound and success and not getattr(context, '_audio_requested', False):
            try:
                receipt = context.robot.play_sound_file('sounds/reveal.wav')
                presentation['audio_played'] = receipt.get('ok') is True
            except AppCancelled:
                raise
            except Exception:
                context.checkpoint()
                presentation['audio_state'] = 'unavailable'
        if isinstance(data, dict):
            # No private result text/values are copied into presentation metadata.
            data['presentation'] = presentation
        return result
