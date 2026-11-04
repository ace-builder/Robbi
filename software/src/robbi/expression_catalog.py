"""Shared expression names and narrow, local voice shortcuts."""
import re


SEASONAL_ANIMATIONS = {
    'season-birthday': 'A birthday cake celebrates with a tiny confetti surprise.',
    'season-christmas': 'A festive gift reveals a gingerbread surprise.',
    'season-halloween': 'A tiny ghost borrows a pumpkin hat.',
    'season-easter': 'An Easter egg opens to a springtime surprise.',
    'season-summer': 'A melting ice cream gets its own parasol.',
    'season-autumn': 'A hedgehog catches a cozy leaf blanket.',
    'season-winter': 'A snowman pulls its scarf up against the falling snow.',
    'season-spring': 'A watering can brings a small garden to life.',
    'season-new-year': 'A tiny party popper releases a shower of stars, without a fixed year.',
}

SEASONAL_ENCORE_ANIMATIONS = {
    'season-valentine': 'Two shy paper hearts orbit and find each other.',
    'season-lunar-new-year': 'A red lucky packet opens with a golden flourish.',
    'season-carnival': 'A carnival mask performs a ribbon shimmy and confetti bow.',
    'season-lucky-clover': 'A green hat catches a little shamrock.',
    'season-earth-day': 'A little Earth receives a leafy repair.',
    'season-summer-picnic': 'Tiny ants attempt a very ambitious strawberry heist.',
    'season-autumn-harvest': 'A basket catches a surprisingly generous mushroom harvest.',
    'season-winter-solstice': 'A small sunrise brings warmth back to snowy hills.',
    'season-april-fools': 'A flowerpot sprouts a decidedly unexpected rubber duck.',
    'season-oktoberfest': 'Two pretzels bounce into a cheerful little toast.',
    'season-diwali': 'An oil lamp lights a blooming geometric rangoli.',
    'season-hanukkah': 'Eight candles and a raised helper candle make a warm greeting.',
}

ALL_SEASONAL_ANIMATIONS = {**SEASONAL_ANIMATIONS, **SEASONAL_ENCORE_ANIMATIONS}

ADVENT_ANIMATIONS = {
    f'advent-{day:02d}': description for day, description in enumerate([
        'A tiny door opens to the first star.', 'A falling spark lights a candle.',
        'A scarf wraps a chilly gift.', 'An ornament finds its branch.',
        'A festive letter folds itself.', 'A Nicholas boot receives treats.',
        'A cookie gets a little icing.', 'A mitten catches a snowflake.',
        'A tiny train brings cocoa.', 'A paper snowflake unfolds.',
        'An orange peel curls into a star.', 'Two mugs share a little toast.',
        'A sleepy gingerbread tucks into a matchbox bed.', 'A penguin skates into a spin.',
        'A gift ribbon ties itself.', 'A snow globe settles into calm.',
        'A music box plays a dancing paper tree.', 'A lantern catches a wandering star.',
        'A pinecone tries on a festive hat.', 'A tiny sled delivers an enormous heart.',
        'A reindeer finds a carrot snack.', 'Socks warm beside a glowing fireplace.',
        'The moon tucks in a snowy village.', 'A paper theatre lights up for Christmas Eve.',
    ], 1)
}


POSTCARD_ANIMATIONS = {
    'post-sunshine': 'A jar opens to release a little sun. For your cloudy days.',
    'post-peace-tea': 'A cup of tea raises a tiny peace flag. Sorry about the chaos.',
    'post-slow-club': 'A determined snail earns a party hat for small progress. Still counts.',
    'post-heart-mail': 'A heart rises from a priority envelope. You matter, a lot.',
    'post-return-guilt': 'Unnecessary guilt gets a return-to-sender stamp. Not yours to carry.',
    'post-soft-landing': 'A paper plane lands on a soft pillow. It is okay to restart.',
}


