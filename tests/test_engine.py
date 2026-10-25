import copy
import tempfile
from pathlib import Path
import unittest

from engine import Engine, Series, BatchError, EngineError, checked_bytes


class SeriesTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.addCleanup(self.engine.close)
        self.series = Series(self.engine)

    def test_fixed_pairs_swap_owners_of_identical_positions(self):
        for left, right in (("g1", "g2"), ("g3", "g4"), ("g5", "g6")):
            a, b = self.series.boards[left], self.series.boards[right]
            self.assertEqual(a["state"], b["state"])
            self.assertEqual(a["owners"], b["owners"][::-1])
        self.assertEqual(self.series.boards["g5"]["factions"], ["fey", "necro"])

    def test_wildcard_respects_choice_and_mirror_first(self):
        cross = Series(self.engine, wildcard=("necro", "fey"), mirror_first="A")
        self.assertEqual(cross.boards["g7"]["owners"], ["B", "A"])
        mirror = Series(self.engine, wildcard=("fey", "fey"), mirror_first="B")
        self.assertEqual(mirror.boards["g7"]["owners"], ["B", "A"])
        with self.assertRaises(BatchError):
            Series(self.engine, wildcard=("orc", "invented"))

    def test_batch_failure_is_atomic_even_when_last_action_illegal(self):
        before = copy.deepcopy(self.series.boards)
        actions = self.series.legal_batch()
        actions[list(actions)[-1]] = {"kind": "move", "from": [99, 99], "to": [0, 0], "facing": None}
        with self.assertRaises(BatchError):
            self.series.apply_batch("A", actions)
        self.assertEqual(self.series.boards, before)
        self.assertEqual((self.series.next_actor, self.series.batch_index), ("A", 0))

    def test_wrong_actor_partial_extra_and_empty_batches_rejected(self):
        actions = self.series.legal_batch()
        before = copy.deepcopy(self.series.boards)
        for actor, candidate in (("B", actions), ("A", {}),
                                 ("A", dict(list(actions.items())[:-1])),
                                 ("A", {**actions, "not-a-game": {}})):
            with self.assertRaises(BatchError):
                self.series.apply_batch(actor, candidate)
        self.assertEqual(self.series.boards, before)
        self.assertEqual(self.series.batch_index, 0)

    def test_copycat_cannot_submit_copied_opening_then_wait_for_reply(self):
        opening = self.series.legal_batch()
        copied_orc_opening = copy.deepcopy(opening["g1"])
        self.series.apply_batch("A", opening)
        self.assertEqual(set(self.series.required_games()), {f"g{i}" for i in range(1, 8)})
        # Copying g1's first move into g2 is legal, but B must simultaneously
        # provide its g1 reply before A supplies the g2 reply it would copy.
        with self.assertRaises(BatchError):
            self.series.apply_batch("B", {"g2": copied_orc_opening})
        batch = self.series.legal_batch()
        batch["g2"] = copied_orc_opening
        self.series.apply_batch("B", batch)
        self.assertEqual(self.series.boards["g1"]["state"]["ply"], 2)
        self.assertEqual(self.series.boards["g2"]["state"]["ply"], 1)

    def test_complete_seven_game_series_replays_exactly(self):
        with self.assertRaises(BatchError):
            self.series.result()
        records = []
        while not self.series.finished:
            self.assertTrue(self.series.required_games())
            records.append(self.series.apply_batch(self.series.next_actor, self.series.legal_batch()))
            self.assertLessEqual(len(records), 85)
        replay = Series(self.engine)
        for r in records:
            self.assertEqual(replay.batch_index, r["batch_index"])
            replay.apply_batch(r["actor"], r["actions"])
        self.assertEqual(replay.boards, self.series.boards)
        self.assertEqual(replay.result(), self.series.result())
        self.assertEqual(sum(replay.result()["score2"].values()), 14)
        with self.assertRaises(BatchError):
            replay.apply_batch(replay.next_actor, {})

    def test_hash_pin_rejects_modified_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact"
            path.write_bytes(b"changed")
            with self.assertRaises(EngineError):
                checked_bytes(path, "0" * 64)


if __name__ == "__main__":
    unittest.main()
