#!/usr/bin/env python3
"""Challenger client for the Hex War bounty. Your bot runs here, on your machine.

    python bounty_client.py keygen
    python bounty_client.py play --server URL --handle GITHUB_HANDLE --payout 0x... \\
        [--bot search --depth 8 --budget 300000 | --bot-cmd "python my_bot.py"]
    python bounty_client.py verify --record match.json --house-key HEX

--bot-cmd starts your program once and talks to it in JSON lines. Each turn it
receives {"type":"turn","seq":N,"games":{"g1":{"state":S,"legal":[...]},...}}
and must answer {"actions":{"g1":ACTION,...}} with one legal action per listed
game. Only the signed moves leave your machine; your code never does.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

import bounty
from engine import Engine
import signing

DEFAULT_KEY = Path(".hxo-private/bounty.key")


POLL_SECONDS = 2          # while the house thinks; this wait is not charged to you
HOUSE_WAIT_LIMIT = 1800   # one house turn taking longer than this means something broke


class Transient(Exception):
    """A proxy hiccup or dropped connection: safe to re-read the match and carry on."""


def http(method, url, body=None, timeout=120):
    data = None if body is None else signing.canonical(body)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "hexwar-bounty-client/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
            raise Transient(f"HTTP {exc.code}") from exc
        raise SystemExit(f"server said {exc.code}: {exc.read().decode(errors='replace')[:300]}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise Transient(str(exc)) from exc


def get_retrying(url, tries=30):
    for i in range(tries):
        try:
            return http("GET", url)
        except Transient as exc:
            print(f"  (retrying after {exc})", file=sys.stderr)
            time.sleep(min(30, 2 * (i + 1)))
    raise SystemExit(f"server unreachable: {url}")


def wait_for_turn(server, match_id, state):
    """The house thinks in the background; poll until it is A's turn or the match ends."""
    started = time.monotonic()
    while state["status"] == "active" and state.get("next_actor") != "A":
        if time.monotonic() - started > HOUSE_WAIT_LIMIT:
            raise SystemExit(f"the house has not replied in {HOUSE_WAIT_LIMIT}s; match {match_id} "
                             "is probably stuck on my side. Please report it; it won't count.")
        time.sleep(POLL_SECONDS)
        state = get_retrying(f"{server}/v1/matches/{match_id}")
    return state


class SearchBot:
    def __init__(self, engine, depth, budget, quiescence=True):
        self.engine, self.depth, self.budget, self.quiescence = engine, depth, budget, quiescence
        self.seed = secrets.randbits(52)

    def __call__(self, games, seq):
        return {gid: self.engine.ask({"op": "search", "state": g["state"], "policy": "tactical",
                                      "depth": self.depth, "node_budget": self.budget,
                                      "seed": self.seed, "quiescence": self.quiescence})["action"]
                for gid, g in games.items()}