SWEET_ANIMATIONS = {
    'sweet-flower': 'A bashful Robbi offers a smiling flower. Just because.',
    'sweet-pocket-hug': 'A tiny robot wraps its arms around an oversized heart. One hug, all yours.',
    'sweet-sorry': 'A sheepish apology patches a heart with a little bandage. Sorry, I will try again.',
    'sweet-cookie': 'The last cookie breaks in two. The bigger half is yours.',
    'sweet-rain': 'Robbi opens an umbrella and makes room for quiet company on a rainy day.',
    'sweet-rest': 'A blanket tuck, closing eyes and a little moon. You have done enough.',
}


MEME_ANIMATIONS = {
    'meme-support-brick': 'An emotional-support brick lands with a thud. It gets me.',
    'meme-optimism-shredder': 'A tiny shredder recycles optimism. Back to reality.',
    'meme-last-beep': 'Per my last beep: a receipt, a stamp and extremely pointed beeps.',
    'meme-premium-empathy': 'A warm heart becomes a locked subscription. Free trial ended.',
    'meme-overreaction': 'A microscopic inconvenience gets a huge theatrical response. Reasonable.',
    'meme-family-audacity': 'A can of audacity expands to family size with a smug honk.',
    'meme-problem-compost': 'A problem is composted into a thriving plant. Personal growth, unfortunately.',
}


MEME_ENCORE_ANIMATIONS = {
    'meme-dumpster-promotion': 'A flaming dumpster receives a crown. Management material.',
    'meme-meeting-escape': 'A calendar invitation grows wings. Could have been an email.',
    'meme-brain-tabs': 'Too many browser tabs reveal a duck DJ. One tab is playing music.',
    'meme-delulu-launch': 'A toaster launches its bread into orbit. Powered by delusion.',
    'meme-last-spoon': 'An unimpressed raccoon loses its final spoon. Absolutely not.',
    'meme-tiny-deadline': 'A snail meets an unreasonable deadline. Due yesterday.',
    'meme-vibe-check': 'A duck passes the brain scanner. No thoughts. Approved.',
    'meme-budget-luxury': 'One coin gets a museum display. Financially decorative.',
    'meme-overthinking': 'An evidence board fills with red string. I connected nothing.',
    'meme-couch-quest': 'A cat accepts a soft cushion and a crown. Side quest: nap.',
}

MEME_REFLECTIONS_ANIMATIONS = {
    'meme-boundary-garden': 'A cactus raises a fence against incoming demands. Boundaries, not attitude.',
    'meme-weather-candle': 'An umbrella shelters a stubborn candle. Still here, still lit.',
    'meme-priority-octopus': 'An octopus is overwhelmed by eight urgent flags, then keeps one.',
    'meme-moving-goalposts': 'A goal grows shoes and runs away from an approaching ball.',
    'meme-perfection-draft': 'A perfectionist pencil finally accepts a good-enough stamp.',
    'meme-invisible-progress': 'Quiet soil reveals growing roots. Roots do not do PR.',
    'meme-inner-critic': 'A peaceful frog mutes an intrusive megaphone.',
    'meme-reply-later': 'Messages become a letter in a bottle. Reachable, eventually.',
    'meme-comparison-clocks': 'Two clocks run at different speeds and receive equal gold stars.',
    'meme-plot-twist-boat': 'A discarded plan becomes a boat sailing toward a sunrise.',
}

AFTER_HOURS_ANIMATIONS = {
    'meme-feature-cactus': 'A cactus passes an inspection without sanding down a single spine.',
    'meme-urgent-laundry': 'An urgent memo takes a miniature wash cycle and comes out reading can wait.',
    'meme-expectation-luggage': 'An enormous cloud of shoulds compresses into a tiny suitcase that rolls away.',
    'meme-tomorrow-vending': 'Pressing NOW on a vending machine delivers three cans of tomorrow.',
    'meme-plot-armor': 'A cardboard shield protects a teacup from a meteor that becomes a sugar cube.',
    'meme-still-mending': 'Two halves of a cracked mug reunite along a golden seam, then hold a small flower.',
    'meme-shrimp-authority': 'A tiny shrimp judge bangs a gavel and stamps an official permission to log off.',
    'meme-capypause': 'A serene capybara lowers its parasol while incoming mail bounces harmlessly away.',
    'meme-hot-take-freezer': 'A flaming speech bubble enters a freezer and comes back as one harmless ice cube.',
    'meme-control-tea': 'An impossible control panel folds down to reveal one calm, manageable cup of tea.',
}

