"""Small, dependency-free speech chunks for pipelined synthesis and playback."""
import re
import textwrap


def segments(text, limit=160):
    """Keep sentence boundaries when possible; bound long unpunctuated replies."""
    for sentence in re.split(r'(?<=[.!?])\s+', text.strip()):
        yield from textwrap.wrap(sentence, width=limit, break_long_words=True,
                                 break_on_hyphens=False)