class CommandBot:
    def __init__(self, command):
        self.proc = subprocess.Popen(shlex.split(command), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, bufsize=1)

    def __call__(self, games, seq):
        self.proc.stdin.write(json.dumps({"type": "turn", "seq": seq, "games": games}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise SystemExit("your bot exited without answering")
        return json.loads(line)["actions"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def play(server, key, handle, payout, bot_factory, record_path, house_key=None, recheck=True,
         house=bounty.HOUSE):
    info = get_retrying(server + "/v1/bounty")
    terms = info["terms"]
    if house_key and terms["house_key"] != house_key:
        raise SystemExit("server's house key differs from the one you pinned")
    if terms != bounty.terms(terms["house_key"], house):
        raise SystemExit("server terms differ from this kit; update the kit or check the server")
    request = signing.sign(bounty.request_payload(signing.digest(terms), handle, payout), key)
    try:
        acceptance = http("POST", server + "/v1/matches", request)["acceptance"]
    except Transient as exc:
        raise SystemExit(f"opening the match failed ({exc}); if it went through, the server "
                         "will refuse a second one until it ends")
    match_id = acceptance["payload"]["match_id"]
    print(f"match {match_id} accepted; you are A on {len(bounty.GAMES)} boards", file=sys.stderr)
    with Engine(timeout=600) as engine:
        m = bounty.Match(request, acceptance, engine, terms["house_key"], house)
        bot = bot_factory(engine)
        state = {"status": "active"}
        while state["status"] == "active":
            games = {gid: {"state": m.series.boards[gid]["state"],
                           "legal": engine.legal(m.series.boards[gid]["state"])["actions"]}
                     for gid in m.series.required_games()}
            batch = signing.sign(m.batch_payload(bot(games, len(m.batches))), key)
            sent = len(m.batches)
            for attempt in range(5):
                try:
                    state = http("POST", f"{server}/v1/matches/{match_id}/batches", batch)
                    break
                except Transient as exc:
                    # Did it land? Re-read before resending; a resend of a landed batch is refused.
                    print(f"  (submit hiccup: {exc}; checking)", file=sys.stderr)
                    time.sleep(3)
                    state = get_retrying(f"{server}/v1/matches/{match_id}")
                    if len(state["batches"]) > sent or state["status"] != "active":
                        break
            else:
                raise SystemExit("could not submit the batch after 5 attempts")
            state = wait_for_turn(server, match_id, state)
            # Replay everything new, checking the house's signatures and legality.
            for envelope, at in zip(state["batches"][len(m.batches):],
                                    state["timeline"][len(m.timeline):]):
                m.apply(envelope, at)
            done = len(m.batches)
            if done % 10 < 2:
                print(f"  batch {done}: status {state['status']}", file=sys.stderr)
        getattr(bot, "close", lambda: None)()
    record = {"request": request, "acceptance": acceptance,
              "batches": state["batches"], "result": state["result"]}
    Path(record_path).write_text(json.dumps(record) + "\n")
    result = bounty.verify_record(record, terms["house_key"], house=house, check_house_moves=recheck)
    return result


def summary(r):
    return {k: r[k] for k in ("match_id", "status", "challenger_wins", "draws", "house_wins",
                              "win_threshold", "cell_wins", "min_wins_per_cell", "passed")}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--key", type=Path, default=DEFAULT_KEY)
    q = sub.add_parser("play")
    q.add_argument("--server", required=True)
    q.add_argument("--key", type=Path, default=DEFAULT_KEY)
    q.add_argument("--handle", required=True, help="your GitHub handle (proven at claim time)")
    q.add_argument("--payout", required=True, help="Ethereum mainnet receiving address")
    q.add_argument("--house-key", help="pin the published house key")
    q.add_argument("--bot", choices=("search",), default="search")
    q.add_argument("--bot-cmd", help="your own bot program speaking JSON lines")
    q.add_argument("--depth", type=int, default=8)
    q.add_argument("--budget", type=int, default=300000)
    q.add_argument("--no-quiescence", action="store_true", help="built-in search without quiescence")
    q.add_argument("--record", type=Path, default=Path("bounty-record.json"))
    q.add_argument("--no-recheck", action="store_true", help="skip recomputing every house move")
    q.add_argument("--expect-house", type=json.loads, default=bounty.HOUSE, help=argparse.SUPPRESS)
    v = sub.add_parser("verify")
    v.add_argument("--record", type=Path, required=True)
    v.add_argument("--house-key", required=True)
    v.add_argument("--no-recheck", action="store_true")
    v.add_argument("--expect-house", type=json.loads, default=bounty.HOUSE, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.command == "keygen":
        args.key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        print(json.dumps({"public_key": signing.generate_key(args.key), "key_file": str(args.key)}))
        return 0
    if args.command == "verify":
        record = json.loads(args.record.read_text())
        result = bounty.verify_record(record, args.house_key, house=args.expect_house,
                                      check_house_moves=not args.no_recheck)
        print(json.dumps({"verified": True, **summary(result)}, indent=1))
        return 0
    key = signing.load_key(args.key)
    if args.bot_cmd:
        factory = lambda engine: CommandBot(args.bot_cmd)
    else:
        factory = lambda engine: SearchBot(engine, args.depth, args.budget, not args.no_quiescence)
    result = play(args.server.rstrip("/"), key, args.handle, args.payout, factory, args.record,
                  args.house_key, recheck=not args.no_recheck, house=args.expect_house)
    print(json.dumps({"verified": True, "record": str(args.record), **summary(result)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
