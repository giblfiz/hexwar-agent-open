"""Beat-the-house bounty (HXB-v1): rules, signed records and an offline verifier.

A challenger plays 24 concurrent games against a frozen house bot. Everything
the house does is reproducible: its search settings are fixed here, and its
per-match tie-break seed comes from a secret committed in the signed acceptance
and revealed in the signed result. Anyone can replay a match record with the
pinned engine and confirm every challenger signature, every legal move, every
house move, and the score. No network access, no money movement.

Gameplay batches reuse the HXO-v0 batch payload unchanged (see WIRE.md).
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import secrets

import signing
from engine import Engine, Series, BatchError, ENGINE_SHA256, SNAPSHOT_SHA256

PROTOCOL = "HXB-v1"
BOUNTY_ID = "hexwar-bounty-v1"
# Hex War varies only which fixed army each side commands. Initiative between
# different factions is frozen (Orc > Fey > Necro), so there are six pairings
# (three cross, three mirror) and two seats in each: which army you command in a
# cross pairing, first or second move in a mirror. That is twelve cells; each is
# played twice. A is always the challenger, B the house.
PAIRINGS = [("orc", "necro"), ("orc", "fey"), ("fey", "necro"),
            ("orc", "orc"), ("necro", "necro"), ("fey", "fey")]
REPEATS = 2


def _layout():
    games = []
    for x, y in PAIRINGS:
        seats = [[x, y, "A"], [y, x, "A"]] if x != y else [[x, x, "A"], [x, x, "B"]]
        for seat in seats:
            games += [list(seat) for _ in range(REPEATS)]
    return games


GAMES = _layout()            # 24 boards, g1..g24
# Win bar: beat the house overall (17/24, ~70%) AND win at least once in every
# pairing-and-seat cell, so no favourable matchup can be farmed to the total.
WIN_THRESHOLD = 17
MIN_WINS_PER_CELL = 1
CLOCK_SECONDS = 14400        # challenger's total thinking time for the whole match
# The frozen house bot: the kit's built-in search player at a fixed setting.
# quiescence=False at this budget lost 0-24 to the same search with quiescence on
# (2026-10-29 ladder), so the frozen house searches captures to quiet positions.
HOUSE = {"policy": "tactical", "depth": 8, "node_budget": 100000, "quiescence": True}


def cell(game) -> str:
    """Pairing-and-seat label from the challenger's point of view."""
    a, b, first = game
    if a == b:
        return f"{a}-mirror/{'first' if first == 'A' else 'second'}"
    pair = "-".join(sorted((a, b), key=("orc", "fey", "necro").index))
    return f"{pair}/as-{a}"


CELLS = sorted({cell(g) for g in GAMES})
HANDLE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class BountyError(ValueError):
    pass


def utc(t: datetime | None = None) -> str:
    t = t or datetime.now(timezone.utc)
    return t.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


UTC_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


def micros(text: str) -> int:
    if not isinstance(text, str) or not UTC_TIME.fullmatch(text):
        raise BountyError("timestamps must be RFC3339 UTC ending in Z")
    t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    d = t - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (d.days * 86400 + d.seconds) * 1_000_000 + d.microseconds


def terms(house_key: str, house: dict = HOUSE) -> dict:
    """Everything a challenger agrees to by signing a request."""
    return {"protocol": PROTOCOL, "bounty_id": BOUNTY_ID, "house_key": house_key,
            "engine_sha256": ENGINE_SHA256, "snapshot_sha256": SNAPSHOT_SHA256,
            "games": GAMES, "win_threshold": WIN_THRESHOLD,
            "min_wins_per_cell": MIN_WINS_PER_CELL, "clock_seconds": CLOCK_SECONDS,
            "house": dict(house)}


def request_payload(terms_hash, github_handle, payout_address, client_nonce=None):
    if not isinstance(github_handle, str) or not HANDLE.fullmatch(github_handle):
        raise BountyError("invalid GitHub handle")
    if not isinstance(payout_address, str) or not ADDRESS.fullmatch(payout_address) \
            or int(payout_address[2:], 16) == 0:
        raise BountyError("invalid EVM payout address")
    client_nonce = client_nonce or secrets.token_hex(32)
    for v in (terms_hash, client_nonce):
        if not isinstance(v, str) or not HEX64.fullmatch(v):
            raise BountyError("expected 64 lowercase hex characters")
    return {"kind": "bounty_request", "bounty_id": BOUNTY_ID, "terms_hash": terms_hash,
            "github_handle": github_handle, "payout_chain_id": 1,
            "payout_address": payout_address, "client_nonce": client_nonce}


