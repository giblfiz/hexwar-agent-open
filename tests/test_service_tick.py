import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import service_tick

class TickTests(unittest.TestCase):
    def test_stop_prevents_any_launch_read(self):
        with tempfile.TemporaryDirectory() as d, patch('service_tick.deliver.tick',return_value={'phase':'stopped'}), patch('service_tick.op._read',side_effect=AssertionError('must not read')), patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(service_tick.run(d),0)
            self.assertEqual(json.loads((Path(d)/'.operator/last-tick.json').read_text())['delivery']['phase'],'stopped')

    def test_launch_failure_never_opens_registration(self):
        with tempfile.TemporaryDirectory() as d, patch('service_tick.deliver.tick',return_value={'phase':'needs_review'}), patch('service_tick.op._read',return_value={'state':'needs_review'}), patch('service_tick.op.poll',side_effect=AssertionError('must not poll')), patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(service_tick.run(d),0)
            self.assertEqual(json.loads((Path(d)/'.operator/last-tick.json').read_text())['operator']['phase'],'awaiting_registration_announcement')

    def test_published_launch_checked_before_poll(self):
        with tempfile.TemporaryDirectory() as d, patch('service_tick.deliver.tick',return_value={'phase':'outbox_idle'}), patch('service_tick.op._read',side_effect=[{'state':'published','publication_id':'official','content':'our text'}, {'registration_post_id':''}]), patch('service_tick.MoltbookClient') as client, patch('service_tick.op.poll',return_value={'phase':'registration_open'}) as poll, patch('sys.stdout',new_callable=io.StringIO):
            client.return_value.fetch_post.return_value={'content':'our text'}
            self.assertEqual(service_tick.run(d),0)
            self.assertEqual(json.loads((Path(d)/'operator-config.json').read_text())['registration_post_id'],'official')
            poll.assert_called_once()

    def test_mismatched_launch_cannot_open_registration(self):
        with tempfile.TemporaryDirectory() as d, patch('service_tick.deliver.tick',return_value={'phase':'outbox_idle'}), patch('service_tick.op._read',side_effect=[{'state':'published','publication_id':'official','content':'our text'}, {'registration_post_id':''}]), patch('service_tick.MoltbookClient') as client, patch('service_tick.op.poll',side_effect=AssertionError('must not poll')), patch('sys.stdout',new_callable=io.StringIO):
            client.return_value.fetch_post.return_value={'content':'different'}
            self.assertEqual(service_tick.run(d),1)
            self.assertFalse((Path(d)/'operator-config.json').exists())
