#!/usr/bin/env python3
"""House-side bounty server. Plays the frozen house bot; never runs submitted code.

Challengers send only signed JSON (a match request, then gameplay batches). Every
submitted action is checked by the pinned engine before it is applied. Nothing
here touches a wallet: a passing match only writes a claim flag for the operator.

    python bounty_server.py serve  --state DIR [--host 127.0.0.1] [--port 8495]
    python bounty_server.py status --state DIR
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading

import bounty
from bounty import BountyError, Match, micros, utc
from engine import Engine, BatchError
import signing

DEADLINE = "2026-12-31T23:59:59Z"
MAX_BODY = 256 * 1024
MATCH_ID = re.compile(r"^m[0-9]{8}T[0-9]{6}-[0-9a-f]{8}$")


class Refused(BountyError):
    """A well-formed request the house declines (rate limit, closed, wrong turn)."""


def write_json(path: Path, value, mode=0o640):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, indent=1, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


class Bounty:
    def __init__(self, state_dir, *, house=bounty.HOUSE, clock=utc, max_active=3,
                 daily_per_handle=3, deadline=DEADLINE, engine_timeout=120, background=False):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        key_path = self.root / "house.key"
        if not key_path.exists():
            signing.generate_key(key_path)
        self.key = signing.load_key(key_path)
        self.house_key = signing.public_hex(self.key)
        self.house, self.clock, self.deadline = dict(house), clock, deadline
        self.max_active, self.daily_per_handle = max_active, daily_per_handle
        self.engine_timeout = engine_timeout
        # background=True (the public server): the house thinks in a thread and
        # POST returns at once, because a proxy (Cloudflare: 100 s) cuts long
        # requests and the house can take minutes per batch. Clients poll GET.
        self.background, self.threads = background, {}
        self.terms = bounty.terms(self.house_key, self.house)
        self.terms_hash = signing.digest(self.terms)
        self.lock = threading.Lock()
        self.live = {}           # match_id -> (Match, Engine, threading.Lock)
        for path in sorted((self.root / "matches").glob("*.json")):
            state = json.loads(path.read_text())
            if state["status"] == "active":
                m = self._resume(state)
                if self.background and not m.series.finished and m.next_actor == "B":
                    self._kick(m.manifest["match_id"])   # interrupted mid house turn

    # ---- storage -------------------------------------------------------
    def _state_path(self, match_id):
        return self.root / "matches" / f"{match_id}.json"

    def _load(self, match_id):
        if not isinstance(match_id, str) or not MATCH_ID.fullmatch(match_id):
            raise KeyError(match_id)
        path = self._state_path(match_id)
        if not path.exists():
            raise KeyError(match_id)
        return json.loads(path.read_text())

    def _resume(self, state):
        engine = Engine(timeout=self.engine_timeout)
        m = Match(state["request"], state["acceptance"], engine, self.house_key, self.house)
        for envelope, at in zip(state["batches"], state["timeline"]):
            m.apply(envelope, at)
        self.live[m.manifest["match_id"]] = (m, engine, threading.Lock())
        return m

    def _save(self, m, secret, status, result=None):
        state = {"request": m.request_envelope, "acceptance": m.acceptance, "batches": m.batches,
                 "timeline": m.timeline, "house_secret": secret, "status": status,
                 "result": result, "next_actor": None if m.series.finished else m.next_actor}
        write_json(self._state_path(m.manifest["match_id"]), state, mode=0o600)

    # ---- public operations ---------------------------------------------
    def status(self):
        claims = sorted(p.name for p in (self.root / "claims").glob("*.json"))
        state = "closed" if self._closed() else "claim_pending" if claims else "open"
        board = []
        for path in sorted((self.root / "records").glob("*.json")):
            r = json.loads(path.read_text())["result"]["payload"]
            board.append({k: r[k] for k in ("match_id", "github_handle", "status",
                                            "challenger_wins", "draws", "house_wins", "passed")}
                         | {"cells_won": sum(1 for v in r["cell_wins"].values() if v)})
        return {"status": state, "deadline": self.deadline, "terms": self.terms,
                "terms_hash": self.terms_hash, "active_matches": len(self.live),
                "claims": len(claims), "finished": board}

    def _closed(self):
        return (self.root / "STOP").exists() or micros(self.clock()) > micros(self.deadline)

    def open_match(self, request_envelope):
        now = self.clock()
        request = bounty.check_request(request_envelope, self.house_key, self.house)
        with self.lock:
            self.expire_all()
            if self._closed():
                raise Refused("the bounty is closed to new matches")
            handle, key = request["github_handle"].lower(), request_envelope["public_key"]
            if key == self.house_key:
                raise BountyError("challenger key must differ from the house key")
            for m, _, _ in self.live.values():
                if m.request["github_handle"].lower() == handle or m.manifest["players"]["A"] == key:
                    raise Refused("this handle or key already has an active match")
            if len(self.live) >= self.max_active:
                raise Refused("the house is busy; try again later")
            day_ago = micros(now) - 86400 * 1_000_000
            recent = 0
            for path in (self.root / "matches").glob("*.json"):
                st = json.loads(path.read_text())
                if (st["request"]["payload"]["github_handle"].lower() == handle
                        and not st["status"].startswith("void")
                        and micros(st["acceptance"]["payload"]["accepted_at"]) > day_ago):
                    recent += 1
            if recent >= self.daily_per_handle:
                raise Refused(f"limit is {self.daily_per_handle} matches per handle per 24 hours")
            secret = secrets.token_hex(32)
            stamp = now[:19].replace("-", "").replace(":", "")
            match_id = f"m{stamp}-{secrets.token_hex(4)}"
            acceptance = signing.sign(bounty.acceptance_payload(
                request_envelope, self.house_key, secret, match_id, now, self.house), self.key)
            engine = Engine(timeout=self.engine_timeout)
            try:
                m = Match(request_envelope, acceptance, engine, self.house_key, self.house)
            except Exception:
                engine.close()
                raise
            self.live[match_id] = (m, engine, threading.Lock())
            self._save(m, secret, "active")
        return acceptance

    def view(self, match_id):
        self.expire(match_id)
        state = self._load(match_id)
        out = {k: state[k] for k in ("request", "acceptance", "batches", "timeline", "status")}
        out["result"] = state["result"]
        live = self.live.get(match_id)
        if live:
            m = live[0]
            # The saved state, not the live object: it is written only after a
            # house batch is applied, so "A" never appears before that batch does.
            out["next_actor"] = state.get("next_actor") or m.next_actor
            out["clock_remaining_seconds"] = (bounty.CLOCK_SECONDS * 1_000_000
                                              - m.challenger_elapsed_us(self.clock())) // 1_000_000
        return out

    def submit(self, match_id, envelope):
        self._load(match_id)
        self.expire(match_id)
        live = self.live.get(match_id)
        if not live:
            status = self._load(match_id)["status"]
            if status == "timeout":
                raise Refused("challenger clock expired; match recorded as timeout")
            raise Refused(f"match is finished ({status})")
        m, engine, lock = live
        if m.next_actor != "A":           # house thinking: refuse now, don't queue on its lock
            raise Refused("it is not the challenger's turn")
        with lock:
            secret = self._load(match_id)["house_secret"]
            if engine.process.poll() is not None:
                self._void(m, secret, RuntimeError("house engine stopped"))
            if m.next_actor != "A":
                raise Refused("it is not the challenger's turn")
            now = self.clock()
            if m.challenger_elapsed_us(now) > bounty.CLOCK_SECONDS * 1_000_000:
                self._finish(m, secret, "timeout", now)
                raise Refused("challenger clock expired; match recorded as timeout")
            try:
                m.apply(envelope, now)
            except (BatchError, ValueError) as exc:
                raise BountyError(str(exc)) from exc
            if self.background and not m.series.finished:
                self._save(m, secret, "active")
            else:
                self._house_turn(m, secret)   # inline; also finishes a match A just ended
        if self.background:
            self._kick(match_id)
        return self.view(match_id)

    def _house_turn(self, m, secret):
        """The house replies; its thinking time never counts against the
        challenger because the next challenger clock starts at publication."""
        try:
            while not m.series.finished and m.next_actor == "B":
                payload = m.batch_payload(m.house_actions(secret))
                m.apply(signing.sign(payload, self.key), self.clock())
        except Exception as exc:
            self._void(m, secret, exc)
        if m.series.finished:
            self._finish(m, secret, "complete", self.clock())
        else:
            self._save(m, secret, "active")

    def _kick(self, match_id):
        """Run the house's turn: in a thread for the public server, inline otherwise."""
        def run():
            live = self.live.get(match_id)
            if not live:
                return
            m, _, lock = live
            with lock:
                if match_id not in self.live or m.series.finished or m.next_actor != "B":
                    return
                try:
                    self._house_turn(m, self._load(match_id)["house_secret"])
                except Refused:
                    pass          # house failure: _void already recorded the match as void
                except Exception as exc:   # never die silently: a stuck turn must be visible
                    import sys, traceback
                    print(f"house turn crashed in {match_id}: {exc!r}", file=sys.stderr)
                    traceback.print_exc()
        if not self.background:
            return run()
        t = threading.Thread(target=run, name=f"house-{match_id}", daemon=True)
        self.threads[match_id] = t
        t.start()

    def wait_house(self, match_id, timeout=None):
        t = self.threads.get(match_id)
        if t:
            t.join(timeout)

    def _void(self, m, secret, exc):
        """A house-side failure voids the match; it never counts against the
        challenger, who may simply start another."""
        self._save(m, secret, "void_house_error")
        self.live.pop(m.manifest["match_id"])[1].close()
        raise Refused(f"house failure ({type(exc).__name__}); match void, not counted") from exc

    def expire(self, match_id):
        live = self.live.get(match_id)
        if not live:
            return
        m, _, lock = live
        # Never wait here: a held lock means the house is thinking or a batch is
        # being applied, and in both cases the challenger's clock is not running.
        if not lock.acquire(blocking=False):
            return
        try:
            if match_id not in self.live:
                return
            now = self.clock()
            if (not m.series.finished and m.next_actor == "A"
                    and m.challenger_elapsed_us(now) > bounty.CLOCK_SECONDS * 1_000_000):
                self._finish(m, self._load(match_id)["house_secret"], "timeout", now)
        finally:
            lock.release()

    def expire_all(self):
        for match_id in list(self.live):
            self.expire(match_id)

    def _finish(self, m, secret, status, at):
        match_id = m.manifest["match_id"]
        result = signing.sign(m.result_payload(status, secret, at), self.key)
        record = {"request": m.request_envelope, "acceptance": m.acceptance,
                  "batches": m.batches, "result": result}
        write_json(self.root / "records" / f"{match_id}.json", record)
        self._save(m, secret, status, result)
        if result["payload"]["passed"]:
            # Claim order is the house-observed finishing time. Payout is manual:
            # verify, publish, wait >=24h for challenges, confirm the handle owns the key.
            write_json(self.root / "claims" / f"{at}_{match_id}.json", {
                "match_id": match_id, "finished_at": at,
                "github_handle": m.request["github_handle"],
                "payout_address": m.request["payout_address"],
                "challenger_key": m.manifest["players"]["A"],
                "record_hash": signing.digest(record),
                "challenge_window_hours": 24,
                "next_steps": ["re-verify with bounty_client.py verify",
                               "publish record + provisional result",
                               "wait >= 24h from actual publication; resolve challenges",
                               "handle owner posts the challenger key from that GitHub account",
                               "pay only after all of the above; never automatically"]})
        _, engine, _ = self.live.pop(match_id)
        engine.close()

    def close(self):
        for _, engine, _ in self.live.values():
            engine.close()
        self.live.clear()


