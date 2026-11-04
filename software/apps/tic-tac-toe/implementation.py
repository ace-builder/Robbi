"""Project-local implementation. Shared runtime access uses only the supported SDK."""

import io

import os

from pathlib import Path

import random

import re

import time

from PIL import Image, ImageDraw, ImageFont, ImageOps

BG, INK, ACCENT = '#d9f4f0', '#172d31', '#e26d32'

COLOURS = {'red': '#e65b59', 'blue': '#4386e5', 'green': '#35a57a', 'yellow': '#e5b637'}

POSITIONS = ['top left', 'top middle', 'top right', 'middle left', 'center', 'middle right', 'bottom left', 'bottom middle', 'bottom right']

LINES = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]

NUMBER_WORDS = dict(zip('one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split(), range(1,21)))

NUMBER_WORDS.update(dict(zip('eins zwei drei vier fünf sechs sieben acht neun zehn elf zwölf dreizehn vierzehn fünfzehn sechzehn siebzehn achtzehn neunzehn zwanzig'.split(), range(1,21))))
NUMBER_WORDS.update({'ein': 1, 'eine': 1, 'fuenf': 5, 'zwoelf': 12, 'fuenfzehn': 15})

def spoken_number(value):
    text = clean(value)
    text = re.sub(r'^(?:(?:square|number|field|feld|nummer|zahl)\s*|(?:i choose|i pick|ich nehme|ich wähle)\s+)', '', text)
    return NUMBER_WORDS.get(text, int(text) if text.isdigit() and len(text) < 4 else 0)

def clean(text):
    return re.sub(r'[^\w\s-]', '', str(text).casefold()).strip().replace('-', ' ')

def position(text):
    text = clean(text)
    text = re.sub(r'^(?:put (?:it )?(?:in |on )?(?:the )?|place (?:it )?(?:in |on )?(?:the )?|the )', '', text)
    aliases = {'top center':'top middle','upper left':'top left','upper right':'top right',
               'upper middle':'top middle','middle':'center','centre':'center','middle center':'center','middle middle':'center',
               'bottom center':'bottom middle','lower left':'bottom left','lower right':'bottom right',
               'lower middle':'bottom middle','left middle':'middle left','right middle':'middle right',
               'oben links':'top left','oben rechts':'top right','oben mitte':'top middle',
               'mitte':'center','mitte links':'middle left','mitte rechts':'middle right',
               'unten links':'bottom left','unten rechts':'bottom right','unten mitte':'bottom middle'}
    text = aliases.get(text, text)
    if text in POSITIONS:
        return POSITIONS.index(text)
    number = spoken_number(text)
    return number-1 if 1 <= number <= 9 else None

def winner(board):
    for a,b,c in LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return 'draw' if all(board) else None

def best_move(board, difficulty='normal'):
    choices = [i for i, value in enumerate(board) if not value]
    if difficulty == 'easy':
        return random.choice(choices)
    def score(values, turn):
        result = winner(values)
        if result:
            return {'O': 1, 'X': -1, 'draw': 0}[result]
        valuescores = []
        for move, value in enumerate(values):
            if not value:
                trial = values[:]
                trial[move] = turn
                valuescores.append(score(trial, 'X' if turn == 'O' else 'O'))
        return (max if turn == 'O' else min)(valuescores)
    ranked = []
    for i in choices:
        trial = board[:]
        trial[i] = 'O'
        ranked.append((score(trial, 'X'), i))
    high = max(value for value, _ in ranked)
    return random.choice([i for value, i in ranked if value == high])

def font(size):
    return ImageFont.load_default(size=size)

def text_center(draw, text, y, size=22, colour=INK):
    selected = font(size)
    while size > 12 and draw.textlength(text, font=selected) > 260:
        size -= 1
        selected = font(size)
    draw.text((140,y), text, fill=colour, font=selected, anchor='mt')

def board_image(board):
    canvas = Image.new('RGB', (280,240), BG)
    draw = ImageDraw.Draw(canvas)
    text_center(draw, 'YOU: X    ROBBI: O', 9, 17)
    left, top, cell = 50, 39, 60
    for i in (1,2):
        draw.line((left+i*cell,top,left+i*cell,top+180), fill=INK,width=3)
        draw.line((left,top+i*cell,left+180,top+i*cell), fill=INK,width=3)
    for index,value in enumerate(board):
        x,y = left+(index%3)*cell+30, top+(index//3)*cell+30
        if value:
            draw.text((x,y), value, anchor='mm', font=font(41), fill=ACCENT if value=='X' else INK)
        else:
            draw.text((x,y), str(index+1), anchor='mm', font=font(14), fill='#789c98')
    return canvas

def memory_image(round_number, active=None):
    canvas = Image.new('RGB',(280,240),BG)
    draw = ImageDraw.Draw(canvas)
    text_center(draw, f'MEMORY · ROUND {round_number}', 10, 18)
    for index,(name,colour) in enumerate(COLOURS.items()):
        x,y = 14+(index%2)*132, 45+(index//2)*94
        # A flash must be obvious at a glance on the small LCD. Darken the
        # other pads while one is lit; the dot also makes it colour-independent.
        pad_colour = colour
        if active and name != active:
            rgb = tuple(int(colour[i:i+2], 16) for i in (1, 3, 5))
            pad_colour = tuple(round(channel * .38) for channel in rgb)
        draw.rounded_rectangle((x,y,x+120,y+82),radius=14,fill=pad_colour,
                               outline=INK if name==active else BG,width=6 if name==active else 2)
        draw.text((x+60,y+42),name.upper(),anchor='mm',font=font(18),fill=INK if name=='yellow' and (not active or name==active) else 'white')
        if name==active:
            draw.ellipse((x+99,y+8,x+110,y+19),fill='white')
    return canvas

def number_image(low, high, message='YOUR GUESS?'):
    canvas=Image.new('RGB',(280,240),BG)
    draw=ImageDraw.Draw(canvas)
    text_center(draw,'HIGHER / LOWER',20,20)
    text_center(draw,f'{low} - {high}',86,44)
    text_center(draw,message,175,20,ACCENT)
    return canvas

def celebration(label):
    # The exported app carries its own artwork; no robot-root filesystem read.
    artwork = Path(__file__).parent / 'images/screen.png'
    if artwork.is_file():
        with Image.open(artwork) as source:
            canvas = ImageOps.fit(source.convert('RGB'), (280, 240))
    else:
        canvas = Image.new('RGB', (280, 240), BG)
        face = ImageDraw.Draw(canvas)
        face.rounded_rectangle((50, 42, 230, 174), radius=34, fill='#7bd5c4', outline=INK, width=4)
        for x in (106, 174):
            face.arc((x-12, 79, x+12, 101), 180, 360, fill=INK, width=5)
        face.arc((107, 95, 173, 143), 0, 180, fill=INK, width=5)
    draw=ImageDraw.Draw(canvas)
    draw.rounded_rectangle((10,190,270,231),radius=12,fill=INK)
    text_center(draw,label,200,24,'white')
    return canvas

def show(context, image, title):
    artwork = Path(__file__).parent / 'images/icon.png'
    if artwork.is_file():
        with Image.open(artwork) as source:
            badge = ImageOps.fit(source.convert('RGB'), (30, 30))
        image = image.copy()
        mask = Image.new('L', (30, 30))
        ImageDraw.Draw(mask).ellipse((0, 0, 29, 29), fill=255)
        image.paste(badge, (243, 3), mask)
    ImageDraw.Draw(image).rounded_rectangle((1, 1, 278, 238), radius=16, outline='#73c6b9', width=2)
    stream=io.BytesIO()
    image.save(stream,format='PNG')
    artifact=context.save_image(stream.getvalue(),title)
    receipt=context.robot.show_image(artifact['id'],hold_s=120)
    return artifact, receipt.get('displayed') is True

def result(context,state,text,image,*,won=False,ok=True):
    context.checkpoint()
    state['updated_at']=time.time()
    context.write_state(state)
    artifact,displayed=show(context,image,text)
    if won and context.config.get('sound',True):
        context.robot.play_sound('game_victory')
    # Never disclose the hidden number/sequence to the model or dashboard result.
    public={key:value for key,value in state.items() if key not in {'secret','sequence'}}
    # An invalid move is an answered game turn, not an app failure. Keeping the
    # board's successful display receipt also prevents the speech face hiding it.
    return {'ok':True,'text':text,'data':{**public,'accepted':ok,'displayed':displayed,'display_hold_s':120 if displayed else 0,
        'voice_input_hint': ('Say a square from 1 to 9 or its position.' if state.get('game')=='tic-tac-toe' else 'Say the colours in order.' if state.get('game')=='memory-colours' else 'Say a number from 1 to 20.')},'artifacts':[artifact]}

def run_game(context, game, arguments):
    permissions=['state','robot.display']+(['robot.audio'] if context.config.get('sound',True) else [])
    context.request_permissions(*permissions)
    state=context.read_state()
    action=arguments.get('action','move')
    answer=str(arguments.get('answer','')).strip()
    if action=='stop':
        return result(context,{'active':False,'game':game},'Game stopped. We can play again anytime.',
                      number_image(1,20,'SEE YOU NEXT TIME'))
    if action=='start':
        state={'active':True,'game':game,'moves':0}
        if game=='tic-tac-toe':
            state['board']=['']*9
            return result(context,state,'You are X. Say top left, center, bottom right, or a number from one to nine.',board_image(state['board']))
        if game=='memory-colours':
            state.update(sequence=[random.choice(list(COLOURS))],index=0)
            return play_sequence(context,state)
        state.update(secret=random.randint(1,20),low=1,high=20)
        return result(context,state,'I picked a number from one to twenty. What is your guess?',number_image(1,20))
    if not state.get('active'):
        return {'ok':False,'text':'Start a new game first.'}
    if state.get('game')!=game:
        return {'ok':False,'text':'Saved game is invalid. Start a new game.'}
    if game=='tic-tac-toe':
        board=state.get('board')
        if not isinstance(board,list) or len(board)!=9 or any(x not in ('','X','O') for x in board):
            return {'ok':False,'text':'Saved board is invalid. Start a new game.'}
        if action=='status':
            return result(context,state,'Your turn. Choose an empty square.',board_image(board))
        index=position(answer)
        if index is None or board[index]:
            return result(context,state,'Choose an empty square, such as top right or center.',board_image(board),ok=False)
        board[index]='X'
        state['moves']+=1
        outcome=winner(board)
        reply=''
        if not outcome:
            move=best_move(board,context.config.get('difficulty','normal'))
            board[move]='O'
            reply=f'I chose {POSITIONS[move]}. '
            outcome=winner(board)
        if outcome:
            state.update(active=False,winner=outcome)
            label={'X':'YOU WIN!','O':'ROBBI WINS!','draw':'A GOOD DRAW!'}[outcome]
            return result(context,state,reply+label,board_image(board),won=outcome!='draw')
        return result(context,state,reply+'Your turn.',board_image(board))
    if game=='memory-colours':
        sequence=state.get('sequence',[])
        if not sequence or len(sequence)>8 or any(c not in COLOURS for c in sequence):
            return {'ok':False,'text':'Saved sequence is invalid. Start a new game.'}
        if action=='status':
            return play_sequence(context,state)
        tokens=clean(answer).split()
        aliases={'rot':'red','blau':'blue','grün':'green','gelb':'yellow'}
        tokens=[aliases.get(t,t) for t in tokens if t not in {'and','then','und','dann'}]
        if not tokens or any(t not in COLOURS for t in tokens) or len(tokens)>len(sequence)-state['index']:
            return result(context,state,'Say red, blue, green or yellow, in the order I showed you.',memory_image(len(sequence)),ok=False)
        for token in tokens:
            if token!=sequence[state['index']]:
                state.update(active=False,winner='robot')
                return result(context,state,'Almost! The sequence was '+', '.join(sequence)+'. Let us try again.',celebration('NICE TRY!'))
            state['index']+=1
        if state['index']==len(sequence):
            if len(sequence)==context.config.get('rounds',5):
                state.update(active=False,winner='user')
                return result(context,state,'Perfect memory. You win!',celebration('YOU WIN!'),won=True)
            state['sequence'].append(random.choice(list(COLOURS)))
            state['index']=0
            return play_sequence(context,state)
        return result(context,state,'Correct. Next colour?',memory_image(len(sequence)))
    if action=='status':
        return result(context,state,f'Guess between {state["low"]} and {state["high"]}.',number_image(state['low'],state['high']))
    answer=clean(answer)
    guess=spoken_number(answer)
    if not state['low']<=guess<=state['high']:
        return result(context,state,f'Choose a number from {state["low"]} to {state["high"]}.',number_image(state['low'],state['high']),ok=False)
    state['moves']+=1
    if guess==state['secret']:
        state.update(active=False,winner='user')
        return result(context,state,f'You got it in {state["moves"]} guesses!',celebration('YOU GOT IT!'),won=True)
    if guess<state['secret']:
        state['low']=guess+1
        hint='Higher!'
    else:
        state['high']=guess-1
        hint='Lower!'
    return result(context,state,hint,number_image(state['low'],state['high'],hint.upper()))

def play_sequence(context,state):
    state['index']=0
    # Reserve the whole sequence so ambient faces cannot hide a flash.
    with context.robot.session('display',duration_s=15):
        for colour in state['sequence']:
            context.checkpoint()
            show(context,memory_image(len(state['sequence']),colour),'Watch the sequence')
            context.cancelled.wait(.65)
            context.checkpoint()
            show(context,memory_image(len(state['sequence'])),'Watch the sequence')
            context.cancelled.wait(.2)
    return result(context,state,'Your turn. Repeat the colours in order.',memory_image(len(state['sequence'])))