def seed_commitment(secret: str) -> str:
    return signing.digest({"domain": "HXB-v1/seed-commit", "secret": secret})


def house_seed(secret: str, client_nonce: str, gid: str) -> int:
    h = signing.digest({"domain": "HXB-v1/house-seed", "secret": secret,
                        "client_nonce": client_nonce, "game": gid})
    return int(h, 16) % (2 ** 53)


def check_request(envelope, house_key, house=HOUSE) -> dict:
    payload = signing.verify(envelope)
    expected = request_payload(signing.digest(terms(house_key, house)),
                               payload.get("github_handle"), payload.get("payout_address"),
                               payload.get("client_nonce"))
    if payload != expected:
        raise BountyError("request does not match current bounty terms")
    return payload


def acceptance_payload(request_envelope, house_key, secret, match_id, accepted_at, house=HOUSE):
    request = check_request(request_envelope, house_key, house)
    return {"kind": "bounty_match", "protocol": PROTOCOL, "bounty_id": BOUNTY_ID,
            "match_id": match_id, "terms_hash": request["terms_hash"],
            "request_hash": signing.digest(request_envelope),
            "players": {"A": request_envelope["public_key"], "B": house_key},
            "games": GAMES, "seed_commitment": seed_commitment(secret), "accepted_at": accepted_at}


class Match:
    """Replays or extends one match. Validates each envelope before committing."""

    def __init__(self, request_envelope, acceptance_envelope, engine, house_key, house=HOUSE):
        self.request = check_request(request_envelope, house_key, house)
        if request_envelope["public_key"] == house_key:
            raise BountyError("challenger key must differ from the house key")
        self.manifest = signing.verify(acceptance_envelope, house_key)
        if set(self.manifest) != {"kind", "protocol", "bounty_id", "match_id", "terms_hash",
                                  "request_hash", "players", "games", "seed_commitment", "accepted_at"}:
            raise BountyError("invalid acceptance fields")
        expected = dict(self.manifest, kind="bounty_match", protocol=PROTOCOL, bounty_id=BOUNTY_ID,
                        terms_hash=self.request["terms_hash"],
                        request_hash=signing.digest(request_envelope),
                        players={"A": request_envelope["public_key"], "B": house_key}, games=GAMES)
        if self.manifest != expected:
            raise BountyError("acceptance does not bind this request and these terms")
        micros(self.manifest["accepted_at"])
        self.request_envelope, self.acceptance = request_envelope, acceptance_envelope
        self.house = dict(house)
        self.engine = engine
        self.manifest_hash = signing.digest(self.manifest)
        self.series = Series(engine, games=GAMES)
        self.batches, self.timeline = [], []
        self.head = self.manifest_hash

    @property
    def next_actor(self):
        return self.series.next_actor

    def apply(self, envelope, at: str):
        actor = self.series.next_actor
        payload = signing.verify(envelope, self.manifest["players"][actor])
        if set(payload) != {"kind", "manifest_hash", "seq", "previous", "actor", "actions"}:
            raise BountyError("invalid batch fields")
        if (payload["kind"] != "batch" or payload["manifest_hash"] != self.manifest_hash
                or type(payload["seq"]) is not int or payload["seq"] != len(self.batches)
                or payload["previous"] != self.head or payload["actor"] != actor):
            raise BountyError("wrong batch context, sequence, parent or actor")
        self.series.apply_batch(actor, payload["actions"])
        self.batches.append(envelope)
        self.timeline.append(at)
        self.head = signing.digest(envelope)

    def batch_payload(self, actions):
        return {"kind": "batch", "manifest_hash": self.manifest_hash, "seq": len(self.batches),
                "previous": self.head, "actor": self.series.next_actor, "actions": actions}

    def house_actions(self, secret):
        """The frozen house bot's complete batch, deterministic given the secret."""
        return {gid: self.engine.ask({"op": "search", "state": self.series.boards[gid]["state"],
                                      "seed": house_seed(secret, self.request["client_nonce"], gid),
                                      **self.house})["action"]
                for gid in self.series.required_games()}

    def challenger_elapsed_us(self, now: str | None = None) -> int:
        """Server-observed challenger time: house publication (or acceptance) to receipt."""
        last, used = micros(self.manifest["accepted_at"]), 0
        for envelope, at in zip(self.batches, self.timeline):
            if envelope["payload"]["actor"] == "A":
                used += micros(at) - last
            last = micros(at)
        if now is not None and not self.series.finished and self.series.next_actor == "A":
            used += micros(now) - last
        return used

    def score(self):
        winners, cells = {}, {c: 0 for c in CELLS}
        for i, (gid, b) in enumerate(self.series.boards.items()):
            o = b["outcome"]
            # Unfinished boards (abandonment or timeout) are challenger losses.
            w = (b["owners"][o["winner"]] if o["status"] == "win" else
                 None if o["status"] == "draw" else "B")
            winners[gid] = w
            if w == "A":
                cells[cell(GAMES[i])] += 1
        wins = sum(1 for w in winners.values() if w == "A")
        return {"winners": winners, "challenger_wins": wins,
                "draws": sum(1 for w in winners.values() if w is None),
                "house_wins": sum(1 for w in winners.values() if w == "B"),
                "cell_wins": cells,
                "passed": wins >= WIN_THRESHOLD and min(cells.values()) >= MIN_WINS_PER_CELL}

    def result_payload(self, status, secret, finished_at):
        if status not in ("complete", "timeout"):
            raise BountyError("unknown result status")
        if (status == "complete") != self.series.finished:
            raise BountyError("status does not match board state")
        return {"kind": "bounty_result", "bounty_id": BOUNTY_ID,
                "match_id": self.manifest["match_id"], "manifest_hash": self.manifest_hash,
                "head": self.head, "batches": len(self.batches), "status": status,
                "house_secret": secret, "timeline": list(self.timeline),
                "finished_at": finished_at, "win_threshold": WIN_THRESHOLD,
                "min_wins_per_cell": MIN_WINS_PER_CELL,
                "github_handle": self.request["github_handle"],
                "payout_address": self.request["payout_address"],
                **self.score()}