def handler_for(service: Bounty):
    class Handler(BaseHTTPRequestHandler):
        server_version = "hexwar-bounty/1"

        def _send(self, code, value):
            data = json.dumps(value, sort_keys=True).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                raise BountyError("body must be 1..256 KiB of JSON")
            return signing.parse(self.rfile.read(n).decode("ascii"))

        def _route(self, method):
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            try:
                if method == "GET" and parts == ["v1", "bounty"]:
                    return self._send(200, service.status())
                if method == "POST" and parts == ["v1", "matches"]:
                    return self._send(201, {"acceptance": service.open_match(self._body())})
                if len(parts) == 3 and parts[:2] == ["v1", "matches"] and method == "GET":
                    return self._send(200, service.view(parts[2]))
                if len(parts) == 4 and parts[:2] == ["v1", "matches"] and parts[3] == "batches" \
                        and method == "POST":
                    return self._send(200, service.submit(parts[2], self._body()))
                return self._send(404, {"error": "not found"})
            except KeyError:
                return self._send(404, {"error": "unknown match"})
            except Refused as exc:
                return self._send(409, {"error": str(exc)})
            except (ValueError, UnicodeDecodeError) as exc:
                return self._send(400, {"error": str(exc)[:300]})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def log_message(self, fmt, *args):
            with (service.root / "access.log").open("a") as f:
                f.write(f"{utc()} {self.address_string()} {fmt % args}\n")

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--state", type=Path, required=True)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8495)
    s.add_argument("--house", type=json.loads, default=bounty.HOUSE,
                   help=argparse.SUPPRESS)  # dry runs only; the public house is bounty.HOUSE
    t = sub.add_parser("status")
    t.add_argument("--state", type=Path, required=True)
    args = p.parse_args(argv)
    service = Bounty(args.state, house=getattr(args, "house", bounty.HOUSE),
                     background=args.command == "serve")
    if args.command == "status":
        print(json.dumps(service.status() | {"terms": "(omitted)"}, indent=1))
        service.close()
        return 0
    server = ThreadingHTTPServer((args.host, args.port), handler_for(service))
    print(json.dumps({"listening": f"http://{args.host}:{server.server_address[1]}",
                      "house_key": service.house_key, "terms_hash": service.terms_hash}), flush=True)
    try:
        server.serve_forever()
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
