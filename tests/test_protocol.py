import copy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import signing
import protocol as p
from engine import Engine, Series, ENGINE_SHA256, SNAPSHOT_SHA256

START = '2026-10-30T18:00:00Z'
AS_OF = '2026-11-01T18:00:00Z'
SEED = 'ab' * 32
ADDRESS = '0x' + '12' * 20


def stamp(seconds):
    return (datetime(2026,10,30,18,tzinfo=timezone.utc)+timedelta(seconds=seconds)).isoformat().replace('+00:00','Z')


def record(payload, key, actor, seconds, ident):
    envelope = signing.sign(payload,key)
    return dict(id=str(ident), author_id='agent-'+actor, created_at=stamp(seconds), updated_at=None,
                is_deleted=False, content='HXO1 '+signing.canonical(envelope).decode())


def setup_records(tid, mid, keys):
    records=[]
    for i,actor in enumerate(('A','B')):
        records.append(record(p.commit_payload(tid,mid,actor,'orc', ('a1' if actor=='A' else 'b2')*32),keys[actor],actor,-700+i,'c'+actor))
    for i,actor in enumerate(('A','B')):
        records.append(record(p.reveal_payload(tid,mid,actor,'orc', ('a1' if actor=='A' else 'b2')*32),keys[actor],actor,-598+i,'v'+actor))
    for i,actor in enumerate(('A','B')):
        records.append(record(p.ready_payload(tid,mid,actor),keys[actor],actor,-596+i,'r'+actor))
    return records


