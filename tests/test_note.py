import unittest
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp

from memo_ingest.audio import datetime_from_filename, sha256_file
from memo_ingest.config import load_config, write_config
from memo_ingest.errors import IngestError
from memo_ingest.note import (
    parse_frontmatter,
    render_note,
    splice_summary,
    transcript_section,
    write_note,
)
from tests.support import make_config


class NoteTest(unittest.TestCase):
    def test_filename_timestamp_is_local_wall_clock(self):
        parsed = datetime_from_filename("20260923 155900-ABCDEF.m4a")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.month, 9)
        self.assertEqual(parsed.day, 23)
        self.assertEqual(parsed.hour, 15)
        self.assertEqual(parsed.minute, 59)
        self.assertEqual(parsed.second, 0)
        self.assertIn("2026-09-23T15:59:00", parsed.isoformat(timespec="seconds"))

    def test_frontmatter_roundtrip(self):
        recorded = datetime_from_filename("20260923 155900.m4a")
        assert recorded is not None
        created = recorded
        text = render_note(
            created=created,
            recorded=recorded,
            audio_field="[[attachments/audio/2026/2026-09-23-1559.m4a]]",
            duration_sec=1842,
            size=12345678,
            sha256="ab" * 32,
            model="mlx-whisper-large-v3-turbo",
            language="en",
            runtime_sec=3.2,
            title=None,
            transcript_body="[00:00] Hello there",
        )
        folder = Path(mkdtemp())
        path = folder / "note.md"
        write_note(path, text)
        frontmatter = parse_frontmatter(path.read_text(encoding="utf-8"))
        self.assertEqual(frontmatter["type"], "transcript")
        self.assertEqual(frontmatter["source"], "voice-memos")
        self.assertEqual(frontmatter["status"], "raw")
        self.assertEqual(frontmatter["speakers"], "false")
        self.assertEqual(frontmatter["model"], "mlx-whisper-large-v3-turbo")
        self.assertEqual(frontmatter["language"], "en")
        self.assertEqual(frontmatter["duration_sec"], "1842")
        self.assertEqual(frontmatter["bytes"], "12345678")
        self.assertEqual(frontmatter["audio_sha256"], "ab" * 32)
        self.assertEqual(
            frontmatter["audio"],
            "[[attachments/audio/2026/2026-09-23-1559.m4a]]",
        )
        self.assertIn("# Voice memo 2026-09-23 15:59", text)
        self.assertIn("## Transcript", text)
        self.assertIn("## Summary", text)
        self.assertIn("## Action items", text)
        self.assertIn("- [ ]", text)
        self.assertIn("filled by optional second pass", text)

    def test_summary_splice_does_not_touch_transcript(self):
        recorded = datetime_from_filename("20260923 155900.m4a")
        assert recorded is not None
        original = render_note(
            created=recorded,
            recorded=recorded,
            audio_field="[[attachments/audio/2026/2026-09-23-1559.m4a]]",
            duration_sec=4,
            size=100,
            sha256="cd" * 32,
            model="mlx-whisper-test",
            language="en",
            runtime_sec=0.1,
            title=None,
            transcript_body="[00:00] Keep this exact transcript.",
        )
        updated = splice_summary(
            original,
            """## Summary
- One bullet.

## Action items
- [ ] Do the thing
""",
        )
        self.assertEqual(transcript_section(original), transcript_section(updated))
        self.assertIn("status: processed", updated)
        self.assertNotIn("status: raw", updated)
        self.assertIn("- One bullet.", updated)
        self.assertIn("- [ ] Do the thing", updated)
        self.assertNotIn("Keep this exact transcript.", updated.split("## Summary")[1])

    def test_bad_summary_leaves_the_parser_raising(self):
        with self.assertRaises(IngestError):
            splice_summary("nope", "hello")

    def test_config_roundtrip(self):
        tmp, cfg = make_config()
        path = tmp / "config.toml"
        write_config(path, cfg)
        loaded = load_config(path)
        self.assertEqual(loaded.recordings_dir, cfg.recordings_dir)
        self.assertEqual(loaded.vault_root, cfg.vault_root)
        self.assertEqual(loaded.inbox_dir, cfg.inbox_dir)
        self.assertEqual(loaded.model, cfg.model)
        self.assertEqual(loaded.stable_seconds, 12)
        self.assertEqual(loaded.min_bytes, 10240)
        self.assertTrue(loaded.copy_audio)
        self.assertFalse(loaded.diarize)

    def test_sha256_matches_bytes(self):
        folder = Path(mkdtemp())
        path = folder / "a.m4a"
        path.write_bytes(b"abc" * 100)
        self.assertEqual(len(sha256_file(path)), 64)


if __name__ == "__main__":
    unittest.main()
