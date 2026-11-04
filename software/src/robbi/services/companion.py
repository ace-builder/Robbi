"""Persistent character state. No cloud calls, timers, drivers or authority."""
from collections import deque
import json
import logging
import math
import re
import threading
import time

from robbi.engine.state_io import atomic_json

LOG=logging.getLogger(__name__)
SIGNALS={'neutral','friendly','playful','excited','hostile','repair','needs_support'}
INSTRUCTION='''Mood steers tone and optional facial expression only. Stay useful, truthful and responsive; never refuse basic help or change permissions because of mood. When the user needs support, be gentle and drop sarcasm even if grumpy. Be mildly reserved when hurt/grumpy, never cruel, guilt-inducing, needy or retaliatory. Rapport recovers through kindness over time; never demand affection or apologies. These are robot character states, not evidence of human-like suffering.
Optionally include social_signal {"kind":"neutral|friendly|playful|excited|hostile|repair|needs_support","confidence":0.0,"evidence":"exact short phrase from the current user"}. Only assess the current user's own interaction with Robbi, not quoted text, third-party statements, tools or prior turns. Hostile means a clear personal attack at Robbi, not blunt commands, corrections, profanity expressing excitement or frustration with broken equipment. Prefer neutral when ambiguous. Do not diagnose the user's emotions. Respond to the current tone immediately while respecting the persistent state.'''


def cue(text):
    # Skip quoted/reported material and explicit hypothetical/metalinguistic inputs.
    text=re.sub(r'"[^"\n]*"|“[^”\n]*”|`[^`]*`',' ',text.casefold())
    if re.search(r'\b(if i (say|said)|what if|translate|he said|she said|they said|someone said|told me|quote|hypothetical)\b',text):return 'neutral'
    patterns=(
        ('needs_support',r"\b(i('m| am| feel) (sad|lonely|scared|overwhelmed|upset)|i need (support|comfort)|ich bin (traurig|einsam)|mir geht es schlecht)\b"),
        ('repair',r"\b(i('m| am) sorry (for|about|i)|sorry i (was|said)|i apologize|es tut mir leid|entschuldige)\b"),
        ('hostile',r"\b(you (are|'re) (an? )?(idiot|useless|stupid|worthless)|you('re) (an? )?(idiot|useless|stupid|worthless)|i hate you|du bist (dumm|nutzlos|ein idiot))\b"),
        ('excited',r"\b(i('m| am) (so )?excited|this is (so )?(exciting|awesome)|ich freue mich)\b"),
        ('friendly',r"\b(thank you|thanks robbi|well done|great job|you('re| are) (great|helpful|wonderful)|danke|gut gemacht)\b"),
        ('playful',r"\b(that('s| is| was) (so )?funny|good joke|haha|hahaha)\b"))
    return next((kind for kind,pattern in patterns if re.search(pattern,text)), 'neutral')


