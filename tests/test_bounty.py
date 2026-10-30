import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bounty
import bounty_server
from engine import Engine, Series, BatchError, INITIATIVE
import signing

TINY = {"policy": "tactical", "depth": 1, "node_budget": 50, "quiescence": False}
PAYOUT = "0x" + "11" * 20


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.t += timedelta(milliseconds=5)
        return bounty.utc(self.t)


def first_legal(engine, m):
    return {gid: engine.legal(m.series.boards[gid]["state"])["actions"][0]
            for gid in m.series.required_games()}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.clock = Clock()
        self.svc = bounty_server.Bounty(self.dir / "state", house=TINY, clock=self.clock)
        self.ckey_path = self.dir / "c.key"
        signing.generate_key(self.ckey_path)
        self.ckey = signing.load_key(self.ckey_path)
        self.engine = Engine()

    def tearDown(self):
        self.svc.close()
        self.engine.close()
        self.tmp.cleanup()

    def request(self, handle="octocat", key=None):
        payload = bounty.request_payload(self.svc.terms_hash, handle, PAYOUT)
        return signing.sign(payload, key or self.ckey)

    def open(self, handle="octocat"):
        req = self.request(handle)
        acc = self.svc.open_match(req)
        return req, acc, bounty.Match(req, acc, self.engine, self.svc.house_key, TINY)

    def play_out(self, req, acc, m, bot=first_legal):
        match_id = acc["payload"]["match_id"]
        state = {"status": "active"}
        while state["status"] == "active":
            state = self.svc.submit(match_id, signing.sign(m.batch_payload(bot(self.engine, m)), self.ckey))
            for env, at in zip(state["batches"][len(m.batches):], state["timeline"][len(m.timeline):]):
                m.apply(env, at)
        return {"request": req, "acceptance": acc, "batches": state["batches"], "result": state["result"]}


class LayoutTest(unittest.TestCase):
    def test_every_pairing_in_both_seats_twice(self):
        self.assertEqual(len(bounty.GAMES), 24)
        self.assertEqual(len(bounty.CELLS), 12)
        counts = {c: sum(1 for g in bounty.GAMES if bounty.cell(g) == c) for c in bounty.CELLS}
        self.assertEqual(set(counts.values()), {2})
        pairs = {tuple(sorted(g[:2])) for g in bounty.GAMES}
        self.assertEqual(len(pairs), 6)  # 3 cross + 3 mirror: every faction pairing
        with Engine() as engine:
            s = Series(engine, games=bounty.GAMES)
            firsts = [b["owners"][0] for b in s.boards.values()]
            self.assertEqual(firsts.count("A"), 12)
            for b in s.boards.values():
                f0, f1 = b["factions"]
                if f0 != f1:  # frozen initiative Orc > Fey > Necro
                    self.assertLess(INITIATIVE[f0], INITIATIVE[f1])
            with self.assertRaises(BatchError):
                Series(engine, games=[["orc", "elf", "A"]])
        default = None
        with Engine() as engine:
            default = Series(engine)
        self.assertEqual(len(default.boards), 7)  # pilot layout unchanged


class ScoreTest(Fixture):
    def set_outcomes(self, m, winners):
        for (gid, b), w in zip(m.series.boards.items(), winners):
            if w == "draw":
                b["outcome"] = {"status": "draw", "winner": None}
            elif w == "ongoing":
                b["outcome"] = {"status": "ongoing", "winner": None}
            else:
                b["outcome"] = {"status": "win", "winner": b["owners"].index(w)}

    def test_threshold_and_every_cell_rule(self):
        _, _, m = self.open()
        cases = [
            (["A"] * 24, True),
            # 17 wins: one per game pair (12 cells) plus 5 extra -> pass
            ([("A" if i % 2 == 0 or i < 10 else "B") for i in range(24)], True),
            # 16 wins covering every cell -> fail on total
            ([("A" if i % 2 == 0 or i < 8 else "B") for i in range(24)], False),
            # 22 wins but the last cell (both its games) lost -> fail
            (["A"] * 22 + ["B", "B"], False),
            # draws are not wins
            (["A"] * 16 + ["draw"] * 8, False),
            # unfinished boards count as house wins
            (["A"] * 16 + ["ongoing"] * 8, False),
        ]
        for winners, passed in cases:
            self.set_outcomes(m, winners)
            score = m.score()
            self.assertEqual(score["passed"], passed, winners)
        self.set_outcomes(m, ["A"] * 16 + ["ongoing"] * 8)
        self.assertEqual(m.score()["house_wins"], 8)


