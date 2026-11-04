"""Project-local implementation. Shared runtime access uses only the supported SDK."""

from copy import deepcopy

from datetime import datetime, timedelta, timezone

import re

import uuid

from robbi.errors import ValidationError

def now():
    return datetime.now(timezone.utc)

LESSONS = {
    "python": [
        ("Names and values", "A Python variable is a name referring to a value. In score = 3, score refers to the integer 3. The statement score = score + 2 evaluates the right side first, then binds score to the new value. A quoted value such as '3' is text, not an integer. Try predicting each line before running it.",
         "After score = 3 and score = score + 2, what is score?", "5", "Write a second example starting at 10. Explain why '3' + '2' produces text rather than the number 5."),
        ("Choose with conditions", "An if statement runs an indented block when its condition is true. else handles the other case. Equality is written ==; assignment is written =. For example, if temperature < 10: can select a warm-coat reminder. Test a condition with a value below, at, and above its boundary.",
         "Which operator tests equality: = or ==?", "==", "Sketch an if/else rule for whether a shopping list is empty. Test both branches and the exact boundary."),
        ("Repeat with loops", "A for loop visits each item in an iterable. For item in ['tea', 'milk'] visits tea and then milk. range(3) supplies 0, 1 and 2; its stopping value is excluded. Loops reduce repeated code, but each pass still needs a clear purpose. An accumulator starts with a value, then updates during each pass.",
         "What values does range(3) produce?", "0, 1, 2", "Trace total = 0 followed by a loop adding each number in [2, 4, 6]. Write the total after every pass."),
    ],
    "german": [
        ("A polite request", "Bitte can mean please or you're welcome, depending on context. Ich möchte ... is a useful polite opening meaning I would like ... . For example: Ich möchte einen Tee, bitte. Read the sentence slowly, then substitute a different drink. Focus on recalling the whole useful phrase before analysing every ending.",
         "How do you begin 'I would like ...' in German?", "Ich möchte ...", "Say three polite requests aloud. Put the English prompt out of sight and recall the German opening."),
        ("Where is it?", "Wo means where. Wo ist der Bahnhof? asks where the train station is. Hier means here and dort means there. German nouns are capitalised, including Bahnhof. Practise pairing a real object or place with a short question; a concrete situation makes retrieval easier.",
         "What does 'Wo?' mean?", "Where?", "Make two 'Wo ist ...?' questions using words you already know. Answer one with hier and one with dort."),
        ("Today and tomorrow", "Heute means today, morgen means tomorrow, and gestern means yesterday. These words help anchor simple plans. Morgen can also mean morning when used as the capitalised noun in expressions such as am Morgen. Context matters: morgen alone commonly points to the next day.",
         "What is the German word for yesterday?", "gestern", "Recall heute, morgen and gestern without looking. Give each word a gesture or an example from your week."),
    ],
    "science": [
        ("A fair comparison", "An experiment compares conditions to investigate a question. Change the factor you are testing while keeping relevant other conditions as similar as possible. Repeated observations help distinguish a pattern from random variation. A single surprising result is a reason to investigate, not a guarantee that your explanation is right.",
         "Why keep other conditions similar when testing one change?", "To reduce alternative explanations for the observed difference.", "Design a paper-airplane comparison. Name the one feature you would change, what you would measure, and what you would hold constant."),
        ("Energy changes form", "Energy can be transferred between objects and transformed between forms. A battery-powered lamp transforms stored chemical energy into electrical energy and then light and heat. Useful output is often accompanied by heating. An energy diagram traces transfers; it does not require energy to vanish when a device stops being useful.",
         "Besides light, what common output does a lamp produce?", "Heat.", "Draw the energy transfers in a moving bicycle or a kettle. Include one output that is not the device's main purpose."),
        ("Evaporation", "Liquid water can evaporate below its boiling point. Molecules leave the liquid surface and become water vapour. Air movement and a larger exposed surface often help drying by carrying vapour away or exposing more liquid. Compare damp cloths under similar conditions rather than treating one observation as proof.",
         "Must water boil before any of it can evaporate?", "No. Evaporation can occur below the boiling point.", "Predict which will dry faster: equal water spread thinly or kept in a narrow cup. Explain your prediction and identify another factor to control."),
    ],
    "history": [
        ("Ask who made the source", "A historical source is evidence from which we investigate the past. Ask who created it, when, for whom, and for what purpose. A diary, law, photograph and advertisement can reveal different things. A biased source can still be useful evidence of the author's priorities; bias does not make every detail false.",
         "Name two questions to ask about a historical source.", "For example: who made it, when, for whom, and why?", "Choose a family photograph or old advertisement. Separate what it directly shows from what you are inferring."),
        ("Cause and consequence", "Large historical changes usually have several contributing causes. A trigger can begin an event without explaining all the conditions that made it possible. Consequences can be intended or unintended and can differ between groups. A useful explanation connects evidence to a claim instead of only listing dates.",
         "Is an event's immediate trigger necessarily its only cause?", "No. Longer-term conditions and several causes may contribute.", "Pick a change you know, such as a new transport system. List a trigger, a background condition and two different groups affected."),
        ("Change and continuity", "History investigates what changed and what stayed similar. A new technology can alter some jobs quickly while habits and institutions change slowly. Avoid assuming everyone experienced a period in the same way. Compare a specific place, group and time range, then explain where your evidence is incomplete.",
         "What does continuity mean when comparing historical periods?", "Something that stayed similar across the periods being compared.", "Compare communication in two generations of your family. List two changes and one continuity, then identify who you could ask for evidence."),
    ],
}

