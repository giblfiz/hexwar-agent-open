"""Verify signed gameplay against an externally trusted match manifest.

This prototype verifies signatures and legal play only. It does NOT authenticate
platform timestamps, prove roster inclusion, enforce faction commit/reveal,
resolve a series tie, or authorize a prize payout. Those remain launch gates.
"""
import signing
from engine import Series


def verify_match(manifest, envelopes, engine):
    required = {'protocol', 'tournament_id', 'match_id', 'players', 'wildcard', 'mirror_first'}
    if type(manifest) is not dict or set(manifest) != required:
        raise ValueError('invalid manifest fields')
    if manifest['protocol'] != 'HXO-v0':
        raise ValueError('unsupported protocol')
    for field in ['tournament_id', 'match_id']:
        if type(manifest[field]) is not str or not manifest[field]:
            raise ValueError('missing context identifier')
    players = manifest['players']
    if type(players) is not dict or set(players) != {'A', 'B'} or players['A'] == players['B']:
        raise ValueError('two distinct entrant keys required')
    manifest_hash = signing.digest(manifest)
    previous = manifest_hash
    series = Series(engine, wildcard=manifest['wildcard'], mirror_first=manifest['mirror_first'])
    if type(envelopes) is not list or len(envelopes) > 85:
        raise ValueError('invalid batch collection')
    for seq, envelope in enumerate(envelopes):
        actor = series.next_actor
        payload = signing.verify(envelope, players[actor])
        if set(payload) != {'kind', 'manifest_hash', 'seq', 'previous', 'actor', 'actions'}:
            raise ValueError('invalid batch fields')
        if (payload['kind'] != 'batch' or payload['manifest_hash'] != manifest_hash
                or type(payload['seq']) is not int or payload['seq'] != seq
                or payload['previous'] != previous or payload['actor'] != actor):
            raise ValueError('wrong batch context, sequence or parent')
        series.apply_batch(actor, payload['actions'])
        previous = signing.digest(envelope)
    if not series.finished:
        raise ValueError('incomplete gameplay record')
    return {'gameplay_verified': True, 'payout_authorized': False,
            'manifest_hash': manifest_hash, 'final_record_hash': previous,
            'result': series.result(),
            'unverified': ['venue history and timestamps', 'registration and bracket qualification',
                           'wildcard selection protocol', 'series tiebreak if required']}
