from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deliver
import event_operator as op
from moltbook import MoltbookError

NOW=datetime(2026,10,25,16,tzinfo=timezone.utc)
def iso(delta=0):return (NOW+timedelta(seconds=delta)).isoformat().replace('+00:00','Z')

class FakeClient:
    def __init__(self,response=None,error=None,verified=None,verify_error=None):
        self.response=response if response is not None else {'comment':{'id':'comment-1','created_at':iso()}}
        self.error=error;self.verified=verified if verified is not None else {'success':True};self.verify_error=verify_error
        self.calls=[]
    def create_comment(self,*args):
        self.calls.append(('comment',args))
        if self.error:raise self.error
        return self.response
    def create_post(self,*args):
        self.calls.append(('post',args))
        if self.error:raise self.error
        return self.response
    def verify_challenge(self,*args):
        self.calls.append(('verify',args))
        if self.verify_error:raise self.verify_error
        return self.verified

class DeliverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'outbox').mkdir()
        op._write(self.root/'delivery-config.json',{'credentials_path':'/tmp/unused','stop_at':iso(86400)})
    def item(self,name='a',**changes):
        item=dict(id=name,kind='comment',post_id='official-thread',content='HXO1 test',state='pending')
        item.update(changes);op._write(self.root/'outbox'/(name+'.json'),item);return item
    def get(self,name='a'):return op._read(self.root/'outbox'/(name+'.json'))

    def test_publishes_once_with_authoritative_time(self):
        self.item();client=FakeClient()
        result=deliver.tick(self.root,client=client,now=NOW)
        self.assertEqual(result['phase'],'published')
        self.assertEqual(self.get()['publication_id'],'comment-1')
        self.assertEqual(self.get()['published_at'],iso())
        deliver.tick(self.root,client=client,now=NOW+timedelta(seconds=70))
        self.assertEqual(len(client.calls),1)

    def test_dedup_returning_known_publication_is_not_published(self):
        (self.root/'outbox-resolved').mkdir()
        op._write(self.root/'outbox-resolved'/'old.json',{'id':'old','publication_id':'post-1'})
        self.item(kind='post',submolt='agents',title='t')
        client=FakeClient(response={'post':{'id':'post-1','created_at':iso()}})
        result=deliver.tick(self.root,client=client,now=NOW)
        self.assertEqual(result['phase'],'needs_review')
        self.assertEqual(self.get()['error'],'returned_existing_publication')

    def test_stale_returned_object_is_not_published(self):
        self.item(kind='post',submolt='agents',title='t')
        client=FakeClient(response={'post':{'id':'post-2','created_at':iso(-7200)}})
        result=deliver.tick(self.root,client=client,now=NOW)
        self.assertEqual(result['phase'],'needs_review')
        again=deliver.tick(self.root,client=client,now=NOW+timedelta(seconds=70))
        self.assertEqual(len(client.calls),1);self.assertEqual(again['needs_review'],['a'])

    def test_unknown_write_failure_is_not_retried_and_stays_visible(self):
        self.item();client=FakeClient(error=MoltbookError('private transport prose'))
        result=deliver.tick(self.root,client=client,now=NOW)
        self.assertEqual(result['phase'],'needs_review')
        again=deliver.tick(self.root,client=client,now=NOW+timedelta(seconds=70))
        self.assertEqual(again['phase'],'needs_review');self.assertEqual(again['needs_review'],['a'])
        self.assertEqual(len(client.calls),1)
        self.assertNotIn('private transport prose',json.dumps(self.get()))

    def test_interrupted_create_and_verification_never_retry(self):
        for name,state in [('a','sending'),('b','verifying_send')]:self.item(name,state=state)
        client=FakeClient()
        result=deliver.tick(self.root,client=client,now=NOW)
        self.assertEqual(result['phase'],'needs_review');self.assertEqual(client.calls,[])
        self.assertEqual(set(result['needs_review']),{'a','b'})

    def test_429_sets_global_cooldown_and_allows_later_retry(self):
        self.item('a');self.item('b')
        client=FakeClient(error=MoltbookError('rate limit',status=429,details={'retry_after_seconds':120}))
        result=deliver.tick(self.root,client=client,now=NOW)
        self.assertEqual(result['phase'],'pending');self.assertEqual(self.get()['next_at'],iso(125))
        again=deliver.tick(self.root,client=FakeClient(),now=NOW+timedelta(seconds=70))
        self.assertEqual(again['phase'],'cooldown')
        good=FakeClient(response={'comment':{'id':'comment-1','created_at':iso(126)}});deliver.tick(self.root,client=good,now=NOW+timedelta(seconds=126))
        self.assertEqual(len(good.calls),1);self.assertEqual(self.get()['state'],'published')
        self.assertEqual(self.get('b')['state'],'pending')

    def test_retry_metadata_is_defensive(self):
        for details in ({'retry_after_minutes':[]},{'retry_after_seconds':float('nan')},{'retry_after_seconds':True}):
            self.assertEqual(deliver._retry_delay(details),7205)
        self.assertEqual(deliver._retry_delay({'retry_after_minutes':'2'}),125)
        self.assertEqual(deliver._retry_delay({'rate_limits':{'Retry-After':'90'}}),95)

    def test_missing_platform_timestamp_never_becomes_local_timestamp(self):
        self.item();client=FakeClient(response={'comment':{'id':'known-id'}})
        result=deliver.tick(self.root,client=client,now=NOW)
        self.assertEqual(result['phase'],'needs_review')
        self.assertNotIn('published_at',self.get())
        self.assertEqual(self.get()['publication_id'],'known-id')
        self.assertEqual(len(client.calls),1)

    def test_malformed_response_and_unexpected_transport_failure_are_reviewable(self):
        self.item();client=FakeClient(response=['not-an-object'])
        self.assertEqual(deliver.tick(self.root,client=client,now=NOW)['phase'],'needs_review')
        self.item('b');client=FakeClient(error=TimeoutError('unknown completion'))
        self.assertEqual(deliver.tick(self.root,client=client,now=NOW)['phase'],'needs_review')
        self.assertEqual(self.get('b')['state'],'needs_review')

    def test_unicode_challenge_metadata_survives_and_verifies(self):
        self.item();response={'comment':{'id':'unicode-id','created_at':iso()},
                             'verification':{'verification_code':'code-1','challenge_text':'Twenty—five plus three',
                                             'hint':'Answer → numeric', 'metadata':{'score':0.5}}}
        client=FakeClient(response=response)
        result=deliver.tick(self.root,client=client,now=NOW)
        self.assertEqual(result['phase'],'published')
        self.assertEqual(client.calls[-1],('verify',('code-1','28.00')))
        self.assertEqual(self.get()['publication_id'],'unicode-id')
        self.assertNotIn('verification',self.get())

    def test_unsupported_challenge_preserves_known_id_for_review(self):
        self.item();response={'comment':{'id':'known-id','created_at':iso()},
                             'verification':{'verification_code':'code-1','challenge_text':'One hundred plus two → answer'}}
        client=FakeClient(response=response)
        result=deliver.tick(self.root,client=client,now=NOW)
        self.assertEqual(result['phase'],'needs_review')
        self.assertEqual(self.get()['publication_id'],'known-id')
        self.assertEqual(len(client.calls),1)
        self.assertIn('→',self.get()['verification']['challenge_text'])

    def test_challenge_requires_explicit_success(self):
        self.item();response={'comment':{'id':'known-id','created_at':iso()},
                             'verification':{'verification_code':'code-1','challenge_text':'Two plus three'}}
        client=FakeClient(response=response,verified={})
        result=deliver.tick(self.root,client=client,now=NOW)
        self.assertEqual(result['phase'],'needs_review')
        deliver.tick(self.root,client=client,now=NOW+timedelta(seconds=70))
        self.assertEqual(len(client.calls),2)

    def test_verification_started_is_durable_before_request(self):
        self.item();response={'comment':{'id':'known-id','created_at':iso()},
                             'verification':{'verification_code':'code-1','challenge_text':'Two plus three'}}
        client=FakeClient(response=response)
        def verify(*args):
            self.assertEqual(self.get()['state'],'verifying_send')
            self.assertEqual(self.get()['publication_id'],'known-id')
            raise MoltbookError('ambiguous verification')
        client.verify_challenge=verify
        self.assertEqual(deliver.tick(self.root,client=client,now=NOW)['phase'],'needs_review')

    def test_invalid_outbox_never_touches_network(self):
        self.item(post_id='https://attacker.invalid')
        client=FakeClient()
        self.assertEqual(deliver.tick(self.root,client=client,now=NOW)['phase'],'needs_review')
        self.assertEqual(client.calls,[])

    def test_stop_and_horizon_do_not_load_credentials_or_post(self):
        self.item();client=FakeClient();(self.root/'STOP').touch()
        self.assertEqual(deliver.tick(self.root,client=client,now=NOW)['phase'],'stopped')
        (self.root/'STOP').unlink()
        self.assertEqual(deliver.tick(self.root,client=client,now=NOW+timedelta(days=1))['phase'],'delivery_horizon_ended')
        self.assertEqual(client.calls,[])

    def test_local_state_json_rejects_duplicates_and_nonfinite_numbers(self):
        for text in ('{"a":1,"a":2}','{"score":NaN}'):
            path=self.root/'bad.json';path.write_text(text)
            with self.assertRaises(ValueError):op._read(path)
        with self.assertRaises(ValueError):op._write(self.root/'nan.json',{'score':float('nan')})

