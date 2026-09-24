import unittest
from pathlib import Path

from memo_ingest.gate import Observation, consider, is_audio_candidate


class GateTest(unittest.TestCase):
    def test_rejects_junk_names(self):
        junk = [
            "Cloud.db",
            "Cloud.db-shm",
            "Cloud.db-wal",
            "note.waveform",
            "memo.m4a.icloud",
            ".hidden.m4a",
            "ABC_SUPPORT_1.m4a",
            "folder",
        ]
        for name in junk:
            self.assertFalse(is_audio_candidate(Path(name)), name)
        self.assertTrue(is_audio_candidate(Path("20260923 155900.m4a")))

    def test_first_sight_is_never_ready(self):
        folder = Path(self._tmp())
        path = folder / "20260923 155900.m4a"
        path.write_bytes(b"x" * 20000)
        decision = consider(path, None, 1_000, min_bytes=10240, stable_seconds=12)
        self.assertEqual(decision.state, "wait")
        self.assertIn("first sight", decision.reason)

    def test_small_file_stays_waiting(self):
        folder = Path(self._tmp())
        path = folder / "short.m4a"
        path.write_bytes(b"x" * 100)
        first = consider(path, None, 1_000, min_bytes=10240, stable_seconds=12)
        second = consider(path, first.observation, 9_000, min_bytes=10240, stable_seconds=12)
        self.assertEqual(second.state, "wait")
        self.assertIn("below minimum", second.reason)

    def test_growth_resets_the_clock(self):
        folder = Path(self._tmp())
        path = folder / "growing.m4a"
        path.write_bytes(b"x" * 12000)
        first = consider(path, None, 1_000, min_bytes=10240, stable_seconds=12)
        path.write_bytes(b"y" * 24000)
        grown = consider(path, first.observation, 1_030, min_bytes=10240, stable_seconds=12)
        self.assertEqual(grown.state, "wait")
        self.assertIn("changed", grown.reason)
        too_soon = consider(path, grown.observation, 1_035, min_bytes=10240, stable_seconds=12)
        self.assertEqual(too_soon.state, "wait")
        ready = consider(path, grown.observation, 1_042, min_bytes=10240, stable_seconds=12)
        self.assertEqual(ready.state, "ready")

    def test_stable_file_becomes_ready_on_second_sight(self):
        folder = Path(self._tmp())
        path = folder / "stable.m4a"
        path.write_bytes(b"z" * 20000)
        first = consider(path, None, 500, min_bytes=10240, stable_seconds=12)
        ready = consider(path, first.observation, 520, min_bytes=10240, stable_seconds=12)
        self.assertEqual(ready.state, "ready")
        self.assertIsInstance(first.observation, Observation)

    def _tmp(self) -> str:
        import tempfile

        return tempfile.mkdtemp()


if __name__ == "__main__":
    unittest.main()
