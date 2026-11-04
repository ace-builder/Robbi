"""Cheap explicit context budgets. No tokenization/model/vector DB on the Pi."""
import json


def clipped(text, maximum):
    text = str(text or '')
    if len(text) <= maximum:return text
    return text[:max(0,maximum-28)] + '\n[Context omitted for size]'


def recent_context(messages, maximum=6000, count=10):
    selected, remaining = [], maximum
    for item in reversed(messages or []):
        if item.get('role') not in {'user','assistant'}:continue
        text = str(item.get('content') or '')
        if not text:continue
        text = clipped(text,min(1600,remaining))
        selected.append({'role':item['role'],'content':text})
        remaining -= len(text)
        if len(selected)>=count or remaining<100:break
    return list(reversed(selected))


def prompt_sections(memory_text='',soul_text='',tools_text='',app_catalog_text='',extra_instructions=''):
    # Execution/security instructions are kept separate from untrusted context.
    # Limits apply to every provider, foreground/background and tool followups.
    return dict(memory_text=clipped(memory_text,5000), soul_text=clipped(soul_text,3500),
                tools_text=clipped(tools_text,26000), app_catalog_text=clipped(app_catalog_text,2500),
                extra_instructions=clipped(extra_instructions,3500))


def daily_context(daily):
    # Avoid repeating full event histories, diagnostics and conversation dumps.
    result = {key:daily[key] for key in ('day_cycle','suggested_next_action') if key in daily}
    for key in ('open_todos','active_reminders'):
        result[key] = [{k:clipped(v,200) for k,v in row.items() if k in {'id','title','message','due_at'}}
                       for row in daily.get(key,[])[:5] if isinstance(row,dict)]
    return json.dumps(result,ensure_ascii=False,separators=(',',':'))


def tool_evidence(value, budget=14000):
    """Keep valid JSON and actual error/source fields rather than slicing JSON."""
    def trim(item, depth=0):
        if depth>6:return '[nested detail omitted]'
        if isinstance(item,str):return clipped(item,600)
        if isinstance(item,list):return [trim(v,depth+1) for v in item[:6]]
        if isinstance(item,dict):
            priority=('call_id','index','tool','action','status','dispatched','reused_from','elapsed_ms',
                      'ok','error','text','query','title','url','source','published_date','date','result','data','results','artifacts')
            keys=sorted(item,key=lambda k:priority.index(k) if k in priority else len(priority))[:18]
            return {k:trim(item[k],depth+1) for k in keys}
        return item
    selected=trim(value)
    encoded=json.dumps(selected,ensure_ascii=False,allow_nan=False)
    if len(encoded.encode('utf8'))<=budget:return encoded
    # A small envelope per actual tool preserves failure truth and the text;
    # broad nested payloads never crowd out the response rules.
    rows=[]
    originals = value[:6]
    allowance = max(300, (budget - 2) // max(1, len(originals)) - 2)
    for row in originals:
        result=row.get('result',{})
        envelope = {k:row[k] for k in ('call_id','index','tool','status','dispatched','reused_from','elapsed_ms') if k in row}
        envelope.update(details_omitted=True, result={'ok':result.get('ok'),
                        'error':clipped(result.get('error'),200), 'text':clipped(result.get('text'),900)})
        if 'action' in row:
            envelope['action_excerpt'] = clipped(json.dumps(row['action'], ensure_ascii=False), 700)
            envelope['arguments_complete'] = False
        # Enforce a byte budget even for non-ASCII mail. Never cut serialized JSON
        # or drop call IDs; shrink only explanatory excerpts.
        while len(json.dumps(envelope, ensure_ascii=False).encode('utf8')) > allowance:
            fields = [(envelope, 'action_excerpt'), (envelope['result'], 'text'), (envelope['result'], 'error')]
            owner, key = max(fields, key=lambda pair: len(pair[0].get(pair[1], '')))
            text = owner.get(key, '')
            if not text:
                break
            owner[key] = text[:len(text)//2]
        rows.append(envelope)
    return json.dumps(rows,ensure_ascii=False,allow_nan=False)