class ChallengeTests(unittest.TestCase):
    def test_narrow_arithmetic_and_obfuscated_words(self):
        for text,expected in [('Twenty three plus five','28.00'),('Twenty-five plus three','28.00'),
                              ('t]w]e]n]t]y f]i]v]e plus three','28.00'),('10 minus 3','7.00'),
                              ('7 times 9','63.00'),('107 divided by 40','2.68'),('100 plus 11','111.00'),
                              ('A] lOoObSst-Errr ] sWImMs^ aT/ TwEnTy ThReE ] cEnTiMeTeRsPeR/ sEcOnD ~ BuT^ dUrInG dRaG iT lOosEs] SeVeN ] cEnTiMeTeRsPeR] sEcOnD, wHaT/ iS ] tHe^ nEw veLoAwCiTeE?','16.00'),
                              ('tWeNnTyy fIvE pLuUs tHrEe','28.00')]:
            self.assertEqual(deliver.solve_challenge(text),expected)
    def test_ambiguous_math_goes_to_review(self):
        for text in ('2.5 plus 3.5','-2 plus 3','one hundred plus two','subtract two from five',
                     'two lobsters each have three claws what total','five plus negative two',
                     'divide two into ten','2 plus 3 times 4','five plus six plus seven'):
            with self.assertRaises(ValueError):deliver.solve_challenge(text)

if __name__=='__main__':unittest.main()
