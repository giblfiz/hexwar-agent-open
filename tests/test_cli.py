import json
import contextlib
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import cli
import protocol
import signing
from engine import Engine, Series, ENGINE_SHA256, SNAPSHOT_SHA256
from cli import replay_prefix


ROOT = Path(__file__).resolve().parents[1]
ADDRESS = "0x1111111111111111111111111111111111111111"


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.a_path = self.work / "a.key"
        self.b_path = self.work / "b.key"
        self.a = json.loads(self.run_cli("keygen", "--key", self.a_path).stdout)["public_key"]
        self.b = json.loads(self.run_cli("keygen", "--key", self.b_path).stdout)["public_key"]
        self.manifest = {"protocol": "HXO-v0", "tournament_id": "test-open", "match_id": "r1-m1",
                         "players": {"A": self.a, "B": self.b}, "wildcard": ["fey", "orc"],
                         "mirror_first": "A"}

    def run_cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(ROOT / "cli.py"), *map(str, args)],
                                cwd=self.work, text=True, capture_output=True, timeout=25)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
        return result

    def envelope(self, result):
        self.assertTrue(result.stdout.startswith("HXO1 "))
        return signing.parse(result.stdout[5:])

    def transcript(self, batches):
        path = self.work / "transcript.json"
        path.write_text(json.dumps({"manifest": self.manifest, "batches": batches}))
        return path

    def test_private_key_is_mode600_and_never_overwritten(self):
        before = self.a_path.read_bytes()
        self.assertEqual(len(before), 32)
        self.assertEqual(stat.S_IMODE(self.a_path.stat().st_mode), 0o600)
        self.run_cli("keygen", "--key", self.a_path, ok=False)
        self.assertEqual(self.a_path.read_bytes(), before)
        self.a_path.chmod(0o644)
        self.run_cli("batch", "--key", self.a_path, "--transcript", self.transcript([]), ok=False)

    def test_registration_binds_account_and_payout_and_verifies(self):
        envelope = self.envelope(self.run_cli(
            "register", "--key", self.a_path, "--tournament-id", "test-open",
            "--charter-hash", "c" * 64, "--account-id", "agent-alice",
            "--payout-address", ADDRESS,
        ))
        payload = signing.verify(envelope, self.a)
        self.assertEqual(payload["account_id"], "agent-alice")
        self.assertEqual(payload["payout_address"].lower(), ADDRESS)
        self.assertEqual(payload["charter_hash"], "c" * 64)
        self.assertNotIn(self.a_path.read_bytes().hex(), json.dumps(envelope))

    def test_commit_reveal_preserves_private_nonce_until_reveal(self):
        secret_path = self.work / "choice.secret"
        arguments = ("commit", "--key", self.a_path, "--tournament-id", "test-open",
                     "--match-id", "r1-m1", "--actor", "A", "--faction", "fey",
                     "--secret", secret_path)
        first = self.run_cli(*arguments)
        commit = signing.verify(self.envelope(first), self.a)
        secret = json.loads(secret_path.read_text())
        self.assertEqual(stat.S_IMODE(secret_path.stat().st_mode), 0o600)
        self.assertNotIn(secret["nonce"], first.stdout)
        self.assertNotIn("faction", commit)
        second = self.run_cli(*arguments)
        self.assertEqual(first.stdout, second.stdout)
        reveal = signing.verify(self.envelope(self.run_cli(
            "reveal", "--key", self.a_path, "--secret", secret_path)), self.a)
        self.assertEqual(reveal["nonce"], secret["nonce"])
        self.assertEqual(reveal["faction"], "fey")
        self.run_cli("reveal", "--key", self.b_path, "--secret", secret_path, ok=False)

    def test_two_cli_batches_replay_and_partial_override_is_rejected(self):
        first = self.envelope(self.run_cli("batch", "--key", self.a_path,
                                          "--transcript", self.transcript([])))
        second_path = self.transcript([first])
        second = self.envelope(self.run_cli("batch", "--key", self.b_path,
                                           "--transcript", second_path))
        self.assertEqual(len(second["payload"]["actions"]), 7)
        with Engine() as engine:
            series, previous = replay_prefix(self.manifest, [first, second], engine)
            self.assertEqual(series.batch_index, 2)
            self.assertEqual(previous, signing.digest(second))
        partial = self.work / "partial.json"
        partial.write_text(json.dumps({"g2": second["payload"]["actions"]["g2"]}))
        self.run_cli("batch", "--key", self.b_path, "--transcript", second_path,
                     "--actions", partial, ok=False)
        self.run_cli("batch", "--key", self.a_path, "--transcript", second_path, ok=False)

    def test_search_strategy_produces_legal_signed_batch(self):
        envelope = self.envelope(self.run_cli("batch", "--key", self.a_path,
            "--transcript", self.transcript([]), "--strategy", "search", "--budget", "200"))
        with Engine() as engine:
            series, _ = replay_prefix(self.manifest, [envelope], engine)
            self.assertEqual(series.batch_index, 1)

    def test_tampered_prefix_rejected_before_signing(self):
        envelope = self.envelope(self.run_cli("batch", "--key", self.a_path,
                                            "--transcript", self.transcript([])))
        envelope["payload"]["seq"] = 5
        self.run_cli("batch", "--key", self.b_path,
                     "--transcript", self.transcript([envelope]), ok=False)

    def test_claim_is_signed_request_bound_to_result_and_bracket(self):
        envelope = self.envelope(self.run_cli(
            "claim", "--key", self.a_path, "--tournament-id", "test-open",
            "--bracket-hash", "b" * 64, "--result-hash", "e" * 64,
            "--payout-address", ADDRESS,
        ))
        payload = signing.verify(envelope, self.a)
        self.assertEqual(payload["bracket_hash"], "b" * 64)
        self.assertEqual(payload["result_hash"], "e" * 64)
        self.assertEqual(payload["payout_address"].lower(), ADDRESS)

    def test_timeout_and_resignation_bind_verified_current_head(self):
        transcript = self.transcript([])
        timeout = signing.verify(self.envelope(self.run_cli(
            "timeout", "--key", self.b_path, "--transcript", transcript)), self.b)
        self.assertEqual(timeout, {"kind": "timeout", "manifest_hash": signing.digest(self.manifest),
            "seq": 0, "previous": signing.digest(self.manifest), "actor": "B"})
        self.run_cli("timeout", "--key", self.a_path, "--transcript", transcript, ok=False)
        for path, pub, actor in ((self.a_path, self.a, "A"), (self.b_path, self.b, "B")):
            resign = signing.verify(self.envelope(self.run_cli(
                "resign", "--key", path, "--transcript", transcript)), pub)
            self.assertEqual((resign["kind"], resign["actor"], resign["seq"]), ("resign", actor, 0))

    def venue_fixture(self):
        seed = "ab" * 32
        descriptor = {"tournament_id": "test-open", "match_id": "r1m1",
            "players": {"A": self.a, "B": self.b}, "author_ids": {"A": "agent-A", "B": "agent-B"},
            "seed": seed, "start_at": "2030-01-01T12:00:00Z"}
        start = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        def timestamp(offset):
            return (start + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")
        keys = {"A": signing.load_key(self.a_path), "B": signing.load_key(self.b_path)}
        def record(payload, actor, offset, ident):
            env = signing.sign(payload, keys[actor])
            return {"id": ident, "author_id": "agent-" + actor, "created_at": timestamp(offset),
                    "updated_at": None, "is_deleted": False, "content": "HXO1 " + signing.canonical(env).decode()}
        records = []
        for i, actor in enumerate(("A", "B")):
            records.append(record(protocol.commit_payload("test-open", "r1m1", actor, "orc", actor.lower() * 64), actor, -700 + i, "c" + actor))
        for i, actor in enumerate(("A", "B")):
            records.append(record(protocol.reveal_payload("test-open", "r1m1", actor, "orc", actor.lower() * 64), actor, -500 + i, "v" + actor))
            records.append(record(protocol.ready_payload("test-open", "r1m1", actor), actor, -400 + i, "r" + actor))
        manifest = protocol.series_manifest("test-open", "r1m1", descriptor["players"], ["orc", "orc"], seed)
        with Engine() as engine:
            series = Series(engine, manifest["wildcard"], manifest["mirror_first"])
            actions = series.legal_batch()
            first = {"kind": "batch", "manifest_hash": signing.digest(manifest), "seq": 0,
                     "previous": signing.digest(manifest), "actor": "A", "actions": actions}
            first_record = record(first, "A", 1, "b0")
            records.append(first_record)
            # Exact duplicate publication must not appear twice in the prefix.
            records.append({**first_record, "id": "duplicate-b0", "created_at": timestamp(2)})
            alternate = dict(actions)
            gid = next(iter(alternate))
            alternate[gid] = engine.legal(series.boards[gid]["state"])["actions"][-1]
            records.append(record({**first, "actions": alternate}, "A", 3, "later-fork"))
            series.apply_batch("A", actions)
            second = {"kind": "batch", "manifest_hash": signing.digest(manifest), "seq": 1,
                      "previous": signing.digest(signing.parse(first_record["content"][5:])),
                      "actor": "B", "actions": series.legal_batch()}
            records.append(record(second, "B", 4, "b1"))
        return descriptor, records, timestamp

    def test_sync_selects_earliest_accepted_branch_and_ignores_duplicate(self):
        descriptor, records, timestamp = self.venue_fixture()
        transcript, status = cli.sync_records(descriptor, list(reversed(records)), timestamp(10))
        self.assertEqual(status["batches"], 2)
        self.assertEqual([b["payload"]["seq"] for b in transcript["batches"]], [0, 1])
        self.assertEqual(status["next_actor"], "A")
        self.assertIsNone(status["timeout_claimable_by"])
        _, expired = cli.sync_records(descriptor, records, timestamp(2000))
        self.assertEqual(expired["timeout_claimable_by"], "B")

    def test_sync_command_fetches_actual_adapter_and_preserves_file_on_failure(self):
        from moltbook import MoltbookError
        descriptor, records, timestamp = self.venue_fixture()
        descriptor_path, output = self.work / "descriptor.json", self.work / "synced.json"
        descriptor_path.write_text(json.dumps(descriptor))
        comments = [{**r, "post_id": "official-thread", "parent_id": None} for r in records]
        argv = ["sync", "--descriptor", str(descriptor_path), "--post-id", "official-thread", "--output", str(output)]
        with patch("moltbook.MoltbookClient.fetch_comments", return_value=comments) as fetch, \
                patch("cli.utc_now", return_value=timestamp(10)), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(argv), 0)
            fetch.assert_called_once_with("official-thread", max_pages=50, max_comments=4096)
        before = output.read_bytes()
        self.assertEqual(len(json.loads(before)["batches"]), 2)
        with patch("moltbook.MoltbookClient.fetch_comments", side_effect=MoltbookError("unavailable")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(argv), 2)
        self.assertEqual(output.read_bytes(), before)

    def test_sync_explains_not_started_and_setup_forfeit(self):
        descriptor, records, timestamp = self.venue_fixture()
        with self.assertRaisesRegex(ValueError, "appointment has not started"):
            cli.sync_records(descriptor, records, timestamp(-100))
        with self.assertRaisesRegex(ValueError, "empty / setup_no_show"):
            cli.sync_records(descriptor, [], timestamp(10))

    def test_descriptor_derives_keys_accounts_from_authenticated_roster(self):
        organizer_path = self.work / "organizer.key"
        organizer_public = signing.generate_key(organizer_path)
        organizer = signing.load_key(organizer_path)
        charter = signing.sign({"kind": "charter", "protocol": "HXO-v0", "tournament_id": "test-open",
            "engine_sha256": ENGINE_SHA256, "snapshot_sha256": SNAPSHOT_SHA256}, organizer)
        regs = [signing.sign(protocol.registration_payload("test-open", signing.digest(charter),
            "agent-" + actor, ADDRESS), signing.load_key(path))
            for actor, path in (("A", self.a_path), ("B", self.b_path))]
        roster = signing.sign({"kind": "roster", "tournament_id": "test-open", "charter_hash": signing.digest(charter),
            "registrations": [signing.digest(r) for r in regs]}, organizer)
        bracket = signing.sign({"kind": "bracket", "tournament_id": "test-open", "charter_hash": signing.digest(charter),
            "roster_hash": signing.digest(roster), "seed": "ab" * 32, "round_starts": ["2030-01-01T12:00:00Z"]}, organizer)
        for name, data in (("charter", charter), ("roster", roster), ("bracket", bracket), ("regs", regs)):
            (self.work / (name + ".json")).write_text(json.dumps(data))
        args = ["descriptor", "--charter", self.work / "charter.json", "--roster", self.work / "roster.json",
            "--bracket", self.work / "bracket.json", "--registrations", self.work / "regs.json",
            "--organizer-key", organizer_public, "--match-id", "r1m1"]
        descriptor = json.loads(self.run_cli(*args).stdout)
        self.assertEqual(set(descriptor["players"].values()), {self.a, self.b})
        for actor, key in descriptor["players"].items():
            self.assertEqual(descriptor["author_ids"][actor], "agent-A" if key == self.a else "agent-B")
        altered = json.loads((self.work / "roster.json").read_text())
        altered["payload"]["registrations"].pop()
        (self.work / "roster.json").write_text(json.dumps(altered))
        self.run_cli(*args, ok=False)


if __name__ == "__main__":
    unittest.main()