ALL_MEME_ANIMATIONS = {**MEME_ANIMATIONS, **MEME_ENCORE_ANIMATIONS, **MEME_REFLECTIONS_ANIMATIONS, **AFTER_HOURS_ANIMATIONS}


SCREEN_GAGS = {
    'gag-buffering': 'A loading bar reaches 99 percent and stubbornly stays there.',
    'gag-deal-with-it': 'Sunglasses descend onto Robbi for a very small mic drop.',
    'gag-this-is-fine': 'Robbi calmly sips tea while little cartoon flames dance nearby.',
    'gag-tiny-violin': 'The world\'s tiniest violin plays a melodramatic lament.',
    'gag-no-thoughts': 'An unbothered rubber duck bobs along: no thoughts, just vibes.',
    'gag-hamster-brain': 'A tiny hamster keeps the one-brain-cell processing wheel turning.',
    'gag-touch-grass': 'A little patch of grass grows; a tiny robot hand gives it a pat.',
    'gag-task-failed': 'Task failed... successfully. An unearned checkmark celebrates.',
    'gag-social-battery': 'A nearly empty social battery recharges with one tiny heart.',
    'gag-bonk': 'A soft cartoon hammer delivers one harmless bonk and a wobbling star.',
    'gag-dramatic-zoom': 'An unnecessarily dramatic zoom into Robbi\'s astonished eyes.',
    'gag-achievement': 'An oversized celebration for the modest achievement: did a thing.',
}


EMOTION_EXPRESSIONS = {
    'affectionate-smile': 'Heart-shaped eyes and a small, warm smile.',
    'proud-smile': 'A quiet, satisfied smile with gently closed eyes.',
    'shy-smile': 'A little bashful smile and a downward gaze.',
    'excited-grin': 'Delighted eyes and a broad, excited grin.',
    'amazed-gasp': 'Wide eyes and a little astonished O mouth.',
    'thoughtful-look': 'Look up thoughtfully, then relax.',
    'skeptical-look': 'One lowered eyelid and a doubtful little look.',
    'confused-look': 'Uneven eyelids and a small puzzled mouth.',
    'sad-sigh': 'Drooping eyes and a gentle, brief frown.',
    'frustrated-frown': 'A mildly grumpy frown that relaxes back to neutral.',
}


FACE_EXPRESSIONS = {
    'natural-blink': 'A small, relaxed blink.',
    'double-blink': 'An attentive double blink.',
    'happy-blink': 'A happy smile with a quick blink.',
    'cheeky-wink': 'A little smile, a wink, then a relaxed face.',
    'happy-dance': 'A looping smile and delighted eyes for a dance.',
    'curious-glance': 'Look left, look right, and return to center.',
    'look-left': 'Glance left briefly and return to center.',
    'look-right': 'Glance right briefly and return to center.',
    'sleepy-yawn': 'Droop, yawn, and settle into a sleepy face.',
    'sleepy-breath': 'A slow, quiet resting expression.',
    'pickup-startle': 'Startle, then a small worried expression.',
    'soft-relief': 'Relax into a relieved smile after landing.',
    'mischievous-grin': 'A small amused grin and a wink.',
    **EMOTION_EXPRESSIONS,
}

