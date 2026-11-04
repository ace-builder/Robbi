"""Bounded live dashboard transcript, independent of prompt memory and audio.

Typed turns remain scoped to their browser tab. Local microphone turns are shared
with the owner dashboard. Background results are visible even when sound is muted.
"""
from collections import OrderedDict
from functools import wraps
from contextvars import ContextVar
import threading
import time
import uuid
import json
import logging
import re
from robbi.engine.state_io import atomic_json

_ACTIVE_TURN = ContextVar('robbi_conversation_feed_turn', default=None)


def social_assessment(controller, action):
    """Consume optional model metadata, never expose it as an executable action."""
    if not isinstance(action,dict):return
    signal=action.pop('social_signal',None)
    active=_ACTIVE_TURN.get()
    mood=getattr(getattr(controller,'behavior',None),'social_state',None)
    if signal is not None and active and mood:
        feed,turn=active
        with feed._lock:row=feed._turns.get(turn)
        if row:mood.observe(row['text'],turn,signal)


def finish_followup(result):
    """Async children keep the exact turn, including browser-session isolation."""
    active = _ACTIVE_TURN.get()
    if active is not None:
        feed, turn_id = active
        feed.finish(turn_id, result, followup=True)


def spoken_text(result):
    if not isinstance(result, dict):
        return ""
    parts = [str(result.get("speak") or result.get("message") or "")]
    for key in ("action", "next"):
        if isinstance(result.get(key), dict):
            parts.append(spoken_text(result[key]))
    for step in result.get("sequence", []) if isinstance(result.get("sequence"), list) else []:
        parts.append(spoken_text(step))
    return "\n".join(part for part in parts if part)[:12000]


class ConversationFeed:
    def __init__(self, limit=60, path=None):
        self._turns = OrderedDict()
        self._limit = limit
        self._lock = threading.RLock()
        self.path=path;self.persistence_error=''
        try:
            if path is not None and path.exists():
                if path.stat().st_size>5_000_000:raise ValueError('Oversized transcript')
                rows=json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(rows,list):raise ValueError('Invalid transcript')
                for row in rows[-limit:]:
                    if not isinstance(row,dict) or row.get('source') not in ('ui_text','speech_input'):continue
                    if not all(isinstance(row.get(k),str) for k in ('id','text','reply','state','conversation_id')):continue
                    row=dict(row,text=row['text'][:12000],reply=row['reply'][:12000])
                    if row['state'] in ('thinking','working'):
                        row.update(state='interrupted',task_id=None,reply=(row['reply']+'\nInterrupted by restart; actions were not resumed.').strip())
                    self._turns[row['id']]=row
        except (OSError,ValueError,TypeError):self.persistence_error='Saved transcript unavailable; starting with an empty feed.'

    def _save(self):
        if self.path is None:return
        try:atomic_json(self.path,list(self._turns.values()));self.persistence_error=''
        except (OSError,ValueError):
            self.persistence_error='Conversation is visible but could not be saved.'
            logging.getLogger(__name__).warning(self.persistence_error)

    def context(self,text,source,conversation_id=''):
        with self._lock:
            rows=[dict(r) for r in self._turns.values() if r['source']==source and
                  (source=='speech_input' or r['conversation_id']==conversation_id) and r['reply']]
        recent=rows[-5:];words=set(re.findall(r'\w{4,}',text.casefold()))-{'what','that','this','your','have','which','were','about','said','tell','please'}
        older=[r for r in rows[:-5] if words & set(re.findall(r'\w{4,}',(r['text']+' '+r['reply']).casefold()))][-1:]
        if older:recent=recent[-4:]
        messages=[]
        for row in older+recent:
            messages.extend([{'role':'user','content':row['text'][:500]},
                {'role':'assistant','content':row['reply'][:500]}])
        return messages

    def begin(self, text, source, conversation_id=""):
        if source not in {"ui_text", "speech_input"}:
            return None
        turn_id = uuid.uuid4().hex
        with self._lock:
            self._turns[turn_id] = dict(id=turn_id, source=source, conversation_id=conversation_id,
                created_at=time.time(), text=str(text)[:12000], reply="", state="thinking", task_id=None)
            while len(self._turns) > self._limit:
                self._turns.popitem(last=False)
            self._save()
        return turn_id

    def finish(self, turn_id, result, *, followup=False):
        if turn_id is None:
            return
        result = result if isinstance(result, dict) else {}
        task = result.get("background_task")
        task_id = task.get("id") if isinstance(task, dict) else None
        with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None:
                return
            if turn.get('_followup_complete') and not followup:
                return  # Fast follow-up won the race with the initial acknowledgement.
            if followup:
                turn['_followup_complete'] = True
            turn.update(reply=spoken_text(result), task_id=task_id,
                        state="working" if task_id else "failed" if result.get("error") else
                              "cancelled" if result.get("cancelled") else "completed")
            if not turn["reply"]:
                turn["reply"] = ("Request failed. Check Robbi's diagnostics and try again." if result.get("error") else
                                 "Request cancelled." if result.get("cancelled") else "Request completed.")
            self._save()

    def snapshot(self, conversation_id="", read_task=None):
        with self._lock:
            visible = [dict(turn) for turn in self._turns.values()
                       if turn["source"] == "speech_input" or turn["conversation_id"] == conversation_id]
        # Resolve outside the lock; a slow store must not block new microphone input.
        for turn in visible:
            if turn["state"] != "working" or not read_task:
                continue
            try:
                task = read_task(turn["task_id"])
            except KeyError:
                task = {"status": "failed", "error": "This background result is no longer available."}
            except Exception:
                continue  # Transient storage failure; keep the last known state.
            if task["status"] in {"queued", "running"}:
                if task.get('progress'):
                    turn['reply'] = task['progress']
                continue
            turn.update(state=task["status"], reply=spoken_text(task.get("result")) or
                        str(task.get("error") or "Background task finished.")[:12000])
            with self._lock:
                current = self._turns.get(turn["id"])
                if current and current["task_id"] == turn["task_id"]:
                    current.update(state=turn["state"], reply=turn["reply"])
                    self._save()
        selected, chars = [], 0
        for row in reversed(visible):
            size = len(row["text"]) + len(row["reply"])
            if selected and chars + size > 48000:
                break
            selected.append({key: value for key, value in row.items() if key != "conversation_id" and not key.startswith('_')})
            chars += size
        return {"turns": list(reversed(selected)), "history_limit": self._limit,'persistence_error':self.persistence_error}


def publish_conversation(callback):
    @wraps(callback)
    def invoke(self, text, include_photo=None, source="ui_text", conversation_id=""):
        feed = getattr(self, "conversation_feed", None)
        turn = feed.begin(text, source, conversation_id) if feed else None
        token = _ACTIVE_TURN.set((feed, turn) if feed and turn else None)
        try:
            mood=getattr(getattr(self,'behavior',None),'social_state',None)
            if mood and turn and not getattr(self,'_filming_mode',False):mood.observe(text,turn)
            result = callback(self, text, include_photo=include_photo, source=source, conversation_id=conversation_id)
        except Exception:
            if feed:
                feed.finish(turn, {"error": "failed", "speak": "This request failed. Check Robbi's diagnostics and try again."})
            raise
        finally:
            _ACTIVE_TURN.reset(token)
        if feed:
            feed.finish(turn, result)
        return result
    return invoke
