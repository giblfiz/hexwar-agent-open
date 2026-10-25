"""Pinned Hex War rules and seven-game atomic batches. No network or signing.

The shipped executable is Linux x86-64. A different platform's build must be
explicitly admitted by a later charter rather than silently replacing this one.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import selectors
import subprocess


BUNDLE = Path(__file__).resolve().parent / "bundle"
ENGINE_SHA256 = "2f98bda6690f0a3902d0f8e23f4d8a8ed27e5a693e97087a35db250098a58da2"
SNAPSHOT_SHA256 = "c4276d8bd04f9b64acc0b6f8a28c9b1697166b3237394390b5473e7858b7a366"
INITIATIVE = {"orc": 0, "fey": 1, "necro": 2}


class EngineError(ValueError):
    """The pinned engine rejected a request or failed its transport contract."""


class BatchError(ValueError):
    """A submitted batch is not an entire valid next turn."""


def checked_bytes(path: Path, expected: str) -> bytes:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise EngineError(f"pinned artifact hash mismatch: {path.name}")
    return data


class Engine:
    def __init__(self, bundle: Path | str = BUNDLE):
        bundle = Path(bundle)
        binary = bundle / "hexwar-engine"
        checked_bytes(binary, ENGINE_SHA256)
        self.snapshot = json.loads(checked_bytes(bundle / "game.json", SNAPSHOT_SHA256))
        self.process = subprocess.Popen(
            [str(binary.resolve())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def ask(self, request: dict) -> dict:
        if self.process.poll() is not None:
            raise EngineError("engine is not running")
        self.process.stdin.write(json.dumps(request, allow_nan=False) + "\n")
        self.process.stdin.flush()
        if not self.selector.select(timeout=10):
            self.close()
            raise EngineError("engine response exceeded 10 seconds")
        line = self.process.stdout.readline()
        if not line:
            raise EngineError("engine closed its output")
        response = json.loads(line)
        if response.get("ok") is not True:
            raise EngineError(response.get("error", "invalid engine response"))
        return response

    def legal(self, state: dict) -> dict:
        return self.ask({"op": "legal", "state": state})

    def apply(self, state: dict, action: dict) -> dict:
        return self.ask({"op": "apply", "state": state, "action": action})

    def close(self) -> None:
        self.selector.close()
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream and not stream.closed:
                stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Series:
    """Seven boards with entrant labels A/B and complete alternating batches.

    `wildcard` lists the faction chosen by A then B. `mirror_first` selects the
    entrant moving first only when their wildcard factions match. All cross-
    faction games use the frozen initiative order. A posts the opening batch.
    """

    def __init__(self, engine: Engine, wildcard=("orc", "orc"), mirror_first="A"):
        if (not isinstance(wildcard, (tuple, list)) or len(wildcard) != 2
                or any(f not in INITIATIVE for f in wildcard)):
            raise BatchError("wildcard must contain A's and B's valid factions")
        if mirror_first not in ("A", "B"):
            raise BatchError("mirror_first must be A or B")
        self.engine = engine
        self.wildcard = tuple(wildcard)
        self.mirror_first = mirror_first
        self.boards = {}
        self.next_actor = "A"
        self.batch_index = 0
        pairs = [("orc", "necro"), ("necro", "orc"),
                 ("orc", "fey"), ("fey", "orc"),
                 ("necro", "fey"), ("fey", "necro"), tuple(wildcard)]
        for i, (fa, fb) in enumerate(pairs, 1):
            a_first = mirror_first == "A" if fa == fb else INITIATIVE[fa] < INITIATIVE[fb]
            owners = ["A", "B"] if a_first else ["B", "A"]
            factions = [fa, fb] if a_first else [fb, fa]
            state = copy.deepcopy(engine.snapshot["setups"]["-".join(factions)])
            self.boards[f"g{i}"] = {
                "owners": owners, "factions": factions, "state": state,
                "outcome": engine.legal(state)["outcome"],
            }

    @property
    def finished(self) -> bool:
        return all(b["outcome"]["status"] != "ongoing" for b in self.boards.values())

    def required_games(self) -> list[str]:
        return [gid for gid, b in self.boards.items()
                if b["outcome"]["status"] == "ongoing"
                and b["owners"][b["state"]["turn"]] == self.next_actor]

    def legal_batch(self) -> dict:
        """First-legal smoke player, deliberately not a competitive entrant."""
        return {gid: self.engine.legal(self.boards[gid]["state"])["actions"][0]
                for gid in self.required_games()}

    def apply_batch(self, actor: str, actions_by_game: dict) -> dict:
        if self.finished:
            raise BatchError("series is already complete")
        if actor != self.next_actor:
            raise BatchError("wrong batch actor")
        required = self.required_games()
        if not isinstance(actions_by_game, dict) or set(actions_by_game) != set(required):
            raise BatchError(f"batch must contain exactly these games: {','.join(required)}")
        if not required:
            raise BatchError("empty batches are not permitted")
        # Stage ALL transitions first. A failure in the last game must not commit
        # earlier valid moves, flip the actor, or advance the batch sequence.
        staged = {}
        for gid in required:
            try:
                staged[gid] = self.engine.apply(self.boards[gid]["state"], actions_by_game[gid])
            except EngineError as exc:
                raise BatchError(f"{gid}: {exc}") from exc
        record = {"batch_index": self.batch_index, "actor": actor,
                  "actions": copy.deepcopy(actions_by_game)}
        for gid, transition in staged.items():
            self.boards[gid]["state"] = transition["state"]
            self.boards[gid]["outcome"] = transition["outcome"]
        self.batch_index += 1
        self.next_actor = "B" if actor == "A" else "A"
        return record

    def result(self) -> dict:
        """Score in half-point units; a tied series deliberately has no winner."""
        if not self.finished:
            raise BatchError("cannot score an unfinished series")
        score2 = {"A": 0, "B": 0}
        games = {}
        for gid, b in self.boards.items():
            o = b["outcome"]
            winner = b["owners"][o["winner"]] if o["status"] == "win" else None
            if winner is None:
                score2["A"] += 1
                score2["B"] += 1
            else:
                score2[winner] += 2
            games[gid] = {"winner": winner, "outcome": copy.deepcopy(o),
                          "plies": b["state"]["ply"]}
        winner = None if score2["A"] == score2["B"] else max(score2, key=score2.get)
        return {"score2": score2, "winner": winner, "games": games,
                "batches": self.batch_index}