def _deck(state, topic):
    if topic in LESSONS:
        return [{"id": f"{topic}-{i + 1}", "topic": topic, "title": item[0], "lesson": item[1],
                 "question": item[2], "answer": item[3], "practice": item[4]} for i, item in enumerate(LESSONS[topic])]
    return [item for item in state["cards"] if item["topic"].casefold() == topic.casefold()]

def _valid(condition, message):
    if not condition:
        raise ValidationError(message + "; restore the saved state before editing")

def _text(value, maximum, *, required=False):
    return isinstance(value, str) and len(value) <= maximum and value == value.strip() and (not required or bool(value))

def _state(context):
    saved = context.read_state()
    if saved == {}:
        return {"version": 1, "cards": [], "reviews": {}, "current": None}
    _valid(set(saved) == {"version", "cards", "reviews", "current"}
           and type(saved["version"]) is int and saved["version"] == 1
           and isinstance(saved["cards"], list) and len(saved["cards"]) <= 30
           and isinstance(saved["reviews"], dict) and len(saved["reviews"]) <= 50,
           "Learning data is invalid")
    card_fields = {"id", "topic", "title", "lesson", "question", "answer", "practice"}
    topics_by_id = {f"{topic}-{i + 1}": topic for topic, cards in LESSONS.items() for i in range(len(cards))}
    for card in saved["cards"]:
        _valid(isinstance(card, dict) and set(card) == card_fields, "A saved learning card is invalid")
        _valid(_text(card["id"], 12) and bool(re.fullmatch(r"[0-9a-f]{12}", card["id"]))
               and card["id"] not in topics_by_id, "A saved learning card ID is invalid or duplicated")
        for key, maximum in (("topic", 40), ("title", 100), ("lesson", 500), ("question", 300), ("answer", 500), ("practice", 200)):
            _valid(_text(card[key], maximum, required=key in {"topic", "title", "question", "answer"}),
                   "A saved learning card is invalid")
        _valid(card["topic"] == card["topic"].casefold() and card["topic"] not in LESSONS,
               "A saved learning topic is invalid")
        topics_by_id[card["id"]] = card["topic"]
    for identifier, review in saved["reviews"].items():
        _valid(identifier in topics_by_id and isinstance(review, dict)
               and set(review) == {"interval_days", "due", "attempts", "difficult", "last_grade"},
               "Saved learning progress refers to an invalid card or record")
        _valid(type(review["interval_days"]) is int and 0 <= review["interval_days"] <= 60
               and type(review["attempts"]) is int and 1 <= review["attempts"] <= 1000000
               and type(review["difficult"]) is int and 0 <= review["difficult"] <= review["attempts"]
               and isinstance(review["last_grade"], str) and review["last_grade"] in {"again", "hard", "good", "easy"}
               and (review["interval_days"] == 0) == (review["last_grade"] == "again"),
               "Saved learning progress is invalid")
        _timestamp(review["due"])
    current = saved["current"]
    if current is not None:
        _valid(isinstance(current, dict) and set(current) == {"topic", "id", "revealed"}
               and isinstance(current["id"], str) and current["id"] in topics_by_id
               and current["topic"] == topics_by_id[current["id"]] and type(current["revealed"]) is bool,
               "Saved learning session is invalid")
    return saved

