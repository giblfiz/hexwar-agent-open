#!/usr/bin/env python3
"""Play and independently replay seven games using the noncompetitive demo bot."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Engine, Series, ENGINE_SHA256, SNAPSHOT_SHA256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write full action transcript JSON")
    parser.add_argument("--wildcard-a", choices=("orc", "necro", "fey"), default="orc")
    parser.add_argument("--wildcard-b", choices=("orc", "necro", "fey"), default="orc")
    parser.add_argument("--mirror-first", choices=("A", "B"), default="A")
    args = parser.parse_args()
    config = {"wildcard": [args.wildcard_a, args.wildcard_b], "mirror_first": args.mirror_first}
    with Engine() as engine:
        series = Series(engine, **config)
        batches = []
        while not series.finished:
            batches.append(series.apply_batch(series.next_actor, series.legal_batch()))
        result = series.result()
    # New engine process and fresh initial states; every game state and outcome
    # must match after replay, not just the final winner.
    with Engine() as engine:
        replay = Series(engine, **config)
        for record in batches:
            assert record["batch_index"] == replay.batch_index
            replay.apply_batch(record["actor"], record["actions"])
        assert replay.boards == series.boards
        assert replay.result() == result
    transcript = {"demo_only": True, "engine_sha256": ENGINE_SHA256,
                  "snapshot_sha256": SNAPSHOT_SHA256, **config,
                  "batches": batches, "result": result}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(transcript, indent=2) + "\n")
    print(json.dumps({"replayed_identically": True, "result": result}, indent=2))


if __name__ == "__main__":
    main()