class EndToEndTest(Fixture):
    def test_full_match_verifies_and_tampering_fails(self):
        record = self.play_out(*self.open())
        result = bounty.verify_record(record, self.svc.house_key, house=TINY)
        self.assertEqual(result["status"], "complete")
        self.assertFalse(result["passed"])  # first-legal does not beat even the tiny house
        self.assertEqual(json.loads((self.dir / "state/records" / (result["match_id"] + ".json")).read_text()),
                         json.loads(json.dumps(record)))
        self.assertEqual(list((self.dir / "state/claims").glob("*.json")), [])
        key = self.svc.key

        def resign_result(change):
            bad = copy.deepcopy(record)
            payload = bad["result"]["payload"]
            change(payload)
            bad["result"] = signing.sign(payload, key)
            return bad

        # A house that flips its own score, even with a valid signature, is caught.
        bad = resign_result(lambda p: p.update(passed=True))
        with self.assertRaisesRegex(bounty.BountyError, "disagrees"):
            bounty.verify_record(bad, self.svc.house_key, house=TINY, check_house_moves=False)
        bad = resign_result(lambda p: p.update(house_secret="00" * 32))
        with self.assertRaisesRegex(bounty.BountyError, "commitment"):
            bounty.verify_record(bad, self.svc.house_key, house=TINY, check_house_moves=False)
        # A house batch that is legal and signed but not the frozen bot's choice.
        bad = copy.deepcopy(record)
        with Engine() as engine:
            m = bounty.Match(bad["request"], bad["acceptance"], engine, self.svc.house_key, TINY)
            m.apply(bad["batches"][0], record["result"]["payload"]["timeline"][0])
            payload = copy.deepcopy(bad["batches"][1]["payload"])
            gid = sorted(payload["actions"])[0]
            legal = engine.legal(m.series.boards[gid]["state"])["actions"]
            payload["actions"][gid] = next(a for a in legal if a != payload["actions"][gid])
        bad["batches"][1] = signing.sign(payload, key)
        with self.assertRaisesRegex(bounty.BountyError, "frozen bot|batch 2"):
            bounty.verify_record(bad, self.svc.house_key, house=TINY)
        # A challenger move altered after signing.
        bad = copy.deepcopy(record)
        bad["batches"][0]["signature"] = "0" * 128
        with self.assertRaises(ValueError):
            bounty.verify_record(bad, self.svc.house_key, house=TINY, check_house_moves=False)
        # Verifying against the real frozen settings rejects a tiny-house record.
        with self.assertRaises(bounty.BountyError):
            bounty.verify_record(record, self.svc.house_key, check_house_moves=False)

    def test_strong_challenger_flags_claim(self):
        def search(engine, m):
            return {gid: engine.ask({"op": "search", "state": m.series.boards[gid]["state"],
                                     "policy": "tactical", "depth": 4, "node_budget": 20000,
                                     "seed": 1, "quiescence": False})["action"]
                    for gid in m.series.required_games()}
        record = self.play_out(*self.open(), bot=search)
        result = bounty.verify_record(record, self.svc.house_key, house=TINY)
        self.assertTrue(result["passed"], result["challenger_wins"])
        claims = list((self.dir / "state/claims").glob("*.json"))
        self.assertEqual(len(claims), 1)
        claim = json.loads(claims[0].read_text())
        self.assertEqual(claim["payout_address"], PAYOUT)
        self.assertEqual(claim["record_hash"], signing.digest(json.loads(json.dumps(record))))
        self.assertEqual(self.svc.status()["status"], "claim_pending")