def _timestamp(value):
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError()
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise ValidationError("Saved review time is invalid") from None

def _new_card_id(cards):
    existing = {card["id"] for card in cards}
    for _ in range(10):
        identifier = uuid.uuid4().hex[:12]
        if identifier not in existing:
            return identifier
    raise ValidationError("Could not allocate a distinct card ID; try again")

def _result(text, **data):
    return {"ok": True, "text": text, "data": data}

def _lesson(state):
    current = state.get("current")
    if not current:
        raise ValidationError("Start a lesson or review first")
    card = next((item for item in _deck(state, current["topic"]) if item["id"] == current["id"]), None)
    if card is None:
        raise ValidationError("The active card is no longer available; start another lesson")
    public = {key: value for key, value in card.items() if key != "answer" or current["revealed"]}
    public["revealed"] = current["revealed"]
    text = f"{card['title']}. {card['lesson']}\n\nRecall: {card['question']}\n\nPractice: {card['practice']}"
    if current["revealed"]:
        text += f"\n\nAnswer: {card['answer']} Rate your recall: again, hard, good or easy."
    else:
        text += "\n\nTry recalling the answer, then choose Reveal. Spend about five minutes reading, recalling and practising."
    return _result(text, lesson=public, minutes=5)

def run(context, tool, args):
    context.checkpoint()
    state = _state(context)
    instant = now()
    if tool == "study":
        action = args.get("action", "topics")
        if action == "topics":
            topics = sorted(set(LESSONS) | {card["topic"] for card in state["cards"]})
            return _result("Choose a topic: " + ", ".join(topics) + ". You can also add your own cards.", topics=topics)
        if action in {"start", "review"}:
            topic = args.get("topic", "python").strip().casefold()
            cards = _deck(state, topic)
            if not cards:
                raise ValidationError("That topic has no cards. Choose a listed topic or add a custom card")
            due = [card for card in cards if card["id"] in state["reviews"] and _timestamp(state["reviews"][card["id"]]["due"]) <= instant]
            new = [card for card in cards if card["id"] not in state["reviews"]]
            choices = sorted(due, key=lambda card: _timestamp(state["reviews"][card["id"]]["due"])) + (new if action == "start" else [])
            if not choices:
                dates = [review["due"] for key, review in state["reviews"].items() if key in {card["id"] for card in cards}]
                return _result("No reviews are due for this topic." + (" Choose Start for a new lesson." if new else ""), next_review=min(dates, key=_timestamp) if dates else None)
            state["current"] = {"id": choices[0]["id"], "topic": topic, "revealed": False}
            context.write_state(state)
            return _lesson(state)
        if action == "status":
            return _lesson(state)
        if action == "reveal":
            _lesson(state)
            state["current"]["revealed"] = True
            context.write_state(state)
            return _lesson(state)
        if action == "grade":
            _lesson(state)
            current = state["current"]
            if not current["revealed"]:
                raise ValidationError("Reveal and compare the answer before rating your recall")
            grade = args.get("grade")
            if grade not in {"again", "hard", "good", "easy"}:
                raise ValidationError("Explicitly rate your recall: again, hard, good or easy")
            previous = state["reviews"].get(current["id"], {"interval_days": 0, "attempts": 0, "difficult": 0})
            if previous["attempts"] >= 1000000:
                raise ValidationError("This card has reached its review-count limit")
            old_interval = previous["interval_days"]
            interval = {"again": 0, "hard": max(1, old_interval), "good": max(1, old_interval * 2), "easy": max(3, old_interval * 3)}[grade]
            interval = min(60, interval)
            try:
                due = instant + (timedelta(minutes=10) if grade == "again" else timedelta(days=interval))
            except OverflowError:
                raise ValidationError("The next review would be outside the supported calendar range") from None
            state["reviews"][current["id"]] = {"due": due.isoformat(), "interval_days": interval,
                "attempts": previous.get("attempts", 0) + 1, "difficult": previous.get("difficult", 0) + int(grade in {"again", "hard"}), "last_grade": grade}
            state["current"] = None
            context.write_state(state)
            return _result("Recall recorded. Review again in " + ("10 minutes." if not interval else f"{interval} day(s)."), due=due.isoformat(), grade=grade)
    elif tool == "cards":
        action = args.get("action", "list")
        if action == "list":
            return _result(f"{len(state['cards'])} custom learning cards.", cards=state["cards"])
        identifier = args.get("id", "")
        existing = next((card for card in state["cards"] if card["id"] == identifier), None)
        if action == "delete":
            if existing is None:
                raise ValidationError("Choose an existing custom card ID")
            state["cards"].remove(existing)
            state["reviews"].pop(identifier, None)
            if state.get("current", {}) and state["current"]["id"] == identifier:
                state["current"] = None
        elif action == "save":
            if identifier and existing is None:
                raise ValidationError("That custom card ID was not found")
            if existing is None and len(state["cards"]) >= 30:
                raise ValidationError("Thirty custom cards are saved; remove one before adding another")
            card = deepcopy(existing) if existing else {"id": _new_card_id(state["cards"]), "topic": "my-notes", "title": "", "lesson": "", "question": "", "answer": "", "practice": "Recall the answer without looking, then explain it in your own words."}
            for key in ("topic", "title", "lesson", "question", "answer", "practice"):
                if key in args:
                    card[key] = args[key].strip()
            card["topic"] = card["topic"].casefold()
            if len(card["topic"]) > 40:
                raise ValidationError("The normalized custom topic must contain at most 40 characters")
            if card["topic"] in LESSONS or not all(card[key] for key in ("topic", "title", "question", "answer")):
                raise ValidationError("Provide a custom topic, title, question and answer; built-in topic names are reserved")
            if existing:
                state["cards"][state["cards"].index(existing)] = card
                state["reviews"].pop(identifier, None)
                if state.get("current") and state["current"]["id"] == identifier:
                    state["current"] = None
            else:
                state["cards"].append(card)
            identifier = card["id"]
        else:
            raise ValidationError("Unknown card action")
        context.write_state(state)
        return _result("Custom card " + ("removed." if action == "delete" else "saved. Previous review scheduling was reset for edited content."), id=identifier)
    elif tool == "progress":
        reviews = state["reviews"]
        due = [key for key, value in reviews.items() if _timestamp(value["due"]) <= instant]
        difficult = [key for key, value in reviews.items() if value.get("last_grade") in {"again", "hard"}]
        return _result(f"{len(reviews)} cards studied; {len(due)} reviews due; {len(difficult)} cards last rated difficult.", studied=len(reviews), due=due, difficult=difficult, reviews=reviews)
    raise ValidationError("Unknown learning action")
