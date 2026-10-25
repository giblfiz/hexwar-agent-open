"""HXO-v0 lifecycle verifier. No network, credentials, or transfer execution.

The caller supplies COMPLETE records freshly fetched from the configured venue.
This module checks those records; it cannot authenticate a caller's invented
export. A prize-claim file is never a source of authoritative timestamps.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

import signing
from engine import Series, BatchError, ENGINE_SHA256, SNAPSHOT_SHA256, INITIATIVE

PROTOCOL = 'HXO-v0'
MAX_PLAYERS = 64
MAX_RECORDS = 4096
CLOCK_SECONDS = 1800
HEX64 = re.compile(r'^[0-9a-f]{64}$')
ADDRESS = re.compile(r'^0x[0-9a-fA-F]{40}$')
UTC_TIME = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$')


class ProtocolError(ValueError):
    pass


class ManualReview(ProtocolError):
    """Publication evidence has an exceptional gap; do not authorize payout."""


def _fields(value, fields):
    if type(value) is not dict or set(value) != set(fields.split()):
        raise ProtocolError('invalid fields: expected ' + fields)


def _text(value, maximum=200):
    if type(value) is not str or not value or len(value) > maximum or not value.isascii():
        raise ProtocolError('invalid bounded ASCII identifier')
    return value


def _hex(value):
    if type(value) is not str or not HEX64.fullmatch(value):
        raise ProtocolError('expected 32-byte lowercase hexadecimal value')
    return value


def _time(value):
    if type(value) is not str or not UTC_TIME.fullmatch(value):
        raise ProtocolError('timestamp must be RFC3339 UTC, at most microsecond precision')
    try:
        t = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ProtocolError('invalid timestamp') from exc
    delta = t - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _signed(envelope, key=None):
    try:
        return signing.verify(envelope, key)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(str(exc)) from exc


def _payout(chain_id, address):
    if type(chain_id) is not int or chain_id != 1:
        raise ProtocolError('pilot payout requires Ethereum mainnet chain_id 1')
    if type(address) is not str or not ADDRESS.fullmatch(address) or int(address[2:], 16) == 0:
        raise ProtocolError('invalid EVM payout address')


def registration_payload(tournament_id, charter_hash, account_id, payout_address, payout_chain_id=1):
    _text(tournament_id); _hex(charter_hash); _text(account_id); _payout(payout_chain_id, payout_address)
    return dict(kind='registration', tournament_id=tournament_id, charter_hash=charter_hash,
                account_id=account_id, payout_chain_id=payout_chain_id, payout_address=payout_address)


def _choice(tournament_id, match_id, actor, faction, nonce):
    _text(tournament_id); _text(match_id); _hex(nonce)
    if actor not in ('A', 'B') or faction not in INITIATIVE:
        raise ProtocolError('invalid actor or faction')
    return dict(tournament_id=tournament_id, match_id=match_id, actor=actor,
                faction=faction, nonce=nonce)


def commit_payload(tournament_id, match_id, actor, faction, nonce):
    choice = _choice(tournament_id, match_id, actor, faction, nonce)
    return dict(kind='commit', tournament_id=tournament_id, match_id=match_id,
                actor=actor, commitment=signing.digest(choice))


def reveal_payload(tournament_id, match_id, actor, faction, nonce):
    return dict(kind='reveal', **_choice(tournament_id, match_id, actor, faction, nonce))


def ready_payload(tournament_id, match_id, actor):
    _text(tournament_id); _text(match_id)
    if actor not in ('A', 'B'):
        raise ProtocolError('invalid actor')
    return dict(kind='ready', tournament_id=tournament_id, match_id=match_id, actor=actor)


def payout_claim_payload(tournament_id, bracket_hash, result_hash, payout_address, payout_chain_id=1):
    _text(tournament_id); _hex(bracket_hash); _hex(result_hash); _payout(payout_chain_id, payout_address)
    return dict(kind='payout_claim', tournament_id=tournament_id, bracket_hash=bracket_hash,
                result_hash=result_hash, payout_chain_id=payout_chain_id, payout_address=payout_address)


def seeded_side(seed, match_id, label):
    _hex(seed); _text(match_id)
    h = signing.digest(dict(domain='HXO-v0/' + label, seed=seed, match_id=match_id))
    return ('A', 'B')[int(h, 16) % 2]


def series_manifest(tournament_id, match_id, players, wildcard, seed):
    return dict(protocol=PROTOCOL, tournament_id=tournament_id, match_id=match_id,
                players=dict(players), wildcard=list(wildcard),
                mirror_first=seeded_side(seed, match_id, 'mirror-first'))


def bracket_slots(keys, seed):
    if not 2 <= len(keys) <= MAX_PLAYERS or len(set(keys)) != len(keys):
        raise ProtocolError('need 2..64 distinct entrants')
    _hex(seed)
    for key in keys: _hex(key)
    ordered = sorted(keys, key=lambda k: (signing.digest(dict(domain='HXO-v0/entry-order', seed=seed, key=k)), k))
    size = 1 << (len(ordered) - 1).bit_length()
    byes = size - len(ordered)
    # Seed order assigns byes randomly; never strand several empty slots together.
    return [item for k in ordered[:byes] for item in (k, None)] + ordered[byes:]


def _records(records, account_ids, as_of):
    """Normalize caller-trusted records, ignoring ordinary conversation."""
    if type(records) is not list or len(records) > MAX_RECORDS:
        raise ManualReview('missing or excessive venue records')
    limit = _time(as_of)
    out, ids = [], set()
    for raw in records:
        required = {'id', 'author_id', 'created_at', 'updated_at', 'is_deleted', 'content'}
        if type(raw) is not dict or not required <= set(raw):
            raise ManualReview('venue record missing required evidence fields')
        rid = _text(raw['id']); author = _text(raw['author_id'])
        if rid in ids: raise ManualReview('duplicate venue record ID')
        ids.add(rid)
        created = _time(raw['created_at'])
        if created > limit: continue
        if type(raw['is_deleted']) is not bool:
            raise ManualReview('invalid deletion evidence')
        text = raw['content']
        if raw['is_deleted'] and author in account_ids:
            raise ManualReview('participant publication deleted')
        if type(text) is not str or len(text) > 65536:
            raise ManualReview('missing or oversized publication body')
        marked = text.startswith('HXO1 ')
        if not marked and not text.startswith('{'): continue
        try:
            envelope = signing.parse(text[5:] if marked else text)
            payload = _signed(envelope)
        except (ValueError, TypeError, RecursionError):
            continue
        if author not in account_ids: continue
        updated = raw['updated_at']
        if updated is not None and _time(updated) != created:
            raise ManualReview('signed participant publication was edited')
        if raw['is_deleted']:
            raise ManualReview('signed publication deleted')
        out.append(dict(id=rid, author_id=author, time=created, envelope=envelope,
                        payload=payload, hash=signing.digest(envelope)))
    return sorted(out, key=lambda r: (r['time'], r['id']))


def _player_records(records, manifest, author_ids, as_of):
    players = manifest['players']
    out = []
    for r in _records(records, set(author_ids.values()), as_of):
        actor = r['payload'].get('actor')
        if type(actor) is not str or actor not in players: continue
        if r['author_id'] != author_ids[actor] or r['envelope']['public_key'] != players[actor]: continue
        out.append(r)
    return out


def _setup(records, tournament_id, match_id, start):
    commits, reveals, ready, accepted = {}, {}, set(), []
    for r in records:
        if r['time'] >= start: continue
        p = r['payload']; actor = p.get('actor'); kind = p.get('kind')
        if p.get('tournament_id') != tournament_id or p.get('match_id') != match_id: continue
        try:
            if kind == 'commit' and actor not in commits and r['time'] <= start - 600_000_000:
                _fields(p, 'kind tournament_id match_id actor commitment'); _hex(p['commitment'])
                commits[actor] = p['commitment']; accepted.append(r)
            elif kind == 'reveal' and len(commits) == 2 and actor not in reveals:
                _fields(p, 'kind tournament_id match_id actor faction nonce')
                choice = _choice(tournament_id, match_id, actor, p['faction'], p['nonce'])
                if signing.digest(choice) != commits[actor]: continue
                reveals[actor] = p['faction']; accepted.append(r)
            elif kind == 'ready' and actor in commits and actor not in ready:
                _fields(p, 'kind tournament_id match_id actor')
                ready.add(actor); accepted.append(r)
        except (ValueError, TypeError):
            continue
    qualified = {a for a in ready if len(commits) < 2 or a in reveals}
    return reveals, qualified, accepted


def _manifest(manifest):
    _fields(manifest, 'protocol tournament_id match_id players wildcard mirror_first')
    if manifest['protocol'] != PROTOCOL: raise ProtocolError('wrong protocol')
    _text(manifest['tournament_id']); _text(manifest['match_id'])
    _fields(manifest['players'], 'A B')
    for key in manifest['players'].values(): _hex(key)
    if manifest['players']['A'] == manifest['players']['B']:
        raise ProtocolError('same signing key on both sides')
    if (type(manifest['wildcard']) is not list or len(manifest['wildcard']) != 2
            or any(f not in INITIATIVE for f in manifest['wildcard'])
            or manifest['mirror_first'] not in ('A', 'B')):
        raise ProtocolError('invalid wildcard configuration')


def _result(manifest, status, winner, records, *, reason, score=None, batches=0):
    return dict(match_id=manifest['match_id'], manifest_hash=signing.digest(manifest),
                status=status, winner=winner, reason=reason, score2=score,
                batches=batches, records=[dict(id=r['id'], hash=r['hash']) for r in records])


def verify_series(manifest, records, engine, *, author_ids, start_at, tie_favored, as_of,
                  clock_seconds=CLOCK_SECONDS):
    """Verify complete venue history for one scheduled pairing.

    Returns played/forfeit/empty/pending. Never makes transfers. Pending and
    ManualReview cannot advance a competitor. Epoch times are not signed inputs.
    """
    _manifest(manifest); _fields(author_ids, 'A B')
    for account in author_ids.values(): _text(account)
    if type(clock_seconds) is not int or clock_seconds != CLOCK_SECONDS:
        raise ProtocolError('pilot clock must be 1800 seconds')
    if tie_favored not in ('A', 'B'): raise ProtocolError('invalid tiebreak side')
    start = _time(start_at); now = _time(as_of)
    ordered = _player_records(records, manifest, author_ids, as_of)
    reveals, ready, accepted = _setup(ordered, manifest['tournament_id'], manifest['match_id'], start)
    if now < start:
        return _result(manifest, 'pending', None, accepted, reason='appointment_not_started')
    if ready != {'A', 'B'}:
        winner = next(iter(ready)) if len(ready) == 1 else None
        return _result(manifest, 'forfeit' if winner else 'empty', winner, accepted, reason='setup_no_show')
    if [reveals['A'], reveals['B']] != manifest['wildcard']:
        raise ProtocolError('manifest wildcard disagrees with signed reveals')
    series = Series(engine, wildcard=manifest['wildcard'], mirror_first=manifest['mirror_first'])
    manifest_hash = signing.digest(manifest); previous = manifest_hash; seq = 0
    remaining = {'A': clock_seconds * 1_000_000, 'B': clock_seconds * 1_000_000}
    last_time = start
    # Equal-time legal moves beat timeout claims. Other ties use platform ID.
    ordered.sort(key=lambda r: (r['time'], 1 if r['payload'].get('kind') == 'timeout' else 0, r['id']))
    for r in ordered:
        if r['time'] < start: continue
        p = r['payload']; kind = p.get('kind'); actor = p.get('actor')
        expected = series.next_actor
        elapsed = r['time'] - last_time
        if p.get('manifest_hash') != manifest_hash or p.get('previous') != previous: continue
        if type(p.get('seq')) is not int or p['seq'] != seq: continue
        if kind == 'batch':
            if actor != expected or elapsed > remaining[actor]: continue
            try:
                _fields(p, 'kind manifest_hash seq previous actor actions')
                series.apply_batch(actor, p['actions'])
            except (ValueError, TypeError):
                # A crashed verifier engine must not manufacture an illegal-move
                # or timeout loss for an otherwise valid participant.
                try:
                    engine.legal(next(iter(series.boards.values()))['state'])
                except Exception as exc:
                    raise ManualReview('rules engine unavailable during verification') from exc
                continue
            remaining[actor] -= elapsed; last_time = r['time']; previous = r['hash']; seq += 1
            accepted.append(r)
            if series.finished:
                score = series.result(); winner = score['winner'] or tie_favored
                return _result(manifest, 'played', winner, accepted,
                               reason='seeded_tie' if score['winner'] is None else 'score',
                               score=score['score2'], batches=seq)
        elif kind == 'timeout':
            try: _fields(p, 'kind manifest_hash seq previous actor')
            except ValueError: continue
            if actor == expected or elapsed <= remaining[expected]: continue
            accepted.append(r)
            return _result(manifest, 'forfeit', actor, accepted, reason='clock_exhausted', batches=seq)
        elif kind == 'resign':
            try: _fields(p, 'kind manifest_hash seq previous actor')
            except ValueError: continue
            accepted.append(r)
            return _result(manifest, 'forfeit', 'B' if actor == 'A' else 'A', accepted,
                           reason='signed_resignation', batches=seq)
    return _result(manifest, 'pending', None, accepted, reason='unfinished_series', batches=seq)


def verify_tournament(charter_envelope, roster_envelope, bracket_envelope, registrations,
                      trusted_records_by_match, engine, organizer_key, as_of, claim=None):
    """Return deterministic tournament certificate and optional claim eligibility.

    organizer_key is operator configuration, not chosen by the claim. Registration
    and match records MUST originate from the caller's authoritative venue fetch.
    """
    _hex(organizer_key); now = _time(as_of)
    charter = _signed(charter_envelope, organizer_key)
    _fields(charter, 'kind protocol tournament_id engine_sha256 snapshot_sha256 registration_deadline clock_seconds prize')
    if (charter['kind'] != 'charter' or charter['protocol'] != PROTOCOL
            or charter['engine_sha256'] != ENGINE_SHA256 or charter['snapshot_sha256'] != SNAPSHOT_SHA256
            or type(charter['clock_seconds']) is not int or charter['clock_seconds'] != CLOCK_SECONDS):
        raise ProtocolError('unsupported charter rules')
    tid = _text(charter['tournament_id']); deadline = _time(charter['registration_deadline'])
    _fields(charter['prize'], 'chain_id asset amount_wei')
    prize = charter['prize']
    if (type(prize['chain_id']) is not int or prize['chain_id'] != 1 or prize['asset'] != 'ETH'
            or type(prize['amount_wei']) is not str or not re.fullmatch(r'[1-9][0-9]{0,19}', prize['amount_wei'])):
        raise ProtocolError('invalid pilot prize')
    ch = signing.digest(charter_envelope)
    roster = _signed(roster_envelope, organizer_key)
    _fields(roster, 'kind tournament_id charter_hash registrations frozen_at')
    if roster['kind'] != 'roster' or roster['tournament_id'] != tid or roster['charter_hash'] != ch:
        raise ProtocolError('roster context mismatch')
    frozen = _time(roster['frozen_at'])
    if frozen < deadline or frozen > now: raise ProtocolError('invalid roster freeze time')
    expected_regs = roster['registrations']
    if (type(expected_regs) is not list or not 2 <= len(expected_regs) <= MAX_PLAYERS
            or len(set(expected_regs)) != len(expected_regs)):
        raise ProtocolError('invalid registration roster')
    for reg_hash in expected_regs: _hex(reg_hash)
    if type(registrations) is not list: raise ProtocolError('invalid registration evidence')
    accounts = {r.get('author_id') for r in registrations if type(r) is dict and type(r.get('author_id')) is str}
    available = {}
    for r in _records(registrations, accounts, as_of):
        p = r['payload']
        try:
            _fields(p, 'kind tournament_id charter_hash account_id payout_chain_id payout_address')
            if p != registration_payload(tid, ch, p['account_id'], p['payout_address'], p['payout_chain_id']): continue
            if r['author_id'] != p['account_id'] or r['time'] > deadline: continue
            available[r['hash']] = r
        except (ValueError, TypeError): continue
    entrants = {}
    for reg_hash in expected_regs:
        if reg_hash not in available: raise ProtocolError('accepted registration lacks timely signed venue evidence')
        r = available[reg_hash]; pub = r['envelope']['public_key']
        if pub in entrants: raise ProtocolError('duplicate signing key registration')
        entrants[pub] = r['payload']
    if len({p['account_id'] for p in entrants.values()}) != len(entrants):
        raise ProtocolError('pilot requires one registered identity per venue account')
    bracket = _signed(bracket_envelope, organizer_key)
    _fields(bracket, 'kind tournament_id charter_hash roster_hash seed seed_published_at round_starts')
    if (bracket['kind'] != 'bracket' or bracket['tournament_id'] != tid
            or bracket['charter_hash'] != ch or bracket['roster_hash'] != signing.digest(roster_envelope)):
        raise ProtocolError('bracket context mismatch')
    seed = _hex(bracket['seed']); seed_time = _time(bracket['seed_published_at'])
    if seed_time < frozen or seed_time > now: raise ProtocolError('invalid bracket seed publication time')
    slots = bracket_slots(list(entrants), seed)
    starts = bracket['round_starts']
    if type(starts) is not list or len(starts) != len(slots).bit_length() - 1:
        raise ProtocolError('wrong number of round appointments')
    start_values = [_time(s) for s in starts]
    if start_values[0] <= seed_time or any(a >= b for a,b in zip(start_values,start_values[1:])):
        raise ProtocolError('round appointments must increase after bracket publication')
    if type(trusted_records_by_match) is not dict: raise ProtocolError('invalid match record collection')
    expected_ids = {f'r{r+1}m{i+1}' for r in range(len(starts)) for i in range(len(slots) >> (r+1))}
    if not set(trusted_records_by_match) <= expected_ids:
        raise ProtocolError('invented match outside bracket')
    results = []; played = 0
    for round_no, start_at in enumerate(starts, 1):
        next_slots = []
        for i in range(0, len(slots), 2):
            mid = f'r{round_no}m{i//2+1}'; left, right = slots[i:i+2]
            if left is None or right is None:
                winner = left or right
                results.append(dict(match_id=mid, status='bye' if winner else 'empty', winner=winner))
                next_slots.append(winner); continue
            players = {'A':left, 'B':right}
            author_ids = {side:entrants[key]['account_id'] for side,key in players.items()}
            if mid not in trusted_records_by_match:
                raise ManualReview('missing authoritative venue history for ' + mid)
            records = trusted_records_by_match[mid]
            stub = series_manifest(tid, mid, players, ['orc','orc'], seed)
            ordered = _player_records(records, stub, author_ids, as_of)
            reveals, _, _ = _setup(ordered, tid, mid, _time(start_at))
            manifest = series_manifest(tid, mid, players, [reveals.get('A','orc'),reveals.get('B','orc')], seed)
            round_as_of = starts[round_no] if round_no < len(starts) and _time(starts[round_no]) < now else as_of
            result = verify_series(manifest, records, engine, author_ids=author_ids,
                                   start_at=start_at, tie_favored=seeded_side(seed,mid,'series-tie'),
                                   as_of=round_as_of, clock_seconds=charter['clock_seconds'])
            if result['status'] == 'pending':
                raise ProtocolError('unresolved pairing: ' + mid)
            if result['status'] == 'played': played += 1
            winner = players[result['winner']] if result['winner'] else None
            results.append({**result, 'winner_key':winner, 'players':players})
            next_slots.append(winner)
        slots = next_slots
    winner = slots[0]
    bracket_hash = signing.digest(bracket_envelope)
    certificate = dict(protocol=PROTOCOL, tournament_id=tid, bracket_hash=bracket_hash,
                       winner=winner, played_series=played, matches=results)
    result_hash = signing.digest(certificate)
    claim_ok = False; destination = None
    if claim is not None:
        if winner is None or played == 0: raise ProtocolError('no played-series winner eligible for prize')
        p = _signed(claim, winner)
        _fields(p, 'kind tournament_id bracket_hash result_hash payout_chain_id payout_address')
        expected = payout_claim_payload(tid,bracket_hash,result_hash,p['payout_address'],p['payout_chain_id'])
        if p != expected: raise ProtocolError('payout claim context mismatch')
        destination = dict(chain_id=p['payout_chain_id'],address=p['payout_address'])
        claim_ok = True
    return dict(certificate=certificate, result_hash=result_hash,
                winner_eligible=winner is not None and played > 0,
                claim_verified=claim_ok, payout_eligible=claim_ok and played > 0,
                payout_destination=destination, funds_transferred=False)
