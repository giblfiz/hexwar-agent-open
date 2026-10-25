import copy
import os
from pathlib import Path
import tempfile
import unittest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import signing


class SigningTests(unittest.TestCase):
    def test_mutated_action_and_wrong_signer_rejected(self):
        key = Ed25519PrivateKey.generate()
        envelope = signing.sign({'match': 'm1', 'seq': 1, 'actions': {'g1': {'kind': 'pass'}}}, key)
        self.assertEqual(signing.verify(envelope), envelope['payload'])
        changed = copy.deepcopy(envelope)
        changed['payload']['match'] = 'm2'
        with self.assertRaises(ValueError): signing.verify(changed)
        with self.assertRaises(ValueError): signing.verify(envelope, '00'*32)

    def test_ambiguous_json_rejected(self):
        for source in ['{"a":1,"a":2}', '{"x":NaN}', '{"x":1.5}', '{"x":9007199254740992}']:
            with self.assertRaises(ValueError): signing.parse(source)
        self.assertEqual(signing.canonical({'b':2,'a':1}), signing.canonical({'a':1,'b':2}))

    def test_key_file_private_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'identity.key'
            pub = signing.generate_key(path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(signing.public_hex(signing.load_key(path)), pub)
            original = path.read_bytes()
            with self.assertRaises(FileExistsError): signing.generate_key(path)
            self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__': unittest.main()