_FACE_COMMANDS = {
    'blink': 'natural-blink', 'blinzle': 'natural-blink', 'blinzel': 'natural-blink',
    'double blink': 'double-blink', 'blink twice': 'double-blink',
    'wink': 'cheeky-wink', 'wink at me': 'cheeky-wink', 'zwinker': 'cheeky-wink',
    'look around': 'curious-glance', 'schau dich um': 'curious-glance',
    'look left': 'look-left', 'schau nach links': 'look-left',
    'look right': 'look-right', 'schau nach rechts': 'look-right',
    'yawn': 'sleepy-yawn', 'gähn': 'sleepy-yawn',
    'give me a cheeky grin': 'mischievous-grin', 'look mischievous': 'mischievous-grin',
    'show affection': 'affectionate-smile', 'look affectionate': 'affectionate-smile', 'schau verliebt': 'affectionate-smile',
    'look proud': 'proud-smile', 'schau stolz': 'proud-smile',
    'look shy': 'shy-smile', 'schau schüchtern': 'shy-smile',
    'look excited': 'excited-grin', 'schau aufgeregt': 'excited-grin',
    'look amazed': 'amazed-gasp', 'schau erstaunt': 'amazed-gasp',
    'look thoughtful': 'thoughtful-look', 'schau nachdenklich': 'thoughtful-look',
    'look skeptical': 'skeptical-look', 'schau skeptisch': 'skeptical-look',
    'look confused': 'confused-look', 'schau verwirrt': 'confused-look',
    'look sad': 'sad-sigh', 'schau traurig': 'sad-sigh',
    'look frustrated': 'frustrated-frown', 'schau genervt': 'frustrated-frown',
}
_GESTURE_COMMANDS = {
    'do a happy dance': 'happy-dance', 'happy dance': 'happy-dance', 'dance': 'happy-dance',
    'tanz': 'happy-dance', 'tanze': 'happy-dance',
    'give me a tiny wiggle': 'happy-wiggle', 'wiggle': 'happy-wiggle', 'happy wiggle': 'happy-wiggle',
    'shake your head': 'tiny-no', 'shake your head no': 'tiny-no', 'tiny no': 'tiny-no',
    'nod': 'tiny-nod', 'nod yes': 'tiny-nod', 'give me a little nod': 'tiny-nod',
    'take a little peek': 'curious-peek', 'curious peek': 'curious-peek',
    'do the shuffle': 'shuffle', 'shuffle': 'shuffle',
    'do an orbit dance': 'orbit-dance', 'orbit dance': 'orbit-dance',
}
_GAG_COMMANDS = {
    'show a loading gag': 'gag-buffering', 'show buffering': 'gag-buffering',
    'show deal with it': 'gag-deal-with-it', 'show this is fine': 'gag-this-is-fine',
    'play the tiny violin': 'gag-tiny-violin', 'show the tiny violin': 'gag-tiny-violin',
    'show no thoughts': 'gag-no-thoughts', 'show the hamster brain': 'gag-hamster-brain',
    'show touch grass': 'gag-touch-grass', 'show task failed successfully': 'gag-task-failed',
    'show social battery': 'gag-social-battery', 'show bonk': 'gag-bonk',
    'show dramatic zoom': 'gag-dramatic-zoom', 'show a tiny achievement': 'gag-achievement',
    'zeig die kleine geige': 'gag-tiny-violin', 'zeig den hamster': 'gag-hamster-brain',
    'zeig die sonnenbrille': 'gag-deal-with-it', 'zeig den ladebalken': 'gag-buffering',
}
_MEME_COMMANDS = {
    'show the support brick': 'meme-support-brick', 'play the support brick': 'meme-support-brick',
    'show the emotional support brick': 'meme-support-brick',
    'shred my optimism': 'meme-optimism-shredder', 'show the optimism shredder': 'meme-optimism-shredder',
    'play per my last beep': 'meme-last-beep', 'show per my last beep': 'meme-last-beep',
    'show premium empathy': 'meme-premium-empathy', 'show the empathy paywall': 'meme-premium-empathy',
    'show a certified overreaction': 'meme-overreaction', 'play the overreaction': 'meme-overreaction',
    'show family size audacity': 'meme-family-audacity', 'show the audacity can': 'meme-family-audacity',
    'show the problem composter': 'meme-problem-compost', 'compost my problem': 'meme-problem-compost',
    'zeig den trostziegel': 'meme-support-brick', 'zeig den optimismus schredder': 'meme-optimism-shredder',
    'zeig die überreaktion': 'meme-overreaction', 'zeig den problemkomposter': 'meme-problem-compost',
}
_MEME_ENCORE_THEMES = {
    'dumpster promotion': 'meme-dumpster-promotion', 'failing upward': 'meme-dumpster-promotion',
    'meeting escape': 'meme-meeting-escape', 'flying meeting': 'meme-meeting-escape',
    'brain tabs': 'meme-brain-tabs', 'too many tabs': 'meme-brain-tabs',
    'delulu launch': 'meme-delulu-launch', 'delusion propulsion': 'meme-delulu-launch',
    'last spoon': 'meme-last-spoon', 'tiny deadline': 'meme-tiny-deadline',
    'vibe check': 'meme-vibe-check', 'budget luxury': 'meme-budget-luxury',
    'overthinking': 'meme-overthinking', 'couch quest': 'meme-couch-quest',
    'side quest nap': 'meme-couch-quest',
}
_MEME_COMMANDS.update({f'{verb} {theme}': name
    for verb in ('show', 'show the', 'play', 'play the')
    for theme, name in _MEME_ENCORE_THEMES.items()})
