#!/usr/bin/env python3
"""Read-only event observer and durable outbox coordinator; never posts or pays.

Runtime directory is private operator state, not a public export. Public release
must explicitly select signed event artifacts, excluding operator-config.json,
.operator/, outbox/ and STOP. Commands are never read from platform messages.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import secrets

import judge
from moltbook import MoltbookClient, MoltbookError
import protocol
import signing

CONFIG_FIELDS = {'organizer_key_path','registration_post_id','first_round_start',
                 'round_gap_seconds','roster_grace_seconds','max_entries','observer_stop_at'}
PROJECT = Path(__file__).resolve().parent
MAX_FILE_BYTES = 2 * 1024 * 1024


class OperatorError(ValueError):
    pass


def _time(text):
    protocol._time(text)
    return datetime.fromisoformat(text.replace('Z','+00:00')).astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00','Z')


def _read(path):
    path=Path(path)
    if path.stat().st_size>MAX_FILE_BYTES:
        raise OperatorError('local state file exceeds size bound')
    def unique_pairs(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('duplicate local JSON key')
            result[key]=value
        return result
    return json.loads(path.read_text(),object_pairs_hook=unique_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite local JSON number')))


def _write(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_name(path.name+'.tmp-'+secrets.token_hex(6))
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as f:
            data=json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False).encode('ascii')
            f.write(data+b'\n'); f.flush(); os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if temp.exists(): temp.unlink()


def _once(path, value, *, outbox=False):
    path=Path(path)
    if path.exists():
        old=_read(path)
        if outbox:
            fields=('id','kind','post_id','content')
            if any(old.get(k)!=value.get(k) for k in fields):
                raise OperatorError('existing outbox entry differs from frozen plan')
        elif old!=value:
            raise OperatorError('existing signed artifact differs from frozen plan')
        return
    _write(path,value)


def _config(root):
    cfg=_read(root/'operator-config.json')
    if type(cfg) is not dict or set(cfg)!=CONFIG_FIELDS:
        raise OperatorError('invalid operator configuration fields')
    key_path=Path(cfg['organizer_key_path']).expanduser()
    if not key_path.is_absolute() or key_path.resolve().is_relative_to(PROJECT):
        raise OperatorError('organizer signing key must be external to source repository')
    if type(cfg['registration_post_id']) is not str or not cfg['registration_post_id']:
        raise OperatorError('registration source missing')
    for name,low,high in [('round_gap_seconds',7200,604800),('roster_grace_seconds',0,86400),('max_entries',2,16)]:
        if type(cfg[name]) is not int or not low<=cfg[name]<=high:
            raise OperatorError('invalid bounded operator configuration')
    first=_time(cfg['first_round_start']); stop=_time(cfg['observer_stop_at'])
    if stop<=first+timedelta(seconds=7200):
        raise OperatorError('observation horizon ends before first adjudication')
    key=signing.load_key(key_path)
    charter=_read(root/'charter.json')
    payload=signing.verify(charter,signing.public_hex(key))
    if payload.get('kind')!='charter' or payload.get('protocol')!='HXO-v0':
        raise OperatorError('invalid signed charter')
    deadline=_time(payload['registration_deadline'])
    if deadline+timedelta(seconds=cfg['roster_grace_seconds']+600)>=first:
        raise OperatorError('registration freeze leaves insufficient setup time')
    return cfg,key,charter


def collect_registrations(records, charter, *, now, max_entries):
    """Strict data-only registration filter. Returns private candidates and counts."""
    if type(records) is not list or len(records)>5000:
        raise OperatorError('registration fetch is not a bounded complete list')
    tid=charter['payload']['tournament_id']; ch=signing.digest(charter)
    deadline=_time(charter['payload']['registration_deadline'])
    valid=[]; invalid=0; seen_ids=set()
    for record in records:
        if type(record) is not dict:
            raise OperatorError('malformed authoritative record')
        rid=record.get('id')
        if type(rid) is not str or not rid or rid in seen_ids:
            raise OperatorError('duplicate or missing authoritative record ID')
        seen_ids.add(rid)
        content=record.get('content')
        if type(content) is not str or len(content)>65536:
            # Deleted registration evidence may be unrecoverable. Surface the gap.
            if record.get('is_deleted'):
                raise protocol.ManualReview('deleted registration-thread evidence')
            continue
        if not content.startswith('HXO1 '): continue
        try:
            envelope=signing.parse(content[5:]); payload=signing.verify(envelope)
            if payload.get('kind')!='registration': continue
            protocol._fields(payload,'kind tournament_id charter_hash account_id payout_chain_id payout_address')
            expected=protocol.registration_payload(tid,ch,payload['account_id'],payload['payout_address'],payload['payout_chain_id'])
            if payload!=expected or record.get('author_id')!=payload['account_id']:
                invalid+=1; continue
            created=_time(record['created_at'])
            if created>deadline or created>now:
                invalid+=1; continue
            if record.get('is_deleted') is True:
                raise protocol.ManualReview('accepted registration publication deleted')
            updated=record.get('updated_at')
            if updated is not None and _time(updated)!=created:
                raise protocol.ManualReview('registration publication edited')
            if type(record.get('is_deleted')) is not bool or 'updated_at' not in record:
                raise OperatorError('registration record missing mutation evidence')
            valid.append(dict(public_key=envelope['public_key'],account_id=payload['account_id'],
                              registration_hash=signing.digest(envelope),source_id=rid,
                              created_at=_iso(created),envelope=envelope))
        except protocol.ManualReview:
            raise
        except OperatorError:
            raise
        except (ValueError,TypeError,KeyError,RecursionError):
            invalid+=1
    valid.sort(key=lambda r:(_time(r['created_at']),r['source_id']))
    selected=[]; keys=set(); accounts=set(); duplicates=0
    for reg in valid:
        if reg['public_key'] in keys or reg['account_id'] in accounts:
            duplicates+=1; continue
        keys.add(reg['public_key']); accounts.add(reg['account_id']); selected.append(reg)
    admitted=selected[:max_entries]
    counts=dict(valid_unique=len(selected),admitted=len(admitted),waitlisted=max(0,len(selected)-max_entries),
                duplicate=duplicates,invalid=invalid,source_records=len(records))
    return admitted,counts


def _outbox_item(name, post_id, envelope):
    return dict(id=name,kind='comment',post_id=post_id,
                content='HXO1 '+signing.canonical(envelope).decode(),state='pending')


def _freeze_plan(cfg,key,charter,selected,now):
    tid=charter['payload']['tournament_id']; ch=signing.digest(charter); frozen=_iso(now)
    if len(selected)<2:
        cancellation=signing.sign(dict(kind='cancellation',tournament_id=tid,charter_hash=ch,
                                       reason='fewer_than_two_valid_entrants',entrants=len(selected),
                                       decided_at=frozen,funds_transferred=False),key)
        return dict(kind='cancel',created_at=frozen,artifacts={'cancellation.json':cancellation},
                    outbox=[_outbox_item('cancellation',cfg['registration_post_id'],cancellation)])
    roster=signing.sign(dict(kind='roster',tournament_id=tid,charter_hash=ch,
                            registrations=[r['registration_hash'] for r in selected],frozen_at=frozen),key)
    seed=secrets.token_hex(32)
    slots=protocol.bracket_slots([r['public_key'] for r in selected],seed)
    rounds=len(slots).bit_length()-1
    starts=[_iso(_time(cfg['first_round_start'])+timedelta(seconds=i*cfg['round_gap_seconds'])) for i in range(rounds)]
    if _time(starts[0])-timedelta(seconds=600)<=now:
        raise OperatorError('cannot freeze a roster after setup cutoff')
    bracket=signing.sign(dict(kind='bracket',tournament_id=tid,charter_hash=ch,roster_hash=signing.digest(roster),
                             seed=seed,seed_published_at=frozen,round_starts=starts),key)
    match_ids=[f'r{r+1}m{i+1}' for r in range(rounds) for i in range(len(slots)>>(r+1))]
    venue=signing.sign(dict(kind='venue_map',tournament_id=tid,charter_hash=ch,
                           registration_post_id=cfg['registration_post_id'],
                           match_posts={mid:cfg['registration_post_id'] for mid in match_ids}),key)
    artifacts={'roster.json':roster,'bracket.json':bracket,'venue-map.json':venue}
    return dict(kind='freeze',created_at=frozen,artifacts=artifacts,
                outbox=[_outbox_item(name.removesuffix('.json'),cfg['registration_post_id'],env) for name,env in artifacts.items()])


def _materialize(root, plan):
    for name,envelope in plan['artifacts'].items(): _once(root/name,envelope)
    for item in plan['outbox']: _once(root/'outbox'/(item['id']+'.json'),item,outbox=True)


def _status(root,state,now):
    state['last_poll_at']=_iso(now)
    _write(root/'.operator'/'state.json',state)
    safe={k:state[k] for k in ['phase','last_poll_at','last_success_at','last_fetch_success_at','last_failure_at','counts','error_code',
                              'final_status','provisional_result_hash','challenge_ends_at','funds_transferred'] if k in state}
    with (root/'.operator'/'log.jsonl').open('a') as log:
        log.write(json.dumps(safe,separators=(',',':'))+'\n')
    return safe


def poll(event_dir, *, client=None, now=None, judge_fn=None):
    """One bounded observation. No API write methods or currency tools are called."""
    root=Path(event_dir).resolve()
    if not root.is_dir(): raise OperatorError('event directory missing')
    private=root/'.operator'; private.mkdir(mode=0o700,exist_ok=True)
    current=_time(now) if type(now) is str else (now or datetime.now(timezone.utc))
    if not isinstance(current,datetime) or current.tzinfo is None:
        raise OperatorError('now must be timezone-aware')
    current=current.astimezone(timezone.utc)
    lock_path=private/'poll.lock'
    with lock_path.open('a+') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return {'phase':'another_poll_running','funds_transferred':False}
        state=_read(private/'state.json') if (private/'state.json').exists() else {'funds_transferred':False}
        if (root/'STOP').exists():
            state['phase']='stopped'; return _status(root,state,current)
        try:
            cfg,key,charter=_config(root)
            if current>=_time(cfg['observer_stop_at']):
                state['phase']='observation_horizon_ended'; return _status(root,state,current)
            freeze_path=private/'freeze-plan.json'
            if freeze_path.exists():
                plan=_read(freeze_path)
                if plan.get('config_hash')!=signing.digest(cfg) or plan.get('charter_hash')!=signing.digest(charter):
                    raise OperatorError('frozen configuration or charter changed')
                _materialize(root,plan)
                state['phase']='cancelled_pending_notice' if plan['kind']=='cancel' else 'frozen'
                if plan['kind']=='cancel':
                    item=_read(root/'outbox'/'cancellation.json')
                    if item.get('state')=='published': state['phase']='cancelled'
                    return _status(root,state,current)
            else:
                transport=client or MoltbookClient()
                transport.fetch_post(cfg['registration_post_id'])
                records=transport.fetch_comments(cfg['registration_post_id'])
                state['last_fetch_success_at']=_iso(current)
                selected,counts=collect_registrations(records,charter,now=current,max_entries=cfg['max_entries'])
                state['last_success_at']=_iso(current); state['counts']=counts; state.pop('error_code',None)
                _write(private/'latest-registration-evidence.json',dict(observed_at=_iso(current),
                         charter_hash=signing.digest(charter),counts=counts,selected=selected))
                freeze_at=_time(charter['payload']['registration_deadline'])+timedelta(seconds=cfg['roster_grace_seconds'])
                if current<freeze_at:
                    state['phase']='registration_open' if current<=_time(charter['payload']['registration_deadline']) else 'registration_grace'
                    return _status(root,state,current)
                # Sponsor-authorized organizer participation needs an explicit public
                # amendment; never fabricate a timely second registration here.
                if len(selected)==1 and (root/'organizer-fallback-authorized').is_file():
                    state['phase']='organizer_entry_required'
                    return _status(root,state,current)
                plan=_freeze_plan(cfg,key,charter,selected,current)
                plan['config_hash']=signing.digest(cfg); plan['charter_hash']=signing.digest(charter)
                _write(freeze_path,plan)  # Durable decision before writing any public artifact/outbox item.
                _materialize(root,plan)
                state['phase']='cancelled_pending_notice' if plan['kind']=='cancel' else 'frozen'
                if plan['kind']=='cancel': return _status(root,state,current)
            bracket=signing.verify(_read(root/'bracket.json'),signing.public_hex(key))
            final_due=_time(bracket['round_starts'][-1])+timedelta(seconds=7200)
            provisional_path=private/'provisional-plan.json'
            if provisional_path.exists():
                plan=_read(provisional_path); _materialize(root,plan)
                state['phase']='provisional_pending_notice'
                state['provisional_result_hash']=plan['result_hash']; state['challenge_ends_at']=plan['challenge_ends_at']
                return _status(root,state,current)
            if current<final_due:
                state['phase']='awaiting_play'; return _status(root,state,current)
            verifier=judge_fn or judge.fetch_and_verify
            result=verifier(root,signing.public_hex(key),client=client,as_of=_iso(current))
            verification=result['verification']
            state['last_success_at']=_iso(current); state.pop('error_code',None)
            _write(private/'latest-final-verification.json',result)
            if verification.get('winner_eligible') is not True:
                state['phase']='final_review_required'; state['final_status']='no_eligible_played_winner'
                return _status(root,state,current)
            ends=_iso(current+timedelta(hours=24))
            payload=dict(kind='provisional_result',tournament_id=charter['payload']['tournament_id'],
                         result_hash=verification['result_hash'],winner=verification['certificate']['winner'],
                         played_series=verification['certificate']['played_series'],challenge_ends_at=ends)
            envelope=signing.sign(payload,key)
            item=_outbox_item('provisional-result',cfg['registration_post_id'],envelope)
            if len(item['content'].encode())>100000: item['state']='needs_review'
            plan=dict(kind='provisional',created_at=_iso(current),result_hash=verification['result_hash'],challenge_ends_at=ends,
                      artifacts={'provisional-result.json':envelope,'result-certificate.json':verification['certificate']},outbox=[item])
            _write(provisional_path,plan); _materialize(root,plan)
            state['phase']='provisional_pending_notice'; state['final_status']='verified_no_transfer'
            state['provisional_result_hash']=verification['result_hash']; state['challenge_ends_at']=ends
            return _status(root,state,current)
        except (MoltbookError,protocol.ProtocolError,OperatorError,OSError,ValueError,KeyError,TypeError) as exc:
            # Never copy exception/server/message prose into logs or outbox. The
            # category is enough to route a bounded retry or manual investigation.
            state['last_failure_at']=_iso(current)
            state['error_code']=type(exc).__name__
            state['phase']='manual_review' if isinstance(exc,protocol.ManualReview) else 'observation_failed'
            return _status(root,state,current)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['poll'])
    parser.add_argument('--event-dir',required=True,type=Path)
    parser.add_argument('--as-of',help='Explicit UTC observation time for local rehearsal')
    args=parser.parse_args()
    result=poll(args.event_dir,now=args.as_of)
    print(json.dumps(result,indent=2))
    return 1 if result['phase'] in ('observation_failed','manual_review','final_review_required') else 0


if __name__=='__main__': raise SystemExit(main())
