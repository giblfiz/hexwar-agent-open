"""The referee's source-selection boundary, using no live network."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import judge
from moltbook import MoltbookError
import signing


class FakeVenue:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
        self.rows = [{'id': 'authoritative-record'}]

    def fetch_post(self, post):
        self.calls.append(('post', post))
        if self.fail == 'post':
            raise MoltbookError('unavailable')
        return {'id': post}

    def fetch_comments(self, post):
        self.calls.append(('comments', post))
        if self.fail == 'comments':
            raise MoltbookError('incomplete pagination')
        return self.rows


class JudgeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = Ed25519PrivateKey.generate()
        self.pub = signing.public_hex(self.key)
        self.charter = signing.sign({'kind': 'charter', 'tournament_id': 'pilot'}, self.key)
        self.mapping = dict(kind='venue_map', tournament_id='pilot',
                            charter_hash=signing.digest(self.charter),
                            registration_post_id='official-thread',
                            match_posts={'r1m1': 'official-thread', 'r1m2': 'official-thread'})
        self.write('charter.json', self.charter)
        # Tournament semantic validation belongs to protocol; these are boundary fixtures.
        self.write('roster.json', signing.sign({'kind': 'roster'}, self.key))
        self.write('bracket.json', signing.sign({'kind': 'bracket'}, self.key))
        self.write('venue-map.json', signing.sign(self.mapping, self.key))
        self.engine_patch = patch('judge.Engine')
        self.engine = self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)
        self.verify_patch = patch('judge.protocol.verify_tournament', return_value={'winner_eligible': True})
        self.verify = self.verify_patch.start()
        self.addCleanup(self.verify_patch.stop)

    def write(self, name, data):
        (self.root / name).write_text(json.dumps(data))

    def run_judge(self, client, claim=None):
        return judge.fetch_and_verify(self.root, self.pub, client=client,
                                      as_of='2026-11-01T00:00:00Z', claim=claim)

    def test_forged_source_map_fails_before_any_network(self):
        attacker = Ed25519PrivateKey.generate()
        self.write('venue-map.json', signing.sign(self.mapping, attacker))
        client = FakeVenue()
        with self.assertRaises(ValueError):
            self.run_judge(client)
        self.assertEqual(client.calls, [])
        self.verify.assert_not_called()

    def test_forged_charter_fails_before_any_network(self):
        self.write('charter.json', signing.sign(self.charter['payload'], Ed25519PrivateKey.generate()))
        client = FakeVenue()
        with self.assertRaises(ValueError):
            self.run_judge(client)
        self.assertEqual(client.calls, [])

    def test_signed_map_wrong_context_fails_before_network(self):
        for field, value in [('charter_hash', '0' * 64), ('tournament_id', 'other'),
                             ('kind', 'claim'), ('registration_post_id', '')]:
            with self.subTest(field=field):
                mapping = dict(self.mapping, **{field: value})
                self.write('venue-map.json', signing.sign(mapping, self.key))
                client = FakeVenue()
                with self.assertRaises(ValueError):
                    self.run_judge(client)
                self.assertEqual(client.calls, [])
        self.verify.assert_not_called()

    def test_missing_post_or_failed_pages_never_becomes_empty_history(self):
        for stage in ['post', 'comments']:
            with self.subTest(stage=stage):
                with self.assertRaises(MoltbookError):
                    self.run_judge(FakeVenue(fail=stage))
        self.verify.assert_not_called()
        self.engine.assert_not_called()

    def test_shared_thread_fetched_once_and_full_records_forwarded(self):
        client = FakeVenue()
        result = self.run_judge(client)
        self.assertEqual(client.calls, [('post', 'official-thread'), ('comments', 'official-thread')])
        args = self.verify.call_args.args
        self.assertIs(args[3], client.rows)
        self.assertIs(args[4]['r1m1'], client.rows)
        self.assertIs(args[4]['r1m2'], client.rows)
        self.assertFalse(result['funds_transferred'])
        self.assertEqual(result['venue_map_hash'], signing.digest(signing.sign(self.mapping, self.key)))

    def test_claim_cannot_select_threads_or_network_hosts(self):
        claim = {'registration_post_id': 'attacker-thread',
                 'match_posts': {'r1m1': 'https://attacker.invalid'},
                 'organizer_key': signing.public_hex(Ed25519PrivateKey.generate())}
        client = FakeVenue()
        self.run_judge(client, claim)
        self.assertEqual(client.calls, [('post', 'official-thread'), ('comments', 'official-thread')])
        self.assertIs(self.verify.call_args.kwargs['claim'], claim)
        self.assertEqual(self.verify.call_args.args[6], self.pub)

    def test_cli_accepts_generated_hxo1_and_bare_json_claims(self):
        claim = signing.sign({'kind': 'payout_claim'}, self.key)
        path = self.root / 'claim.txt'
        for prefix in ['', 'HXO1 ']:
            with self.subTest(prefix=prefix):
                path.write_text(prefix + signing.canonical(claim).decode() + '\n')
                argv = ['judge.py', '--event', str(self.root), '--organizer-key', self.pub,
                        '--claim', str(path)]
                with patch('sys.argv', argv), patch('sys.stdout', new_callable=io.StringIO), \
                        patch('judge.fetch_and_verify', return_value={}) as verify:
                    judge.main()
                self.assertEqual(verify.call_args.kwargs['claim'], claim)


if __name__ == '__main__':
    unittest.main()