_MEME_COMMANDS.update({
    'zeig die müllcontainer beförderung': 'meme-dumpster-promotion',
    'zeig das fliegende meeting': 'meme-meeting-escape',
    'zeig die gehirn tabs': 'meme-brain-tabs',
    'zeig den raketen toaster': 'meme-delulu-launch',
    'zeig den letzten löffel': 'meme-last-spoon',
    'zeig die schnecken deadline': 'meme-tiny-deadline',
    'zeig den vibe check': 'meme-vibe-check',
    'zeig das luxus budget': 'meme-budget-luxury',
    'zeig das overthinking': 'meme-overthinking',
    'zeig die sofa quest': 'meme-couch-quest',
})
_MEME_COMMANDS.update({f'{verb} {name.removeprefix("meme-").replace("-", " ")}': name
    for verb in ('show', 'show the', 'play', 'play the')
    for name in MEME_REFLECTIONS_ANIMATIONS})
_MEME_COMMANDS.update({
    'show the cactus boundary': 'meme-boundary-garden',
    'show everything is urgent': 'meme-priority-octopus',
    'show roots dont do pr': 'meme-invisible-progress',
    'zeig den kaktus zaun': 'meme-boundary-garden',
    'zeig die wetter kerze': 'meme-weather-candle',
    'zeig den prioritäten oktopus': 'meme-priority-octopus',
    'zeig die laufenden torpfosten': 'meme-moving-goalposts',
    'zeig den perfektionismus entwurf': 'meme-perfection-draft',
    'zeig den unsichtbaren fortschritt': 'meme-invisible-progress',
    'zeig den inneren kritiker': 'meme-inner-critic',
    'zeig die spätere antwort': 'meme-reply-later',
    'zeig die vergleichs uhren': 'meme-comparison-clocks',
    'zeig das planwechsel boot': 'meme-plot-twist-boat',
})
_AFTER_HOURS_THEMES = {'feature cactus': 'meme-feature-cactus', 'urgent laundry': 'meme-urgent-laundry', 'expectation luggage': 'meme-expectation-luggage', 'tomorrow vending': 'meme-tomorrow-vending', 'plot armor': 'meme-plot-armor', 'still mending': 'meme-still-mending', 'shrimp authority': 'meme-shrimp-authority', 'capypause': 'meme-capypause', 'hot take freezer': 'meme-hot-take-freezer', 'control tea': 'meme-control-tea', 'shrimp judge': 'meme-shrimp-authority', 'capybara pause': 'meme-capypause', 'control the tea': 'meme-control-tea'}
_MEME_COMMANDS.update({f'{verb} {theme}': name
    for verb in ('show', 'show the', 'play', 'play the')
    for theme, name in _AFTER_HOURS_THEMES.items()})
