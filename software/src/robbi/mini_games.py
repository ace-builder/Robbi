"""Compatibility voice routing for installed games; execution lives in app projects."""

import re
import time

COLOURS = {'red': '#e65b59', 'blue': '#4386e5', 'green': '#35a57a', 'yellow': '#e5b637'}

POSITIONS = ['top left', 'top middle', 'top right', 'middle left', 'center', 'middle right', 'bottom left', 'bottom middle', 'bottom right']

NUMBER_WORDS = dict(zip('one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split(), range(1,21), strict=True))

GAME_IDS = frozenset({'tic-tac-toe', 'higher-lower', 'memory-colours', 'tiny-detective', 'robot-charades', 'rhythm-relay'})
NUMBER_WORDS.update(dict(zip('eins zwei drei vier fünf sechs sieben acht neun zehn elf zwölf dreizehn vierzehn fünfzehn sechzehn siebzehn achtzehn neunzehn zwanzig'.split(), range(1, 21), strict=True)))
NUMBER_WORDS.update({'ein': 1, 'eine': 1, 'fuenf': 5, 'zwoelf': 12})
NUMBER_WORDS.update({'zero': 0, 'null': 0, 'fuenfzehn': 15})


def polite_turn(text):
    """Remove conversational padding only at the edges, never words in a move."""
    text = clean(text)
    text = re.sub(r'^(?:(?:please|bitte)\s+)+', '', text)
    return re.sub(r'(?:\s+(?:please|bitte|thanks|thank you|danke|for me))+$', '', text)


def move_phrase(text):
    text = polite_turn(text)
    # This is deliberately an anchored grammar, not a search for any number.
    # "Don't choose one", "one or two", and "one plus two" are not moves.
    text = re.sub(
        r'^(?:(?:my guess is|i guess|guess|i choose|i pick|i take|ill take|i will take|'
        r'id like|i would like|ich nehme|ich wähle|ich waehle)\s+|'
        r'(?:(?:can you|could you)\s+)?(?:put|place)\s+(?:(?:it|x|my x)\s+)?'
        r'(?:(?:in|on|at)\s+)?)', '', text)
    return re.sub(r'^(?:(?:the|die|das|den)\s+)?(?:(?:square|number|field|feld|nummer|zahl)\s*)?', '', text)


def number_answer(text):
    text = move_phrase(text)
    if text in NUMBER_WORDS:
        return NUMBER_WORDS[text]
    return int(text) if text.isascii() and text.isdigit() and len(text) < 4 else None

def spoken_number(text):
    number = number_answer(text)
    return number if number is not None else 0

def clean(text):
    # Preserve a signed number as a non-move; a minus must never become square 1.
    text = re.sub(r'[-−]\s*(?=\d)', 'minus ', str(text).casefold())
    # STT punctuation must not turn 0.1, .5, or 1/2 into a playable integer.
    text = re.sub(r'(?<!\w)(?:\d+[.,/:]\d+|[.,]\d+)(?!\w)', ' noninteger ', text)
    text = re.sub(r"['’]", '', text)
    return ' '.join(re.sub(r'[^\w\s-]', ' ', text).replace('-', ' ').split())

def position(text):
    text = move_phrase(text)
    aliases = {'top center':'top middle','top centre':'top middle','upper left':'top left','upper right':'top right',
               'upper middle':'top middle','middle':'center','centre':'center','middle center':'center','middle middle':'center',
               'bottom center':'bottom middle','bottom centre':'bottom middle','lower left':'bottom left','lower right':'bottom right',
               'lower middle':'bottom middle','left middle':'middle left','right middle':'middle right',
               'oben links':'top left','oben rechts':'top right','oben mitte':'top middle',
               'mitte':'center','mitte links':'middle left','mitte rechts':'middle right',
               'unten links':'bottom left','unten rechts':'bottom right','unten mitte':'bottom middle'}
    text = aliases.get(text, text)
    if text in POSITIONS:
        return POSITIONS.index(text)
    number = spoken_number(text)
    return number-1 if 1 <= number <= 9 else None

def game_turn_request(game, text):
    """Resolve a bounded turn for an already selected game, without state access."""
    if game not in GAME_IDS:
        return None
    raw = str(text).strip()
    text = polite_turn(raw)
    if not text or len(raw) > 100:
        return None
    command = {
        'stop': 'stop', 'stop game': 'stop', 'quit': 'stop', 'quit game': 'stop',
        'end game': 'stop', 'stopp': 'stop', 'spiel stoppen': 'stop',
        'status': 'status', 'game status': 'status', 'show board': 'status',
        'show sequence': 'status', 'repeat sequence': 'status',
        'repeat': 'status', 'hint': 'hint', 'a hint': 'hint', 'give me a hint': 'hint', 'tipp': 'hint',
        'skip': 'skip', 'überspringen': 'skip', 'replay': 'replay',
        'listen': 'listen', 'listen now': 'listen', 'perform': 'perform',
    }.get(text)
    allowed = {'stop', 'status'}
    if game in {'tiny-detective', 'robot-charades'}:
        allowed |= {'hint', 'skip'}
    if game == 'robot-charades':
        allowed.add('perform')
        if command == 'replay':
            command = 'perform'
    if game == 'rhythm-relay':
        allowed |= {'replay', 'listen'}
        if text == 'repeat':
            command = 'replay'
    arguments = None
    if command in allowed:
        arguments = {'action': command}
    elif command:
        return None
    elif game == 'tic-tac-toe' and position(text) is not None:
        # Apps receive the schema's canonical string, not a second opportunity
        # to interpret natural language differently from microphone admission.
        arguments = {'action': 'move', 'answer': str(position(text) + 1)}
    elif game in {'tic-tac-toe', 'higher-lower'} and number_answer(text) is not None:
        # Out-of-range numbers are still game turns: the app gives its normal
        # retry prompt and preserves the board, instead of invoking a planner.
        arguments = {'action': 'move', 'answer': str(number_answer(text))}
    elif game == 'memory-colours':
        colours = {'red', 'blue', 'green', 'yellow', 'rot', 'blau', 'grün', 'gelb'}
        tokens = text.split()
        if any(t in colours for t in tokens) and all(t in colours | {'and', 'then', 'und', 'dann'} for t in tokens):
            arguments = {'action': 'move', 'answer': text}
    elif game in {'tiny-detective', 'robot-charades'}:
        if '?' in raw or len(text.split()) > 8:
            return None
        if re.match(r'^(?:what|who|where|when|why|how|can|could|would|will|do|does|is|are|tell|please|weather|search|find|open|play|start|new|was|wer|wo|wann|warum|wie|kann|suche|wetter)\b', text):
            return None
        if game == 'tiny-detective' and text.startswith('inspect '):
            arguments = {'action': 'inspect', 'clue': text[8:]}
        else:
            answer = re.sub(r'^(?:my guess is|i guess|guess|i think it is|it is)\s+', '', text)
            if answer:
                arguments = {'action': 'guess', 'answer': answer}
    if arguments is not None:
        return {'app': game, 'tool': 'play', 'arguments': arguments}
    return None


def local_game_request(host, text, *, allow_active=True):
    """Resolve explicit app requests; optionally use local wake/UI game context."""
    from robbi.app_services.creative_games import creative_game_request
    creative = creative_game_request(host, text)
    if creative is not None:
        return creative
    text = polite_turn(text)
    names = {'tic tac toe': 'tic-tac-toe', 'noughts and crosses': 'tic-tac-toe',
             'memory colours': 'memory-colours', 'memory colors': 'memory-colours',
             'colour memory': 'memory-colours', 'color memory': 'memory-colours',
             'higher lower': 'higher-lower', 'higher or lower': 'higher-lower'}
    start = re.sub(r'^(?:please )?(?:play |start |lets play |let us play )', '', text)
    if start in names:
        selected = names[start]
        if selected in host.packages.projects and host.enabled(selected):
            return {'app': selected, 'tool': 'play', 'arguments': {'action': 'start'}}
        return None
    if not allow_active:
        return None
    session_reader = getattr(host, 'game_session', None)
    if callable(session_reader):
        session = session_reader()
        if session:
            selected = session.get('app')
            if selected in GAME_IDS and selected in host.packages.projects and host.enabled(selected):
                return game_turn_request(selected, text)
            return None
    # Compatibility path for explicit local wake/UI calls. Never inspect board,
    # secret number, sequence, or creative-app private state.
    active = []
    for game in ('tic-tac-toe', 'memory-colours', 'higher-lower'):
        if game in host.packages.projects and host.enabled(game):
            state = host._read_object(host.directory / 'data' / game / 'state.json')
            if state and time.time() - state.get('updated_at', 0) < 1800:
                active.append((state.get('updated_at', 0), game, state.get('active', False)))
    if not active:
        return None
    _, game, running = max(active)
    return game_turn_request(game, text) if running else None
