import os
import unittest
from pathlib import Path
from tempfile import mkdtemp

from memo_ingest.discover import (
    RecordingsProbe,
    choose_recordings,
    choose_vault,
    directory_error,
)
from memo_ingest.errors import IngestError


class DiscoverTest(unittest.TestCase):
    def test_prefers_folder_that_has_audio(self):
        empty = RecordingsProbe(Path("/empty"), True, True, 0, None, None)
        older = RecordingsProbe(Path("/old"), True, True, 2, 10, None)
        newer = RecordingsProbe(Path("/new"), True, True, 1, 50, None)
        missing = RecordingsProbe(Path("/nope"), False, False, 0, None, "not found")
        chosen = choose_recordings([missing, empty, older, newer])
        self.assertEqual(chosen, newer)

    def test_falls_back_to_existing_empty_folder(self):
        unreadable = RecordingsProbe(Path("/blocked"), True, False, 0, None, "Operation not permitted")
        missing = RecordingsProbe(Path("/nope"), False, False, 0, None, "not found")
        chosen = choose_recordings([unreadable, missing])
        self.assertEqual(chosen, unreadable)
        self.assertIsNone(choose_recordings([missing]))

    def test_vault_choice(self):
        root = Path(mkdtemp())
        personal = root / "brain-personal"
        work = root / "brain-work"
        for folder in (personal, work):
            (folder / ".obsidian").mkdir(parents=True)
        self.assertEqual(choose_vault([work, personal], None), personal)
        self.assertEqual(choose_vault([work], None), work)
        explicit = work
        self.assertEqual(choose_vault([work, personal], explicit), work)
        with self.assertRaises(IngestError):
            choose_vault([work, personal], root / "missing")
        with self.assertRaises(IngestError):
            choose_vault([], None)

    def test_unreadable_directory(self):
        folder = Path(mkdtemp())
        os.chmod(folder, 0)
        try:
            problem = directory_error(folder)
        finally:
            os.chmod(folder, 0o755)
        self.assertIsNotNone(problem)
        self.assertIn("not permitted", problem or "")


if __name__ == "__main__":
    unittest.main()