_SWEET_COMMANDS = {
    'show a flower gift': 'sweet-flower', 'show here have a flower': 'sweet-flower',
    'show the pocket hug': 'sweet-pocket-hug', 'play the pocket hug': 'sweet-pocket-hug',
    'show the little apology': 'sweet-sorry', 'show a small oops': 'sweet-sorry',
    'show the bigger half': 'sweet-cookie', 'show the last cookie': 'sweet-cookie',
    'show the rainy day umbrella': 'sweet-rain', 'show room under my umbrella': 'sweet-rain',
    'show human charging': 'sweet-rest', 'show the bedtime tuck in': 'sweet-rest',
    'zeig das blumengeschenk': 'sweet-flower', 'zeig die kleine umarmung': 'sweet-pocket-hug',
    'zeig die kleine entschuldigung': 'sweet-sorry', 'zeig den letzten keks': 'sweet-cookie',
    'zeig den regenschirm': 'sweet-rain', 'zeig die gute nacht animation': 'sweet-rest',
}
_POSTCARD_COMMANDS = {
    'show pocket sunshine': 'post-sunshine', 'show the sunshine jar': 'post-sunshine',
    'show the peace tea': 'post-peace-tea', 'show a cup of sorry': 'post-peace-tea',
    'show slow progress club': 'post-slow-club', 'show the little snail': 'post-slow-club',
    'show the heart mail': 'post-heart-mail', 'show priority heart mail': 'post-heart-mail',
    'show return to sender': 'post-return-guilt', 'show the guilt stamp': 'post-return-guilt',
    'show the soft landing': 'post-soft-landing', 'show the pillow plane': 'post-soft-landing',
    'zeig die sonnenschein postkarte': 'post-sunshine', 'zeig den friedenstee': 'post-peace-tea',
    'zeig die kleine schnecke': 'post-slow-club', 'zeig die herzpost': 'post-heart-mail',
    'zeig den zurück zum absender stempel': 'post-return-guilt', 'zeig die sanfte landung': 'post-soft-landing',
}

_SEASONAL_COMMANDS = {
    f'{verb} {theme} animation': 'season-'+theme.replace(' ', '-')
    for verb in ('show the', 'show', 'play the', 'play')
    for theme in ('birthday', 'christmas', 'halloween', 'easter', 'summer', 'autumn', 'winter', 'spring', 'new year')
}
_SEASONAL_COMMANDS.update({f'zeig die {theme} animation': name for theme, name in [
    ('geburtstags', 'season-birthday'), ('weihnachts', 'season-christmas'),
    ('halloween', 'season-halloween'), ('oster', 'season-easter'),
    ('sommer', 'season-summer'), ('herbst', 'season-autumn'),
    ('winter', 'season-winter'), ('frühlings', 'season-spring'), ('neujahrs', 'season-new-year'),
]})
_ENCORE_THEMES = {
    'valentine': 'season-valentine', 'valentines': 'season-valentine',
    'valentines day': 'season-valentine', 'lunar new year': 'season-lunar-new-year',
    'carnival': 'season-carnival', 'lucky clover': 'season-lucky-clover',
    'st patricks day': 'season-lucky-clover', 'saint patricks day': 'season-lucky-clover',
    'earth day': 'season-earth-day', 'summer picnic': 'season-summer-picnic',
    'autumn harvest': 'season-autumn-harvest', 'winter solstice': 'season-winter-solstice',
    'april fools': 'season-april-fools', 'oktoberfest': 'season-oktoberfest',
    'diwali': 'season-diwali', 'hanukkah': 'season-hanukkah', 'chanukah': 'season-hanukkah',
}
_SEASONAL_COMMANDS.update({f'{verb} {theme} animation': name
    for verb in ('show', 'show the', 'play', 'play the')
    for theme, name in _ENCORE_THEMES.items()})