class ServiceRulesTest(Fixture):
    def test_refusals_and_limits(self):
        req, acc, m = self.open()
        with self.assertRaisesRegex(bounty_server.Refused, "active match"):
            self.svc.open_match(self.request("OctoCat"))  # case-insensitive handle
        with self.assertRaisesRegex(bounty.BountyError, "terms"):
            bad = bounty.request_payload("ab" * 32, "someone", PAYOUT)
            self.svc.open_match(signing.sign(bad, self.ckey))
        with self.assertRaises(bounty.BountyError):
            bounty.request_payload(self.svc.terms_hash, "bad handle!", PAYOUT)
        with self.assertRaises(bounty.BountyError):
            bounty.request_payload(self.svc.terms_hash, "ok", "0x" + "0" * 40)
        match_id = acc["payload"]["match_id"]
        # House-signed or wrongly chained batches are rejected without changing state.
        with self.assertRaises(bounty.BountyError):
            self.svc.submit(match_id, signing.sign(m.batch_payload(first_legal(self.engine, m)), self.svc.key))
        stale = m.batch_payload(first_legal(self.engine, m))
        stale["seq"] = 3
        with self.assertRaises(bounty.BountyError):
            self.svc.submit(match_id, signing.sign(stale, self.ckey))
        self.assertEqual(self.svc.view(match_id)["batches"], [])
        with self.assertRaises(KeyError):
            self.svc.view("m20261101T000000-deadbeef")
        with self.assertRaises(KeyError):
            self.svc.view("../../etc/passwd")

    def test_daily_limit_house_key_and_stop(self):
        self.svc.daily_per_handle = 1
        req, acc, m = self.open()
        self.clock.t += timedelta(seconds=bounty.CLOCK_SECONDS + 1)
        self.svc.expire_all()
        with self.assertRaisesRegex(bounty_server.Refused, "per handle"):
            self.svc.open_match(self.request())
        with self.assertRaisesRegex(bounty.BountyError, "differ"):
            self.svc.open_match(self.request("house", key=self.svc.key))
        (self.dir / "state/STOP").write_text("")
        with self.assertRaisesRegex(bounty_server.Refused, "closed"):
            self.svc.open_match(self.request("other"))
        self.assertEqual(self.svc.status()["status"], "closed")

    def test_timeout_is_a_verified_loss(self):
        req, acc, m = self.open()
        match_id = acc["payload"]["match_id"]
        state = self.svc.submit(match_id, signing.sign(m.batch_payload(first_legal(self.engine, m)), self.ckey))
        for env, at in zip(state["batches"], state["timeline"]):
            m.apply(env, at)
        self.clock.t += timedelta(seconds=bounty.CLOCK_SECONDS + 1)
        with self.assertRaisesRegex(bounty_server.Refused, "clock expired"):
            self.svc.submit(match_id, signing.sign(m.batch_payload(first_legal(self.engine, m)), self.ckey))
        view = self.svc.view(match_id)
        self.assertEqual(view["status"], "timeout")
        record = {k: view[k] for k in ("request", "acceptance", "batches", "result")}
        result = bounty.verify_record(record, self.svc.house_key, house=TINY)
        self.assertEqual((result["status"], result["challenger_wins"], result["passed"]), ("timeout", 0, False))
        # Declaring timeout early is detected even when the house signs it.
        payload = dict(result, finished_at=result["timeline"][-1])
        bad = dict(record, result=signing.sign(payload, self.svc.key))
        with self.assertRaisesRegex(bounty.BountyError, "timeout declared"):
            bounty.verify_record(bad, self.svc.house_key, house=TINY, check_house_moves=False)

    def test_restart_resumes_and_house_failure_voids(self):
        req, acc, m = self.open()
        match_id = acc["payload"]["match_id"]
        state = self.svc.submit(match_id, signing.sign(m.batch_payload(first_legal(self.engine, m)), self.ckey))
        for env, at in zip(state["batches"], state["timeline"]):
            m.apply(env, at)
        self.svc.close()
        self.svc = bounty_server.Bounty(self.dir / "state", house=TINY, clock=self.clock)
        self.assertIn(match_id, self.svc.live)
        _, engine, _ = self.svc.live[match_id]
        engine.close()  # simulate the house engine dying
        with self.assertRaisesRegex(bounty_server.Refused, "void"):
            self.svc.submit(match_id, signing.sign(m.batch_payload(first_legal(self.engine, m)), self.ckey))
        self.assertEqual(self.svc.view(match_id)["status"], "void_house_error")
        self.svc.daily_per_handle = 1
        self.open()  # a voided match does not use up the daily allowance