def verify_record(record, house_key, *, house=HOUSE, check_house_moves=True, engine=None):
    """Independently verify a complete published match record.

    `house_key` and `house` must come from the published bounty terms, never from
    the record. Returns the verified result payload. Raises on any defect.
    """
    if type(record) is not dict or set(record) != {"request", "acceptance", "batches", "result"}:
        raise BountyError("record needs request, acceptance, batches and result")
    own = engine is None
    # Recomputing house moves can be slow on a loaded machine; never time out early.
    engine = engine or Engine(timeout=600)
    try:
        m = Match(record["request"], record["acceptance"], engine, house_key, house)
        result = signing.verify(record["result"], house_key)
        if seed_commitment(result.get("house_secret", "")) != m.manifest["seed_commitment"]:
            raise BountyError("revealed house secret does not match its commitment")
        timeline = result.get("timeline")
        if type(timeline) is not list or len(timeline) != len(record["batches"]):
            raise BountyError("timeline does not cover every batch")
        for envelope, at in zip(record["batches"], timeline):
            if check_house_moves and m.next_actor == "B":
                expected = m.house_actions(result["house_secret"])
                if envelope.get("payload", {}).get("actions") != expected:
                    raise BountyError(f"house batch {len(m.batches)} is not the frozen bot's move")
            try:
                m.apply(envelope, at)
            except (BatchError, ValueError) as exc:
                raise BountyError(f"batch {len(m.batches)}: {exc}") from exc
        times = [micros(m.manifest["accepted_at"])] + [micros(t) for t in timeline] \
            + [micros(result.get("finished_at"))]
        if times != sorted(times):
            raise BountyError("timeline is not in publication order")
        limit = CLOCK_SECONDS * 1_000_000
        if m.challenger_elapsed_us() > limit:
            raise BountyError("challenger batch accepted after clock expiry")
        status = result.get("status")
        if status == "timeout" and (m.series.finished or m.next_actor != "A"
                                    or m.challenger_elapsed_us(result["finished_at"]) <= limit):
            raise BountyError("timeout declared before the challenger's clock expired")
        expected = m.result_payload(status, result["house_secret"], result.get("finished_at"))
        if result != expected:
            raise BountyError("signed result disagrees with replay")
        return result
    finally:
        if own:
            engine.close()