_SEASONAL_COMMANDS.update({f'zeig die {theme} animation': name for theme, name in [
    ('valentinstags', 'season-valentine'), ('mondneujahrs', 'season-lunar-new-year'),
    ('karnevals', 'season-carnival'), ('kleeblatt', 'season-lucky-clover'),
    ('tag der erde', 'season-earth-day'), ('sommerpicknick', 'season-summer-picnic'),
    ('herbsternte', 'season-autumn-harvest'), ('wintersonnenwende', 'season-winter-solstice'),
    ('aprilscherz', 'season-april-fools'), ('oktoberfest', 'season-oktoberfest'),
    ('diwali', 'season-diwali'), ('chanukka', 'season-hanukkah'),
]})
_ADVENT_NUMBERS = {str(n): n for n in range(1, 25)}
_ADVENT_NUMBERS.update({word: n for n, word in enumerate([
    'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten',
    'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen',
    'eighteen', 'nineteen', 'twenty', 'twenty one', 'twenty two', 'twenty three', 'twenty four'], 1)})
_ADVENT_NUMBERS.update({word: n for n, word in enumerate([
    'eins', 'zwei', 'drei', 'vier', 'fünf', 'sechs', 'sieben', 'acht', 'neun', 'zehn',
    'elf', 'zwölf', 'dreizehn', 'vierzehn', 'fünfzehn', 'sechzehn', 'siebzehn',
    'achtzehn', 'neunzehn', 'zwanzig', 'einundzwanzig', 'zweiundzwanzig', 'dreiundzwanzig', 'vierundzwanzig'], 1)})


def local_expression_request(host, text):
    if 'expressions' not in host.packages.projects or not host.enabled('expressions'):
        return None
    command = re.sub(r'[^\w\s-]', '', str(text).casefold()).replace('-', ' ')
    command = ' '.join(command.split())
    command = re.sub(r'^(?:robbi )?(?:please |bitte )?', '', command)
    command = re.sub(r' (?:please|bitte)$', '', command)
    if command in _FACE_COMMANDS:
        return {'app': 'expressions', 'tool': 'animate', 'arguments': {'name': _FACE_COMMANDS[command]}}
    if command in _GAG_COMMANDS:
        return {'app': 'expressions', 'tool': 'gag', 'arguments': {'name': _GAG_COMMANDS[command]}}
    if command in _MEME_COMMANDS:
        return {'app': 'expressions', 'tool': 'meme', 'arguments': {'name': _MEME_COMMANDS[command], 'with_audio': True}}
    if command in _SWEET_COMMANDS:
        return {'app': 'expressions', 'tool': 'kindness', 'arguments': {'name': _SWEET_COMMANDS[command], 'with_audio': True}}
    if command in _POSTCARD_COMMANDS:
        return {'app': 'expressions', 'tool': 'postcard', 'arguments': {'name': _POSTCARD_COMMANDS[command], 'with_audio': True}}
    if command in _SEASONAL_COMMANDS:
        return {'app': 'expressions', 'tool': 'seasonal', 'arguments': {'name': _SEASONAL_COMMANDS[command], 'with_audio': True}}
    advent = re.fullmatch(r'(?:show advent day|play advent day|open advent day|zeig advent tag|öffne advent türchen|zeig advent türchen) (.+)', command)
    # Punctuation cleanup must not turn a signed/fractional day into another day.
    malformed_day = re.search(r'[-+]\s*\d|\d\s*[.,]\s*\d', str(text))
    if advent and not malformed_day and advent[1] in _ADVENT_NUMBERS:
        return {'app': 'expressions', 'tool': 'advent', 'arguments': {'day': _ADVENT_NUMBERS[advent[1]], 'with_audio': True}}
    preview = command.startswith('preview ')
    gesture = _GESTURE_COMMANDS.get(command.removeprefix('preview '))
    if gesture:
        return {'app': 'expressions', 'tool': 'perform', 'arguments': {
            'name': gesture, 'with_motion': not preview, 'with_audio': True}}
    return None