def gameplay(engine, manifest, keys):
    series=Series(engine,manifest['wildcard'],manifest['mirror_first'])
    previous=signing.digest(manifest); records=[]
    while not series.finished:
        actor=series.next_actor; actions=series.legal_batch(); seq=series.batch_index
        payload=dict(kind='batch',manifest_hash=signing.digest(manifest),seq=seq,previous=previous,actor=actor,actions=actions)
        r=record(payload,keys[actor],actor,seq+1,f'b{seq:03}')
        previous=signing.digest(signing.parse(r['content'][5:])); records.append(r)
        series.apply_batch(actor,actions)
    return records


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine=Engine()
        cls.keys={a:Ed25519PrivateKey.from_private_bytes(bytes([i+1])*32) for i,a in enumerate(('A','B'))}
        cls.manifest=p.series_manifest('test-open','r1m1',{a:signing.public_hex(k) for a,k in cls.keys.items()},['orc','orc'],SEED)
        cls.records=setup_records('test-open','r1m1',cls.keys)+gameplay(cls.engine,cls.manifest,cls.keys)

    @classmethod
    def tearDownClass(cls): cls.engine.close()

    def verify(self, records=None, manifest=None):
        return p.verify_series(manifest or self.manifest, self.records if records is None else records,self.engine,
                               author_ids={'A':'agent-A','B':'agent-B'},start_at=START,
                               tie_favored='B',as_of=AS_OF)

    def test_complete_signed_series(self):
        result=self.verify()
        self.assertEqual(result['status'],'played')
        self.assertEqual(sum(result['score2'].values()),14)
        self.assertLessEqual(result['batches'],85)

    def test_missing_commit_or_reveal_does_not_trap_honest_opponent(self):
        for missing in ('cB','vB','rB'):
            result=self.verify([r for r in self.records[:6] if r['id']!=missing])
            self.assertEqual((result['status'],result['winner']),('forfeit','A'))
        result=self.verify([])
        self.assertEqual((result['status'],result['winner']),('empty',None))

    def test_late_second_commit_cannot_steal_setup_forfeit(self):
        records=copy.deepcopy(self.records[:6])
        for r in records:
            if r['id']=='cB': r['created_at']=stamp(-1)
            if r['id']=='vB': r['created_at']=stamp(-0.8)
            if r['id']=='rB': r['created_at']=stamp(-0.6)
        result=self.verify(records)
        self.assertEqual((result['status'],result['winner']),('forfeit','A'))

    def test_commit_cutoff_exact_boundary_and_late_reveal(self):
        records=copy.deepcopy(self.records[:6])
        for r in records:
            if r['id']=='cB': r['created_at']=stamp(-600)
        self.assertEqual(self.verify(records)['status'],'pending')
        for r in records:
            if r['id']=='vB': r['created_at']=stamp(0)
        self.assertEqual(self.verify(records)['winner'],'A')

    def test_missing_history_is_not_double_no_show(self):
        args,_,_=self.tournament(); args[4]={}
        with self.assertRaises(p.ManualReview): p.verify_tournament(*args)

    def test_wrong_reveal_does_not_qualify(self):
        records=copy.deepcopy(self.records[:6])
        bad=p.reveal_payload('test-open','r1m1','B','fey','b2'*32)
        records[3]=record(bad,self.keys['B'],'B',-97,'vB')
        self.assertEqual(self.verify(records)['winner'],'A')

    def test_edited_or_deleted_official_records_need_manual_review(self):
        for change in ({'is_deleted':True},{'updated_at':stamp(1)}):
            records=copy.deepcopy(self.records)
            records[0].update(change)
            with self.assertRaises(p.ManualReview): self.verify(records)

    def test_signature_tamper_and_wrong_account_ignored(self):
        for variant in ('signature','account'):
            records=copy.deepcopy(self.records[:7])
            if variant=='account': records[-1]['author_id']='agent-B'
            else:
                envelope=signing.parse(records[-1]['content'][5:]); envelope['payload']['actions']={}
                records[-1]['content']='HXO1 '+signing.canonical(envelope).decode()
            result=self.verify(records)
            self.assertEqual(result['batches'],0)
            self.assertEqual(result['status'],'pending')

    def test_partial_does_not_stop_clock_and_exact_deadline_allowed(self):
        manifest_hash=signing.digest(self.manifest)
        partial=dict(kind='batch',manifest_hash=manifest_hash,seq=0,previous=manifest_hash,actor='A',actions={})
        first=signing.parse(self.records[6]['content'][5:])['payload']
        records=self.records[:6]+[record(partial,self.keys['A'],'A',1700,'bad'),record(first,self.keys['A'],'A',1800,'correct')]
        result=self.verify(records)
        self.assertEqual((result['status'],result['batches']),('pending',1))
        records[-1]['created_at']=stamp(1800.000001)
        self.assertEqual(self.verify(records)['batches'],0)

    def test_timeout_requires_opponent_signature_and_elapsed_budget(self):
        mh=signing.digest(self.manifest)
        timeout=dict(kind='timeout',manifest_hash=mh,seq=0,previous=mh,actor='B')
        before=self.records[:6]+[record(timeout,self.keys['B'],'B',1800,'t')]
        self.assertEqual(self.verify(before)['status'],'pending')
        after=self.records[:6]+[record(timeout,self.keys['B'],'B',1800.000001,'t')]
        result=self.verify(after)
        self.assertEqual((result['status'],result['winner'],result['reason']),('forfeit','B','clock_exhausted'))
        timeout['actor']='A'
        self.assertEqual(self.verify(self.records[:6]+[record(timeout,self.keys['A'],'A',2000,'t')])['status'],'pending')

    def test_earlier_valid_move_prevents_later_timeout_on_old_head(self):
        mh=signing.digest(self.manifest)
        timeout=dict(kind='timeout',manifest_hash=mh,seq=0,previous=mh,actor='B')
        records=self.records[:7]+[record(timeout,self.keys['B'],'B',2000,'t')]
        result=self.verify(records)
        self.assertEqual((result['status'],result['batches']),('pending',1))
        # Omitting that earlier publication produces another result; therefore the
        # final referee must fetch venue history rather than trust a claimant list.
        omitted=self.verify(self.records[:6]+records[-1:])
        self.assertEqual(omitted['status'],'forfeit')

    def test_tie_uses_predeclared_side(self):
        original=Series.result
        def tied(s):
            value=original(s); value['score2']={'A':7,'B':7}; value['winner']=None; return value
        with patch.object(Series,'result',tied):
            result=self.verify()
        self.assertEqual((result['winner'],result['reason']),('B','seeded_tie'))

    def test_no_claim_before_appointment(self):
        result=p.verify_series(self.manifest,[],self.engine,author_ids={'A':'agent-A','B':'agent-B'},
                               start_at=START,tie_favored='A',as_of=stamp(-1))
        self.assertEqual(result['status'],'pending')

    def tournament(self):
        organizer=Ed25519PrivateKey.from_private_bytes(b'\x09'*32)
        charter=signing.sign(dict(kind='charter',protocol='HXO-v0',tournament_id='test-open',engine_sha256=ENGINE_SHA256,
                                 snapshot_sha256=SNAPSHOT_SHA256,registration_deadline=stamp(-1000),clock_seconds=1800,
                                 prize={'chain_id':1,'asset':'ETH','amount_wei':'5000000000000000'}),organizer)
        regs=[]
        by_key={}
        for actor,key in self.keys.items():
            payload=p.registration_payload('test-open',signing.digest(charter),'agent-'+actor,ADDRESS)
            reg=record(payload,key,actor,-2000,'reg'+actor); regs.append(reg); by_key[signing.public_hex(key)]=(actor,key)
        roster=signing.sign(dict(kind='roster',tournament_id='test-open',charter_hash=signing.digest(charter),
                                registrations=[signing.digest(signing.parse(r['content'][5:])) for r in regs],frozen_at=stamp(-900)),organizer)
        bracket=signing.sign(dict(kind='bracket',tournament_id='test-open',charter_hash=signing.digest(charter),
                                 roster_hash=signing.digest(roster),seed=SEED,seed_published_at=stamp(-800),round_starts=[START]),organizer)
        slots=p.bracket_slots(list(by_key),SEED)
        keys={a:by_key[k][1] for a,k in zip(('A','B'),slots)}
        accounts={a:'agent-'+by_key[k][0] for a,k in zip(('A','B'),slots)}
        manifest=p.series_manifest('test-open','r1m1',dict(zip(('A','B'),slots)),['orc','orc'],SEED)
        records=setup_records('test-open','r1m1',keys)+gameplay(self.engine,manifest,keys)
        for r in records:
            actor=signing.parse(r['content'][5:])['payload']['actor']; r['author_id']=accounts[actor]
        args=[charter,roster,bracket,regs,{'r1m1':records},self.engine,signing.public_hex(organizer),AS_OF]
        return args,by_key,organizer

    def test_whole_tournament_and_claim(self):
        args,by_key,_=self.tournament()
        result=p.verify_tournament(*args)
        self.assertTrue(result['winner_eligible']); self.assertFalse(result['payout_eligible'])
        winner=result['certificate']['winner']; key=by_key[winner][1]
        claim=signing.sign(p.payout_claim_payload('test-open',signing.digest(args[2]),result['result_hash'],ADDRESS),key)
        final=p.verify_tournament(*args,claim=claim)
        self.assertTrue(final['payout_eligible']); self.assertFalse(final['funds_transferred'])
        self.assertEqual(final['payout_destination']['address'],ADDRESS)
        wrong=signing.sign(p.payout_claim_payload('test-open',signing.digest(args[2]),'00'*32,ADDRESS),key)
        with self.assertRaises(p.ProtocolError): p.verify_tournament(*args,claim=wrong)

    def test_invented_opponent_and_match_rejected(self):
        args,_,organizer=self.tournament()
        args[4]['r1m99']=[]
        with self.assertRaises(p.ProtocolError): p.verify_tournament(*args)
        del args[4]['r1m99']
        roster=args[1]['payload']; roster['registrations'][0]='00'*32; args[1]=signing.sign(roster,organizer)
        with self.assertRaises(p.ProtocolError): p.verify_tournament(*args)

    def test_all_no_show_cannot_pay(self):
        args,_,_=self.tournament(); args[4]={'r1m1':[]}
        result=p.verify_tournament(*args)
        self.assertFalse(result['winner_eligible']); self.assertEqual(result['certificate']['played_series'],0)

    def test_zero_payout_destination_rejected(self):
        with self.assertRaises(p.ProtocolError):
            p.registration_payload('test-open','00'*32,'agent-A','0x'+'00'*20)
        with self.assertRaises(p.ProtocolError):
            p.payout_claim_payload('test-open','00'*32,'11'*32,'0x'+'00'*20)

    def test_shared_round_threads_with_byes_and_whole_bracket(self):
        organizer=Ed25519PrivateKey.from_private_bytes(b'\x09'*32)
        charter=signing.sign(dict(kind='charter',protocol='HXO-v0',tournament_id='test-open',engine_sha256=ENGINE_SHA256,
                                 snapshot_sha256=SNAPSHOT_SHA256,registration_deadline=stamp(-1000),clock_seconds=1800,
                                 prize={'chain_id':1,'asset':'ETH','amount_wei':'5000000000000000'}),organizer)
        by_key={}; regs=[]
        for i in range(6):
            key=Ed25519PrivateKey.from_private_bytes(bytes([i+11])*32); label='k'+str(i)
            pub=signing.public_hex(key); by_key[pub]=(key,'agent-'+label)
            payload=p.registration_payload('test-open',signing.digest(charter),'agent-'+label,ADDRESS)
            regs.append(record(payload,key,label,-2000,'reg'+label))
        roster=signing.sign(dict(kind='roster',tournament_id='test-open',charter_hash=signing.digest(charter),
                                registrations=[signing.digest(signing.parse(r['content'][5:])) for r in regs],frozen_at=stamp(-900)),organizer)
        starts=[stamp(i*93600) for i in range(3)]
        bracket=signing.sign(dict(kind='bracket',tournament_id='test-open',charter_hash=signing.digest(charter),
                                 roster_hash=signing.digest(roster),seed=SEED,seed_published_at=stamp(-800),round_starts=starts),organizer)
        slots=p.bracket_slots(list(by_key),SEED); histories={}
        for ri,start_at in enumerate(starts):
            round_records=[]; mids=[]; winners=[]
            for i in range(0,len(slots),2):
                mid=f'r{ri+1}m{i//2+1}'; mids.append(mid); left,right=slots[i:i+2]
                if left is None or right is None:
                    winners.append(left or right); continue
                players={'A':left,'B':right}; keys={a:by_key[k][0] for a,k in players.items()}
                accounts={a:by_key[k][1] for a,k in players.items()}
                manifest=p.series_manifest('test-open',mid,players,['orc','orc'],SEED)
                records=setup_records('test-open',mid,keys)+gameplay(self.engine,manifest,keys)
                for r in records:
                    actor=signing.parse(r['content'][5:])['payload']['actor']; r['author_id']=accounts[actor]
                    r['id']=mid+'-'+r['id']
                    dt=datetime.fromisoformat(r['created_at'].replace('Z','+00:00'))+timedelta(seconds=ri*93600)
                    r['created_at']=dt.isoformat().replace('+00:00','Z')
                result=p.verify_series(manifest,records,self.engine,author_ids=accounts,start_at=start_at,
                                       tie_favored=p.seeded_side(SEED,mid,'series-tie'),as_of=stamp(300000))
                winners.append(players[result['winner']]); round_records.extend(records)
            for mid in mids: histories[mid]=round_records
            slots=winners
        result=p.verify_tournament(charter,roster,bracket,regs,histories,self.engine,
                                   signing.public_hex(organizer),stamp(300000))
        self.assertTrue(result['winner_eligible'])
        self.assertEqual(result['certificate']['played_series'],5)
        self.assertEqual(result['certificate']['winner'],slots[0])
        self.assertEqual(sum(r['status']=='bye' for r in result['certificate']['matches']),2)

    def test_seeded_slots_distribute_byes_without_empty_first_pairs(self):
        keys=[signing.public_hex(Ed25519PrivateKey.from_private_bytes(bytes([i+1])*32)) for i in range(5)]
        slots=p.bracket_slots(keys,SEED)
        self.assertEqual(len(slots),8); self.assertEqual(slots.count(None),3)
        self.assertEqual(set(k for k in slots if k),set(keys))
        self.assertTrue(all(slots[i] is not None or slots[i+1] is not None for i in range(0,8,2)))


if __name__=='__main__': unittest.main()