class HttpTest(Fixture):
    def test_http_status_codes(self):
        server = bounty_server.ThreadingHTTPServer(("127.0.0.1", 0), bounty_server.handler_for(self.svc))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}"

        def call(method, path, body=None):
            data = None if body is None else (body if isinstance(body, bytes) else signing.canonical(body))
            try:
                with urllib.request.urlopen(urllib.request.Request(url + path, data=data, method=method)) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())
        try:
            code, info = call("GET", "/v1/bounty")
            self.assertEqual((code, info["status"], info["terms"]["win_threshold"]), (200, "open", 17))
            self.assertEqual(call("POST", "/v1/matches", b"{not json")[0], 400)
            self.assertEqual(call("POST", "/v1/matches", b'{"a":1.5}')[0], 400)
            code, body = call("POST", "/v1/matches", self.request())
            self.assertEqual(code, 201)
            match_id = body["acceptance"]["payload"]["match_id"]
            self.assertEqual(call("POST", "/v1/matches", self.request())[0], 409)
            self.assertEqual(call("GET", "/v1/matches/m20261101T000000-deadbeef")[0], 404)
            code, view = call("GET", f"/v1/matches/{match_id}")
            self.assertEqual((code, view["next_actor"], view["result"]), (200, "A", None))
            self.assertNotIn("house_secret", json.dumps(view))
            self.assertEqual(call("GET", "/nope")[0], 404)
        finally:
            server.shutdown()
            server.server_close()


class BackgroundHouseTest(Fixture):
    """The public server: POST returns before the house replies (a proxy cuts long
    requests), clients poll GET, and a restart mid house turn resumes it."""

    def setUp(self):
        super().setUp()
        self.svc.close()
        self.svc = bounty_server.Bounty(self.dir / "state", house=TINY, clock=self.clock,
                                        background=True)

    def test_post_returns_then_house_replies(self):
        req, acc, m = self.open()
        match_id = acc["payload"]["match_id"]
        gate = threading.Event()
        real = bounty.Match.house_actions
        bounty.Match.house_actions = lambda self_, secret: (gate.wait(10), real(self_, secret))[1]
        try:
            state = self.svc.submit(match_id, signing.sign(
                m.batch_payload(first_legal(self.engine, m)), self.ckey))
            self.assertEqual((state["next_actor"], len(state["batches"])), ("B", 1))
            self.assertEqual(self.svc.view(match_id)["next_actor"], "B")  # GET never blocks
            with self.assertRaisesRegex(bounty_server.Refused, "not the challenger"):
                self.svc.submit(match_id, signing.sign(
                    m.batch_payload(first_legal(self.engine, m)), self.ckey))
            gate.set()
            self.svc.wait_house(match_id, 30)
        finally:
            bounty.Match.house_actions = real
        state = self.svc.view(match_id)
        self.assertEqual((state["next_actor"], len(state["batches"])), ("A", 2))

    def test_client_plays_full_match_over_http(self):
        import bounty_client
        bounty_client.POLL_SECONDS = 0.05
        server = bounty_server.ThreadingHTTPServer(("127.0.0.1", 0), bounty_server.handler_for(self.svc))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = bounty_client.play(
                f"http://127.0.0.1:{server.server_address[1]}", self.ckey, "octocat", PAYOUT,
                lambda engine: bounty_client.SearchBot(engine, 1, 50, False),
                self.dir / "rec.json", house=TINY)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result["status"], "complete")
        record = json.loads((self.dir / "rec.json").read_text())
        bounty.verify_record(record, self.svc.house_key, house=TINY)


if __name__ == "__main__":
    unittest.main()
