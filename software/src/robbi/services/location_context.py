"""Conservative location selection from actual geocoder/forecast receipts."""
import math
import re
import unicodedata


def normalized(value):
    value = unicodedata.normalize('NFKD', str(value).casefold())
    return ' '.join(re.findall(r'[a-z0-9]+', ''.join(c for c in value if not unicodedata.combining(c))))


def dominant_city(choices, query):
    """Prefer an exact named settlement over districts; never pick an arbitrary hit."""
    exact = [p for p in choices if p.get('kind') == 'city' and normalized(p.get('locality')) == normalized(query)]
    if len(exact) == 1 and not any(p.get('kind') == 'unknown' and normalized(p.get('locality')) == normalized(query) for p in choices):
        return exact[0]
    if len(exact) > 1:
        ranked = sorted(exact, key=lambda p: p.get('population', 0), reverse=True)
        # A globally duplicated name still requires clarification unless one city
        # overwhelmingly dominates. Do not interpret missing population as zero.
        if (all(p.get('population', 0) > 0 for p in ranked) and ranked[0]['population'] >= 100000
                and ranked[0]['population'] >= 20 * ranked[1]['population']):
            return ranked[0]
    return None


def confirmed_location(text, choices):
    if not isinstance(choices, list):
        return None
    text = normalized(text)
    if len(text) > 200 or re.search(r'\b(not|no|dont|never|nicht|nein|stop|cancel)\b', text):
        return None
    words = set(text.split())
    filler = {'yes', 'yeah', 'yep', 'please', 'city', 'town', 'stadt', 'the', 'in', 'of',
              'canton', 'canon', 'kanton', 'i', 'mean', 'meant', 'ja', 'bitte', 'die', 'der', 'im'}
    wanted = words - filler
    if not wanted:
        return None  # A bare yes is insufficient when several choices remain.
    matches = []
    canton = re.search(r'\b(?:canton|canon|kanton)(?: of)? ([a-z0-9]+)', text)
    for place in choices:
        if not isinstance(place, dict):
            continue
        if words & {'city', 'town', 'stadt'} and place.get('kind') == 'region':
            continue
        name = normalized(place.get('name', ''))
        locality = normalized(place.get('locality') or str(place.get('name', '')).split(',')[0])
        if canton:
            region = place.get('admin1') or (str(place.get('name', '')).split(',') + ['', ''])[1]
            if canton[1] not in normalized(region).split():
                continue
        if wanted <= set(name.split()) and set(locality.split()) <= wanted:
            matches.append(place)
    return matches[0] if len(matches) == 1 else None


def coordinates(place):
    if not isinstance(place, dict):
        return None
    for key, limit in [('latitude', 90), ('longitude', 180)]:
        value = place.get(key)
        if type(value) not in (float, int) or not math.isfinite(value) or abs(value) > limit:
            return None
    if not isinstance(place.get('timezone'), str) or not place['timezone']:
        return None
    values = {key: place[key] for key in ('latitude', 'longitude', 'timezone')}
    values['location'] = str(place.get('name', ''))[:160]
    if place.get('country_code'):
        values['country_code'] = place['country_code']
    return values


def weather_followup(text, receipt, last_reply=''):
    """Return fresh read arguments, never cached weather or guessed coordinates."""
    if not receipt or not receipt.get('arguments_complete'):
        return None
    call = receipt.get('arguments', {})
    if not isinstance(call, dict):
        return None
    if receipt.get('tool') != 'app_tool' or call.get('app') != 'weather' or call.get('tool') != 'forecast':
        return None
    result = receipt.get('result', {})
    data = result.get('data', {})
    if not isinstance(data, dict):
        return None
    if data.get('needs_location'):
        place = confirmed_location(text, data.get('choices', []))
    elif result.get('ok') is True and re.search(r'\b(weather|wetter)\b', normalized(text)) and re.search(r'\b(there|dort)\b', normalized(text)):
        place = data.get('location')
        locality = str((place or {}).get('name', '')).split(',')[0] if isinstance(place, dict) else ''
        if not locality or normalized(locality) not in normalized(last_reply):
            return None  # Another topic/location may have become the referent.
    else:
        return None
    resolved = coordinates(place)
    if resolved is None:
        return None
    arguments = {**call.get('arguments', {}), **resolved}
    words = set(normalized(text).split())
    for day, aliases in [('today', {'today', 'heute'}), ('tomorrow', {'tomorrow', 'morgen'})]:
        if words & aliases:
            arguments['day'] = day
    return {'app_tool': {'app': 'weather', 'tool': 'forecast', 'arguments': arguments}}