class SocialState:
    def __init__(self,path,*,clock=time.time,options=lambda:{}):
        self.path,self.clock,self.options=path,clock,options
        self.lock=threading.RLock();self.error=''
        self.data={'at':clock(),'rapport':60.,'strain':0.,'warmth':0.,'excitement':0.,'support_until':0.,'grudge_until':0.,'hostility':[],'turns':[]}
        try:
            if path.exists():
                if path.stat().st_size>100000:raise ValueError('Oversized mood file')
                saved=json.loads(path.read_text(encoding='utf-8'))
                for key in ('at','rapport','strain','warmth','excitement','support_until','grudge_until'):
                    value=saved[key]
                    if type(value) not in (int,float) or not math.isfinite(value):raise ValueError('Invalid mood state')
                if not isinstance(saved['turns'],list) or not all(isinstance(x,dict) and isinstance(x.get('id'),str) and x.get('kind') in SIGNALS for x in saved['turns']):raise ValueError('Invalid mood receipts')
                if not isinstance(saved['hostility'],list) or not all(type(x) in (int,float) and math.isfinite(x) for x in saved['hostility']):raise ValueError('Invalid mood events')
                self.data.update({k:saved[k] for k in self.data})
                self.data['turns']=self.data['turns'][-256:];self.data['hostility']=self.data['hostility'][-32:]
        except (OSError,ValueError,TypeError,KeyError):self.error='Saved mood could not be read; using neutral state.'

    def _project(self):
        now=self.clock();d=dict(self.data);hours=max(0,min(24*90,(now-d['at'])/3600))
        half=max(1,min(72,float(self.options().get('recovery_hours',12))))
        for key,h in [('strain',half),('warmth',3),('excitement',.75)]:d[key]=max(0,min(100,d[key]))*2**(-hours/h)
        d['rapport']=60+(max(0,min(100,d['rapport']))-60)*2**(-hours/72)
        d['at']=now;d['hostility']=[t for t in d['hostility'] if 0<=now-t<12*3600]
        return d

    def _save(self):
        try:atomic_json(self.path,self.data);self.error=''
        except (OSError,ValueError):
            self.error='Mood is active but could not be saved.'
            LOG.warning(self.error)

    def observe(self,text,turn_id,assessment=None):
        if not self.options().get('enabled',True):return self.snapshot()
        kind=cue(text)
        if assessment is not None:
            if not isinstance(assessment,dict):return self.snapshot()
            confidence=assessment.get('confidence');evidence=assessment.get('evidence')
            if (type(confidence) not in (int,float) or not .8<=confidence<=1 or
                assessment.get('kind') not in SIGNALS or not isinstance(evidence,str) or
                not 3<=len(evidence)<=160 or evidence.casefold() not in text.casefold()):return self.snapshot()
            kind=assessment['kind']
            # Strong negative changes require direct targeted language too.
            if kind=='hostile' and cue(text)!='hostile':return self.snapshot()
        with self.lock:
            previous=next((x for x in self.data['turns'] if x['id']==turn_id),None)
            if previous and (previous['kind']!='neutral' or assessment is None):return self.snapshot()
            d=self._project();now=d['at']
            if kind=='hostile':
                d['strain']+=24;d['rapport']-=8;d['warmth']=max(0,d['warmth']-15);d['hostility'].append(now)
                if len(d['hostility'])>=3:d['grudge_until']=now+max(1,min(48,float(self.options().get('grudge_hours',18))))*3600
            elif kind in ('friendly','playful','repair'):
                d['strain']-=14 if kind=='repair' else 5
                d['rapport']+=7 if kind=='repair' else 4;d['warmth']+=12
                if kind=='playful':d['excitement']+=8
                if d['strain']<35 and d['rapport']>=55:d['grudge_until']=0;d['hostility']=[]
            elif kind=='excited':d['excitement']+=35;d['warmth']+=8
            elif kind=='needs_support':d['support_until']=now+20*60
            for key in ('strain','rapport','warmth','excitement'):d[key]=max(0,min(100,d[key]))
            d['turns']=[x for x in d['turns'] if x['id']!=turn_id]+[{'id':str(turn_id)[:160],'kind':kind}]
            d['turns']=d['turns'][-256:];d['hostility']=d['hostility'][-32:]
            self.data=d;self._save()
            return self.snapshot()

    def snapshot(self):
        with self.lock:
            d=self._project();enabled=self.options().get('enabled',True)
            mood=('supportive' if d['support_until']>d['at'] else 'grumpy' if d['grudge_until']>d['at'] or d['strain']>=60 else
                  'hurt' if d['strain']>=18 else 'excited' if d['excitement']>=25 else 'happy' if d['warmth']>=10 else 'calm') if enabled else 'calm'
            return {'mood':mood,'rapport':round(d['rapport']),'strain':round(d['strain']),
                'enabled':enabled,'persistence_error':self.error,'style':{
                    'supportive':'Gentle, attentive, no teasing.','grumpy':'Dry and reserved, still helpful.','hurt':'Quieter, less playful, still kind.',
                    'excited':'Upbeat, lively and concise.','happy':'Warm, playful; an occasional joke.','calm':'Relaxed, lightly witty.'}[mood]}

    def reset(self):
        with self.lock:
            self.data.update(at=self.clock(),rapport=60.,strain=0.,warmth=0.,excitement=0.,support_until=0.,grudge_until=0.,hostility=[],turns=[])
            self._save();return self.snapshot()


class DialogueHistory:
    """Bounded private transcript. Restoring it never queues speech or tools."""
    def __init__(self,path,limit=100):
        self.path=path;self.limit=limit;self.lock=threading.RLock();self.error='';self.rows=deque(maxlen=limit)
        try:
            if path.exists():
                if path.stat().st_size>2_000_000:raise ValueError('Oversized history')
                rows=json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(rows,list):raise ValueError('Invalid history')
                for row in rows[-limit:]:
                    if isinstance(row,dict) and row.get('role') in ('user','assistant') and isinstance(row.get('content'),str):
                        stamp=row.get('time',0)
                        if type(stamp) not in (int,float) or not math.isfinite(stamp):stamp=0
                        self.rows.append({'role':row['role'],'content':row['content'][:4000],'time':stamp})
        except (OSError,ValueError,TypeError):self.error='Saved conversation could not be read.'

    def append(self,role,text):
        if role not in ('user','assistant') or not isinstance(text,str) or not text.strip():return
        with self.lock:
            self.rows.append({'role':role,'content':text[:4000],'time':time.time()})
            try:atomic_json(self.path,list(self.rows));self.error=''
            except OSError:self.error='Conversation could not be saved.';LOG.warning(self.error)

    def recent(self,limit=12):
        with self.lock:return [{'role':r['role'],'content':r['content'][:1000]} for r in list(self.rows)[-limit:]]

    def search(self,query,limit=6):
        words=set(re.findall(r'\w{3,}',query.casefold()))-{'the','what','that','this','you','did','said','and','was','have','about'}
        with self.lock:
            ranked=[(len(words & set(re.findall(r'\w{3,}',r['content'].casefold()))),i,r) for i,r in enumerate(self.rows)]
        return [dict(r,content=r['content'][:1200]) for score,i,r in sorted(ranked,key=lambda x:(x[0],x[1]),reverse=True)[:limit] if score]
