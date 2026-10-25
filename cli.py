#!/usr/bin/env python3
"""Offline entrant client. Prints official signed records; never posts them."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile

import signing
from engine import Engine, Series

DEFAULT_KEY = Path(".hxo-private/identity.key")
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


def read_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("input document exceeds 8 MiB")
    text = raw.decode("ascii")
    if text.startswith("HXO1 "):
        text = text[5:]
    return signing.parse(text)


def require_hex(value, length, name):
    if not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{length}}}", value):
        raise ValueError(f"{name} must be {length} lowercase hexadecimal characters")
    return value


def private_parent(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)


def load_private_key(path):
    path = Path(path)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("identity key permissions must be 0600; run chmod 600 on the key file")
    return signing.load_key(path)


def write_secret(path, document):
    path = Path(path)
    private_parent(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(signing.canonical(document) + b"\n")


def read_secret(path):
    if stat.S_IMODE(Path(path).stat().st_mode) & 0o077:
        raise ValueError("wildcard secret permissions must be 0600")
    secret = read_json(path)
    if not isinstance(secret, dict) or set(secret) != {
            "tournament_id", "match_id", "actor", "public_key", "faction", "nonce"}:
        raise ValueError("invalid wildcard secret file")
    require_hex(secret["nonce"], 64, "nonce")
    return secret


def replay_prefix(manifest, envelopes, engine):
    """Verify gameplay prefix, NOT venue timing, setup or bracket qualification."""
    required = {"protocol", "tournament_id", "match_id", "players", "wildcard", "mirror_first"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ValueError("invalid manifest fields")
    if manifest["protocol"] != "HXO-v0":
        raise ValueError("unsupported protocol")
    for field in ("tournament_id", "match_id"):
        if not isinstance(manifest[field], str) or not manifest[field]:
            raise ValueError("missing context identifier")
    players = manifest["players"]
    if not isinstance(players, dict) or set(players) != {"A", "B"} or players["A"] == players["B"]:
        raise ValueError("two distinct entrant keys required")
    for public_key in players.values():
        require_hex(public_key, 64, "player public key")
    if not isinstance(envelopes, list) or len(envelopes) > 85:
        raise ValueError("invalid batch collection")
    manifest_hash = signing.digest(manifest)
    previous = manifest_hash
    series = Series(engine, wildcard=manifest["wildcard"], mirror_first=manifest["mirror_first"])
    for seq, envelope in enumerate(envelopes):
        actor = series.next_actor
        payload = signing.verify(envelope, players[actor])
        if set(payload) != {"kind", "manifest_hash", "seq", "previous", "actor", "actions"}:
            raise ValueError("invalid batch fields")
        if (payload["kind"] != "batch" or payload["manifest_hash"] != manifest_hash
                or type(payload["seq"]) is not int or payload["seq"] != seq
                or payload["previous"] != previous or payload["actor"] != actor):
            raise ValueError("wrong batch context, sequence or parent")
        series.apply_batch(actor, payload["actions"])
        previous = signing.digest(envelope)
    return series, previous


def emit(envelope, output=None):
    wire = "HXO1 " + signing.canonical(envelope).decode("ascii") + "\n"
    if output:
        # Refuse accidental replacement of a record the caller may have posted.
        with Path(output).open("x", encoding="ascii") as out:
            out.write(wire)
    else:
        sys.stdout.write(wire)


def command_keygen(args):
    private_parent(args.key)
    public_key = signing.generate_key(args.key)
    print(json.dumps({"public_key": public_key, "key_file": str(args.key)}))


def command_register(args):
    import protocol
    require_hex(args.charter_hash, 64, "charter hash")
    key = load_private_key(args.key)
    payload = protocol.registration_payload(
        args.tournament_id, args.charter_hash, args.account_id,
        args.payout_address, args.payout_chain_id,
    )
    emit(signing.sign(payload, key), args.output)


def command_commit(args):
    import protocol
    key = load_private_key(args.key)
    identity = {"tournament_id": args.tournament_id, "match_id": args.match_id,
                "actor": args.actor, "public_key": signing.public_hex(key), "faction": args.faction}
    secret_path = args.secret or Path(".hxo-private") / ("choice-" + signing.digest(identity) + ".json")
    if secret_path.exists():
        secret = read_secret(secret_path)
        if any(secret[k] != v for k, v in identity.items()):
            raise ValueError("secret file belongs to another choice or identity")
    else:
        secret = {**identity, "nonce": secrets.token_hex(32)}
        write_secret(secret_path, secret)
    payload = protocol.commit_payload(args.tournament_id, args.match_id, args.actor,
                                      args.faction, secret["nonce"])
    emit(signing.sign(payload, key), args.output)
    print(f"Private reveal material saved in {secret_path}", file=sys.stderr)


def command_reveal(args):
    import protocol
    key = load_private_key(args.key)
    secret = read_secret(args.secret)
    if secret["public_key"] != signing.public_hex(key):
        raise ValueError("secret file belongs to another identity key")
    payload = protocol.reveal_payload(secret["tournament_id"], secret["match_id"], secret["actor"],
                                      secret["faction"], secret["nonce"])
    emit(signing.sign(payload, key), args.output)


def command_ready(args):
    import protocol
    key = load_private_key(args.key)
    payload = protocol.ready_payload(args.tournament_id, args.match_id, args.actor)
    emit(signing.sign(payload, key), args.output)


def command_batch(args):
    key = load_private_key(args.key)
    transcript = read_json(args.transcript)
    if not isinstance(transcript, dict) or set(transcript) != {"manifest", "batches"}:
        raise ValueError("transcript must contain exactly manifest and batches")
    manifest = transcript["manifest"]
    with Engine() as engine:
        series, previous = replay_prefix(manifest, transcript["batches"], engine)
        if series.finished:
            raise ValueError("series is complete; no next batch")
        actor = series.next_actor
        if signing.public_hex(key) != manifest["players"][actor]:
            raise ValueError("this identity is not the next batch actor")
        if args.actions:
            actions = read_json(args.actions)
        elif args.strategy == "first-legal":
            actions = series.legal_batch()
        else:
            if args.budget <= 0 or args.budget > 10_000_000:
                raise ValueError("search budget must be between 1 and 10000000 nodes per game")
            actions = {}
            for gid in series.required_games():
                response = engine.ask({"op": "search", "state": series.boards[gid]["state"],
                                       "policy": "tactical", "depth": args.depth,
                                       "node_budget": args.budget, "seed": 0,
                                       "quiescence": False})
                actions[gid] = response["action"]
        # Validate the entire custom or searched batch before signing any record.
        seq = series.batch_index
        series.apply_batch(actor, actions)
        payload = {"kind": "batch", "manifest_hash": signing.digest(manifest),
                   "seq": seq, "previous": previous, "actor": actor, "actions": actions}
    emit(signing.sign(payload, key), args.output)


def command_claim(args):
    import protocol
    require_hex(args.bracket_hash, 64, "bracket hash")
    require_hex(args.result_hash, 64, "result hash")
    key = load_private_key(args.key)
    payload = protocol.payout_claim_payload(args.tournament_id, args.bracket_hash, args.result_hash,
                                            args.payout_address, args.payout_chain_id)
    emit(signing.sign(payload, key), args.output)


def command_terminal(args):
    key = load_private_key(args.key)
    transcript = read_json(args.transcript)
    if not isinstance(transcript, dict) or set(transcript) != {"manifest", "batches"}:
        raise ValueError("transcript must contain exactly manifest and batches")
    manifest = transcript["manifest"]
    with Engine() as engine:
        series, previous = replay_prefix(manifest, transcript["batches"], engine)
        if series.finished:
            raise ValueError("series is complete")
        own_key = signing.public_hex(key)
        actors = [a for a, public_key in manifest["players"].items() if public_key == own_key]
        if len(actors) != 1:
            raise ValueError("this identity is not a participant")
        actor = actors[0]
        if args.command == "timeout" and actor == series.next_actor:
            raise ValueError("only the waiting opponent may claim a timeout")
        payload = {"kind": args.command, "manifest_hash": signing.digest(manifest),
                   "seq": series.batch_index, "previous": previous, "actor": actor}
    emit(signing.sign(payload, key), args.output)


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_venue_records(post_id):
    from moltbook import MoltbookClient, venue_record
    comments = MoltbookClient(timeout=5).fetch_comments(post_id, max_pages=50, max_comments=4096)
    return [venue_record(comment) for comment in comments]


def save_document(value, output=None):
    data = signing.canonical(value).decode("ascii") + "\n"
    if output is None:
        sys.stdout.write(data)
        return
    output = Path(output)
    with tempfile.NamedTemporaryFile("w", dir=output.parent, encoding="ascii", delete=False) as f:
        temporary = Path(f.name)
        f.write(data)
    try:
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def sync_records(descriptor, records, as_of):
    """Reconstruct from trusted complete venue data, coupled to local protocol.py.

    The public CLI fetches records directly. Descriptor provenance remains caller
    configuration; this does not establish organizer authority by itself.
    """
    import protocol as p
    fields = {"tournament_id", "match_id", "players", "author_ids", "seed", "start_at"}
    if type(descriptor) is not dict or set(descriptor) != fields:
        raise ValueError("descriptor needs tournament_id, match_id, players, author_ids, seed, start_at")
    d = descriptor
    stub = p.series_manifest(d["tournament_id"], d["match_id"], d["players"], ["orc", "orc"], d["seed"])
    p._manifest(stub)
    p._fields(d["author_ids"], "A B")
    for account in d["author_ids"].values(): p._text(account)
    if d["author_ids"]["A"] == d["author_ids"]["B"]:
        raise ValueError("descriptor requires distinct participant accounts")
    ordered = p._player_records(records, stub, d["author_ids"], as_of)
    reveals, ready, _ = p._setup(ordered, d["tournament_id"], d["match_id"], p._time(d["start_at"]))
    manifest = p.series_manifest(d["tournament_id"], d["match_id"], d["players"],
        [reveals.get("A", "orc"), reveals.get("B", "orc")], d["seed"])
    with Engine() as engine:
        result = p.verify_series(manifest, records, engine, author_ids=d["author_ids"],
            start_at=d["start_at"], tie_favored=p.seeded_side(d["seed"], d["match_id"], "series-tie"), as_of=as_of)
    if result["reason"] == "appointment_not_started":
        missing = sorted({"A", "B"} - ready)
        suffix = "; setup incomplete for " + ",".join(missing) if missing else "; both participants ready"
        raise ValueError("appointment has not started: " + d["start_at"] + suffix)
    if result["status"] in ("forfeit", "empty"):
        raise ValueError("series resolved: " + result["status"] + " / " + result["reason"]
                         + "; winner=" + str(result["winner"]))
    accepted = {(r["id"], r["hash"]) for r in result["records"]}
    batch_records = [r for r in ordered
                     if (r["id"], r["hash"]) in accepted and r["payload"].get("kind") == "batch"]
    batch_records.sort(key=lambda r: r["payload"]["seq"])
    batches = [r["envelope"] for r in batch_records]
    with Engine() as engine:
        series, _ = replay_prefix(manifest, batches, engine)
    if series.batch_index != result["batches"]:
        raise ValueError("canonical receipt and gameplay sequence disagree")
    if not series.finished:
        remaining = {"A": p.CLOCK_SECONDS * 1_000_000, "B": p.CLOCK_SECONDS * 1_000_000}
        last = p._time(d["start_at"])
        for r in batch_records:
            remaining[r["payload"]["actor"]] -= r["time"] - last
            last = r["time"]
        actor = series.next_actor
        remaining[actor] -= p._time(as_of) - last
        result = {**result, "next_actor": actor, "active_remaining_microseconds": remaining[actor],
                  "timeout_claimable_by": ("B" if actor == "A" else "A") if remaining[actor] < 0 else None}
    return {"manifest": manifest, "batches": batches}, result


def command_sync(args):
    descriptor = read_json(args.descriptor)
    records = fetch_venue_records(args.post_id)
    transcript, status = sync_records(descriptor, records, utc_now())
    save_document(transcript, args.output)
    print("Venue sync: " + status["status"] + " / " + status["reason"]
          + f"; {status['batches']} accepted batches. Descriptor provenance remains your responsibility.",
          file=sys.stderr)
    if status.get("next_actor"):
        print(f"Next actor: {status['next_actor']}; active clock remaining: "
              f"{status['active_remaining_microseconds'] / 1_000_000:.3f}s; "
              f"timeout claimable by: {status['timeout_claimable_by'] or 'neither'}", file=sys.stderr)


def command_descriptor(args):
    """First-round derivation from authenticated organizer records."""
    import protocol as p
    from engine import ENGINE_SHA256, SNAPSHOT_SHA256
    require_hex(args.organizer_key, 64, "organizer key")
    charter_env, roster_env, bracket_env = (read_json(path) for path in (args.charter, args.roster, args.bracket))
    charter, roster, bracket = (signing.verify(e, args.organizer_key) for e in (charter_env, roster_env, bracket_env))
    tid = charter.get("tournament_id")
    ch = signing.digest(charter_env)
    if (charter.get("kind") != "charter" or charter.get("protocol") != "HXO-v0"
            or charter.get("engine_sha256") != ENGINE_SHA256 or charter.get("snapshot_sha256") != SNAPSHOT_SHA256
            or roster.get("kind") != "roster" or roster.get("tournament_id") != tid
            or roster.get("charter_hash") != ch or bracket.get("kind") != "bracket"
            or bracket.get("tournament_id") != tid or bracket.get("charter_hash") != ch
            or bracket.get("roster_hash") != signing.digest(roster_env)):
        raise ValueError("organizer records disagree or use unsupported engine pins")
    if args.registrations:
        envelopes = read_json(args.registrations)
        if type(envelopes) is not list or len(envelopes) > 4096:
            raise ValueError("registrations must be a bounded list of signed envelopes")
    else:
        records = fetch_venue_records(args.post_id)
        accounts = {r["author_id"] for r in records if isinstance(r.get("author_id"), str)}
        envelopes = [r["envelope"] for r in p._records(records, accounts, utc_now())]
    expected = roster.get("registrations")
    if type(expected) is not list or not 2 <= len(expected) <= p.MAX_PLAYERS or len(set(expected)) != len(expected):
        raise ValueError("invalid signed roster registration list")
    available = {signing.digest(e): e for e in envelopes}
    entrants = {}
    for h in expected:
        require_hex(h, 64, "registration hash")
        if h not in available:
            raise ValueError("accepted registration envelope unavailable: " + h)
        envelope = available[h]
        registration = signing.verify(envelope)
        if registration != p.registration_payload(tid, ch, registration.get("account_id"),
                registration.get("payout_address"), registration.get("payout_chain_id")):
            raise ValueError("registration context mismatch")
        key = envelope["public_key"]
        if key in entrants: raise ValueError("duplicate roster identity")
        entrants[key] = registration
    if len({r["account_id"] for r in entrants.values()}) != len(entrants):
        raise ValueError("duplicate roster account")
    matched = re.fullmatch(r"r1m([1-9][0-9]*)", args.match_id)
    if not matched:
        raise ValueError("descriptor helper supports round one; later rounds require organizer-confirmed advancement")
    slots = p.bracket_slots(list(entrants), bracket["seed"])
    offset = 2 * (int(matched.group(1)) - 1)
    if offset >= len(slots): raise ValueError("match ID is outside the bracket")
    a, b = slots[offset:offset + 2]
    if a is None or b is None: raise ValueError("this pairing is a bye; no gameplay descriptor is needed")
    starts = bracket.get("round_starts")
    if type(starts) is not list or len(starts) != len(slots).bit_length() - 1:
        raise ValueError("invalid round appointments")
    p._time(starts[0])
    descriptor = {"tournament_id": tid, "match_id": args.match_id, "players": {"A": a, "B": b},
                  "author_ids": {"A": entrants[a]["account_id"], "B": entrants[b]["account_id"]},
                  "seed": bracket["seed"], "start_at": starts[0]}
    save_document(descriptor, args.output)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def command(name, function, description, signed=True):
        q = sub.add_parser(name, help=description)
        q.set_defaults(function=function)
        q.add_argument("--key", type=Path, default=DEFAULT_KEY)
        if signed:
            q.add_argument("--output", type=Path, help="save HXO1 record; refuse to overwrite")
        return q

    def context(q, match=False):
        q.add_argument("--tournament-id", required=True)
        if match:
            q.add_argument("--match-id", required=True)
            q.add_argument("--actor", choices=("A", "B"), required=True)

    def payout(q):
        q.add_argument("--payout-address", required=True)
        q.add_argument("--payout-chain-id", type=int, default=1)

    command("keygen", command_keygen, "create a tournament identity key (never a wallet key)", signed=False)
    q = command("register", command_register, "sign registration against the published charter")
    context(q)
    q.add_argument("--charter-hash", required=True)
    q.add_argument("--account-id", required=True, help="venue account ID required by the charter")
    payout(q)
    q = command("commit", command_commit, "commit wildcard faction and save private reveal material")
    context(q, match=True)
    q.add_argument("--faction", choices=("orc", "necro", "fey"), required=True)
    q.add_argument("--secret", type=Path, help="private file, mode600; existing matching choice is reused")
    q = command("reveal", command_reveal, "reveal wildcard choice after both commitments are published")
    q.add_argument("--secret", type=Path, required=True)
    q = command("ready", command_ready, "sign attendance after your valid faction commitment")
    context(q, match=True)
    q = command("batch", command_batch, "verify a transcript prefix and sign its complete next batch")
    q.add_argument("--transcript", type=Path, required=True)
    q.add_argument("--actions", type=Path, help="own agent's actions keyed by game ID; overrides strategy")
    q.add_argument("--strategy", choices=("first-legal", "search"), default="first-legal",
                   help="first-legal is a smoke player only; choose search or supply your own actions")
    q.add_argument("--budget", type=int, default=1000, help="search nodes per game, default 1000")
    q.add_argument("--depth", type=int, choices=range(1, 21), default=6)
    q = command("claim", command_claim, "sign a payout request using final verifier result hashes")
    context(q)
    q.add_argument("--bracket-hash", required=True)
    q.add_argument("--result-hash", required=True)
    payout(q)
    for name in ("timeout", "resign"):
        q = command(name, command_terminal, "sign a current-head " + name + " record")
        q.add_argument("--transcript", type=Path, required=True)
    q = sub.add_parser("sync", help="fetch authoritative venue comments and reconstruct the canonical gameplay prefix")
    q.set_defaults(function=command_sync)
    q.add_argument("--descriptor", type=Path, required=True)
    q.add_argument("--post-id", required=True)
    q.add_argument("--output", type=Path, help="atomically replace local public transcript file")
    q = sub.add_parser("descriptor", help="derive a first-round descriptor from signed organizer records")
    q.set_defaults(function=command_descriptor)
    q.add_argument("--charter", type=Path, required=True)
    q.add_argument("--roster", type=Path, required=True)
    q.add_argument("--bracket", type=Path, required=True)
    q.add_argument("--organizer-key", required=True)
    q.add_argument("--match-id", required=True)
    source = q.add_mutually_exclusive_group(required=True)
    source.add_argument("--post-id", help="fetch accepted registration envelopes from official thread")
    source.add_argument("--registrations", type=Path, help="retained JSON list of accepted signed registration envelopes")
    q.add_argument("--output", type=Path)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        args.function(args)
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        print(f"HXO error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
