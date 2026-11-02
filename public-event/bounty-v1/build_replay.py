#!/usr/bin/env python3
"""Turn a signed bounty record into a replay file and a per-game table.

Every position comes from the pinned engine replaying the signed batches through
bounty.Match, so nothing here is taken on trust from the record's summary. The
script refuses to write anything if its tally disagrees with the signed result.

    python public-event/bounty-v1/build_replay.py <record.json> <house_key_hex> <out_dir>
"""
import json, sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "bounty.py").exists())
sys.path.insert(0, str(ROOT))
import bounty
from engine import Engine

KINDS = {}


def pack_state(state):
    """[turn, ply, reserves, rows]; rows are [q, r, kind_index, side, facing]."""
    rows = []
    for p in state["board"]:
        k = KINDS.setdefault(p["kind"], len(KINDS))
        rows.append([p["q"], p["r"], k, p["side"], p["facing"]])
    return [state["turn"], state["ply"], state["reserves"], rows]


def dist(q, r):
    return (abs(q) + abs(r) + abs(q + r)) // 2


def tiebreak(state, side):
    mine = [p for p in state["board"] if p["side"] == side]
    royal = next((p for p in mine if p["kind"] in ("king", "crowned_lancer")), None)
    d = dist(royal["q"], royal["r"]) if royal else 5
    total = sum(dist(p["q"], p["r"]) for p in mine)
    return {"royal_distance": d, "pieces": len(mine), "total_distance": total}


def main():
    record = json.loads(Path(sys.argv[1]).read_text())
    house_key, out = sys.argv[2], Path(sys.argv[3])
    keep_keys = None
    with Engine(timeout=60) as engine:
        m = bounty.Match(record["request"], record["acceptance"], engine, house_key)
        boards = m.series.boards
        starts = {g: json.loads(json.dumps(b["state"])) for g, b in boards.items()}
        lines = {g: [] for g in boards}
        states = {g: [starts[g]] for g in boards}
        outcomes = {g: [b["outcome"]] for g, b in boards.items()}
        result = record["result"]["payload"]
        for envelope, at in zip(record["batches"], result["timeline"]):
            actions = envelope["payload"]["actions"]
            m.apply(envelope, at)
            for g, a in actions.items():
                lines[g].append(a)
                states[g].append(json.loads(json.dumps(boards[g]["state"])))
                outcomes[g].append(boards[g]["outcome"])
        score = m.score()
    for key in ("challenger_wins", "house_wins", "draws", "cell_wins", "passed", "winners"):
        if score[key] != result[key]:
            raise SystemExit(f"replay disagrees with the signed result on {key}")

    games = []
    for i, (gid, b) in enumerate(boards.items()):
        spec = bounty.GAMES[i]
        o = b["outcome"]
        challenger_side = b["owners"].index("A")
        final = b["state"]
        side_names = {challenger_side: "challenger", 1 - challenger_side: "house"}
        g = {"id": gid, "number": i + 1, "cell": bounty.cell(spec),
             "factions": b["factions"], "challenger_side": challenger_side,
             "challenger_faction": b["factions"][challenger_side],
             "house_faction": b["factions"][1 - challenger_side],
             "winner": side_names[o["winner"]] if o["status"] == "win" else None,
             "reason": o.get("reason"), "actions": len(lines[gid]),
             "tiebreak": {side_names[s]: tiebreak(final, s) for s in (0, 1)},
             "states": [pack_state(s) for s in states[gid]], "line": lines[gid],
             "outcome": o}
        games.append(g)
    wins = sum(g["winner"] == "challenger" for g in games)
    if wins != result["challenger_wins"]:
        raise SystemExit("per-game tally disagrees with the signed result")
    doc = {"match_id": result["match_id"], "handle": result["github_handle"],
           "challenger_wins": result["challenger_wins"], "house_wins": result["house_wins"],
           "draws": result["draws"], "passed": result["passed"], "kinds": list(KINDS),
           "finished_at": result["finished_at"], "games": games}
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{result['match_id']}.replay.json").write_text(json.dumps(doc, separators=(",", ":")))
    print(json.dumps([{k: g[k] for k in ("id", "cell", "winner", "reason", "actions", "tiebreak")} for g in games]))


if __name__ == "__main__":
    main()
