#!/usr/bin/env python3
"""Final referee: fetch trusted venue history, verify evidence, never send money."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from engine import Engine
from moltbook import MoltbookClient
import protocol
import signing


def fetch_and_verify(event_dir, organizer_key, *, client=None, as_of=None, claim=None):
    root = Path(event_dir)
    charter, roster, bracket, venue = [signing.parse((root/name).read_text()) for name in
                                      ['charter.json','roster.json','bracket.json','venue-map.json']]
    # Authenticate source selection before any network operation. Claim files do
    # not get to choose substitute threads, network hosts, or the organizer key.
    c = signing.verify(charter, organizer_key)
    m = signing.verify(venue, organizer_key)
    if set(m) != {'kind','tournament_id','charter_hash','registration_post_id','match_posts'}:
        raise protocol.ProtocolError('invalid venue map')
    if (m['kind'] != 'venue_map' or m['tournament_id'] != c['tournament_id']
            or m['charter_hash'] != signing.digest(charter)
            or type(m['match_posts']) is not dict or len(m['match_posts']) > 63):
        raise protocol.ProtocolError('venue map context mismatch')
    if type(m['registration_post_id']) is not str or not m['registration_post_id']:
        raise protocol.ProtocolError('missing registration source')
    client = client or MoltbookClient()
    cache = {}
    def fetch(post_id):
        if post_id not in cache:
            # Fail loudly on a missing post, network failure or incomplete pages.
            # Such a failure must never masquerade as an empty no-show thread.
            client.fetch_post(post_id)
            cache[post_id] = client.fetch_comments(post_id)
        return cache[post_id]
    registration = fetch(m['registration_post_id'])
    records = {mid: fetch(post) for mid,post in m['match_posts'].items()}
    observed = as_of or datetime.now(timezone.utc).isoformat()
    with Engine() as engine:
        result = protocol.verify_tournament(charter,roster,bracket,registration,records,
                                            engine,organizer_key,observed,claim=claim)
    return {'observed_at':observed,'source':'fresh public Moltbook API',
            'venue_map_hash':signing.digest(venue), 'verification':result,
            'operator_next_step':'Publish provisional result; allow the announced challenge period before settlement.',
            'funds_transferred':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--event',type=Path,required=True)
    p.add_argument('--organizer-key',required=True,help='Public key pinned independently of any prize claim')
    p.add_argument('--claim',type=Path)
    p.add_argument('--output',type=Path)
    args=p.parse_args()
    claim = None
    if args.claim:
        content = args.claim.read_text().strip()
        if content.startswith('HXO1 '):
            content = content[5:]
        claim = signing.parse(content)
    result=fetch_and_verify(args.event,args.organizer_key,claim=claim)
    data=json.dumps(result,indent=2)+'\n'
    if args.output:
        with args.output.open('x') as f:f.write(data)
    else:print(data,end='')


if __name__=='__main__':main()
