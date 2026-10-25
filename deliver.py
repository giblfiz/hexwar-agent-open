#!/usr/bin/env python3
"""Durable, bounded publisher of organizer-authored outbox records. No payments."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import fcntl
import json
import math
from pathlib import Path
import re

from event_operator import _read, _write, _time, _iso
from moltbook import MoltbookClient, MoltbookError, SAFE_ID, publication_time


def solve_challenge(text):
    """Recognize only a narrow two-operand arithmetic grammar; never execute text."""
    if not isinstance(text,str) or len(text)>2000: raise ValueError('challenge bounds')
    if re.search(r'\d\s*[.,]\s*\d|(?:^|\s)-\s*\d',text):
        raise ValueError('unsupported signed or decimal operand')
    # Obfuscated spelling uses punctuation between letters. Spaces delimit words.
    clean=re.sub('[^a-z0-9 ]','',text.lower())
    names='zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split()
    nums={s:i for i,s in enumerate(names)}
    nums.update(dict(twenty=20,thirty=30,forty=40,fifty=50,sixty=60,seventy=70,eighty=80,ninety=90))
    for tens in ('twenty','thirty','forty','fifty','sixty','seventy','eighty','ninety'):
        for unit in names[1:10]:nums[tens+unit]=nums[tens]+nums[unit]
    words=clean.split(); values=[]; i=0
    unsupported={'hundred','thousand','million','billion','point','negative','decimal','fraction','percent','percentage','squared','cubed','power','dozen'}
    if unsupported.intersection(words):raise ValueError('unsupported arithmetic phrase')
    while i<len(words):
        w=words[i]
        v=int(w) if w.isdigit() else nums.get(w)
        if v is not None:
            if v>=20 and v%10==0 and i+1<len(words) and words[i+1] in names[1:10]:
                v+=nums[words[i+1]]; i+=1
            values.append(v)
        i+=1
    if len(values)!=2 or max(values)>10000: raise ValueError('ambiguous operands')
    ops=[]
    if re.search(r'\b(times|multiplied|multiply|product)\b',clean):ops.append('mul')
    if re.search(r'\b(divided|divide|quotient)\b',clean):ops.append('div')
    if re.search(r'\b(minus|subtract|loses|decreases|slows)\b',clean):ops.append('sub')
    if re.search(r'\b(plus|adds|added|addition|sum)\b',clean):ops.append('add')
    if not ops and 'total' in words and 'each' not in words:ops=['add']
    if len(set(ops))!=1: raise ValueError('ambiguous operation')
    if ops[0]=='sub' and 'from' in words:raise ValueError('ambiguous subtraction order')
    if ops[0]=='div' and 'into' in words:raise ValueError('ambiguous division order')
    a,b=map(Decimal,values)
    if ops[0]=='mul':v=a*b
    elif ops[0]=='div':
        if b==0:raise ValueError('division by zero')
        v=a/b
    elif ops[0]=='sub':v=a-b
    else:v=a+b
    return format(v.quantize(Decimal('0.01'),rounding=ROUND_HALF_UP),'.2f')


def _challenge(response):
    for obj in (response,response.get('post',{}),response.get('comment',{})):
        if isinstance(obj,dict) and isinstance(obj.get('verification'),dict):
            return obj['verification']
    return None


def _retry_delay(details):
    """Use only bounded numeric retry metadata; never interpolate server prose."""
    def numeric(value):
        if type(value) is int and value>=0:return min(value,7*86400)
        if type(value) is float and math.isfinite(value) and value>=0:return value
        if type(value) is str and value.isdigit() and len(value)<=8:return int(value)
        return None
    delay=numeric(details.get('retry_after_seconds'))
    if delay is None:
        minutes=numeric(details.get('retry_after_minutes'))
        if minutes is not None:delay=60*minutes
    if delay is None:
        rates=details.get('rate_limits')
        if type(rates) is dict:delay=numeric(rates.get('Retry-After'))
    return max(60,min(7*86400,int(7200 if delay is None else delay)))+5


def _valid_item(item):
    if type(item) is not dict or type(item.get('id')) is not str or not SAFE_ID.fullmatch(item['id']):
        raise ValueError('invalid outbox identifier')
    if type(item.get('content')) is not str or not item['content']:
        raise ValueError('missing outbox content')
    if item.get('kind')=='comment':
        if type(item.get('post_id')) is not str or not SAFE_ID.fullmatch(item['post_id']):
            raise ValueError('invalid comment destination')
    elif item.get('kind')=='post':
        if (type(item.get('submolt')) is not str or not SAFE_ID.fullmatch(item['submolt'])
                or type(item.get('title')) is not str or not 1<=len(item['title'])<=300
                or len(item['content'])>40000):raise ValueError('invalid post')
    else:raise ValueError('unsupported outbox kind')


def tick(event_dir,*,client=None,now=None):
    root=Path(event_dir)
    current=_time(now) if type(now) is str else (now or datetime.now(timezone.utc))
    if not isinstance(current,datetime) or current.tzinfo is None:raise ValueError('aware observation time required')
    current=current.astimezone(timezone.utc)
    private=root/'.operator'; private.mkdir(parents=True,exist_ok=True,mode=0o700)
    if (root/'STOP').exists():return {'phase':'stopped'}
    config=_read(root/'delivery-config.json')
    if current>=_time(config['stop_at']):return {'phase':'delivery_horizon_ended'}
    with (private/'delivery.lock').open('a+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'phase':'delivery_running'}
        state_path=private/'delivery-state.json'
        state=_read(state_path) if state_path.exists() else {}
        reviews=[]; candidates=[]
        for path in sorted((root/'outbox').glob('*.json')):
            item=_read(path)
            try:_valid_item(item)
            except (ValueError,TypeError):
                if type(item) is not dict:raise ValueError('outbox record must be an object')
                item['state']='needs_review';item['error']='invalid_outbox_record';_write(path,item)
            if item.get('state') in ('sending','verifying_send'):
                item['state']='needs_review';item['error']='interrupted_write_outcome_unknown';_write(path,item)
            if item.get('state')=='needs_review':
                reviews.append(item.get('id',path.stem));continue
            if item.get('state')=='published':continue
            if item.get('state') not in ('pending','verifying'):
                item['state']='needs_review';item['error']='unknown_outbox_state';_write(path,item)
                reviews.append(item.get('id',path.stem));continue
            if item.get('next_at') and current<_time(item['next_at']):continue
            candidates.append((path,item))
        if state.get('next_at') and current<_time(state['next_at']):
            return {'phase':'needs_review' if reviews else 'cooldown','next_at':state['next_at'],'needs_review':reviews}
        if not candidates:
            return {'phase':'needs_review' if reviews else 'outbox_idle','needs_review':reviews}
        path,item=candidates[0]
        transport=client or MoltbookClient.from_credentials(config['credentials_path'])
        if item['state']=='pending':
            item['state']='sending';item['attempt_at']=_iso(current);_write(path,item)
            try:
                if item['kind']=='post':response=transport.create_post(item['submolt'],item['title'],item['content'])
                else:response=transport.create_comment(item['post_id'],item['content'])
            except MoltbookError as exc:
                item['error']='HTTP_'+str(exc.status) if exc.status else 'unknown_write_outcome'
                if exc.status==429:
                    until=_iso(current+timedelta(seconds=_retry_delay(exc.details)))
                    item['state']='pending';item['next_at']=until
                    state['next_at']=until;_write(state_path,state)
                else:item['state']='needs_review'
                _write(path,item)
                return {'phase':item['state'],'item':item['id'],'error':item['error'],'needs_review':reviews}
            except Exception:
                item['state']='needs_review';item['error']='unknown_write_outcome';_write(path,item)
                return {'phase':'needs_review','item':item['id'],'error':item['error'],'needs_review':reviews}
            obj=response.get(item['kind'],{}) if type(response) is dict else {}
            try:
                if type(obj) is not dict or type(obj.get('id')) is not str or not SAFE_ID.fullmatch(obj['id']):
                    raise ValueError('missing publication ID')
                item['publication_id']=obj['id']
                published=_iso(publication_time(obj.get('created_at')))
            except (ValueError,MoltbookError):
                item['state']='needs_review';item['error']='missing_publication_metadata';_write(path,item)
                return {'phase':'needs_review','item':item['id'],'needs_review':reviews}
            item['publication_id']=obj['id'];item['published_at']=published
            verification=_challenge(response)
            item['verification']=verification
            item['state']='verifying' if verification is not None else 'published'
            if verification is None and (response.get('verification_required') is True or obj.get('verification_required') is True):
                item['state']='needs_review';item['error']='missing_verification_challenge'
            _write(path,item)
            state['next_at']=_iso(current+timedelta(seconds=65));_write(state_path,state)
        if item['state']=='verifying':
            v=item.get('verification')
            try:
                if type(v) is not dict or type(v.get('verification_code')) is not str or not v['verification_code']:
                    raise ValueError('missing verification code')
                answer=solve_challenge(v.get('challenge_text',v.get('challenge','')))
                item['state']='verifying_send';_write(path,item)
                verified=transport.verify_challenge(v['verification_code'],answer)
                if type(verified) is not dict or verified.get('success') is not True:
                    raise ValueError('verification success not established')
                item['state']='published';item['verified_at']=_iso(current)
                item.pop('verification',None)
            except Exception:
                item['state']='needs_review';item['error']='challenge_needs_review'
            _write(path,item)
        return {'phase':item['state'],'item':item['id'],'publication_id':item.get('publication_id'),'needs_review':reviews}
