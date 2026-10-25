#!/usr/bin/env python3
"""Local two-key rehearsal; produces no real registration or public messages."""
import argparse
import copy
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from engine import Engine
import signing
from replay import verify_match


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('transcript', type=Path)
    ap.add_argument('--output', type=Path)
    args = ap.parse_args()
    source = signing.parse(args.transcript.read_text())
    keys = {a: Ed25519PrivateKey.generate() for a in ('A', 'B')}
    manifest = {'protocol': 'HXO-v0', 'tournament_id': 'LOCAL-REHEARSAL-NOT-A-TOURNAMENT',
                'match_id': 'rehearsal', 'players': {a: signing.public_hex(k) for a,k in keys.items()},
                'wildcard': source['wildcard'], 'mirror_first': source['mirror_first']}
    mh = signing.digest(manifest)
    previous, envelopes = mh, []
    for seq, batch in enumerate(source['batches']):
        payload = {'kind': 'batch', 'manifest_hash': mh, 'seq': seq, 'previous': previous,
                   'actor': batch['actor'], 'actions': batch['actions']}
        envelope = signing.sign(payload, keys[batch['actor']])
        envelopes.append(envelope)
        previous = signing.digest(envelope)
    with Engine() as engine:
        result = verify_match(manifest, envelopes, engine)
        assert result['result'] == source['result']
        bad = copy.deepcopy(envelopes)
        bad[0]['payload']['actions'] = {}
        try:
            verify_match(manifest, bad, engine)
        except ValueError:
            pass
        else:
            raise AssertionError('tampered signed batch accepted')
        wrong = copy.deepcopy(manifest)
        wrong['match_id'] = 'different-match'
        try:
            verify_match(wrong, envelopes, engine)
        except ValueError:
            pass
        else:
            raise AssertionError('cross-match replay accepted')
        try:
            verify_match(manifest, envelopes[:-1], engine)
        except ValueError:
            pass
        else:
            raise AssertionError('truncated result accepted')
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({'demo_only': True, 'manifest': manifest,
                                         'envelopes': envelopes, 'verification': result}, indent=2)+'\n')
    print(json.dumps({'gameplay_verified': True, 'signed_batches': len(envelopes),
                      'result': result['result'], 'tampering_rejected': True,
                      'cross_match_replay_rejected': True, 'truncation_rejected': True,
                      'payout_authorized': False}, indent=2))


if __name__ == '__main__': main()
