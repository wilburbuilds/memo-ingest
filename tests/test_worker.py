import fcntl
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from memo_ingest.audio import sha256_file
from memo_ingest.errors import IngestError
from memo_ingest.note import parse_frontmatter
from memo_ingest.store import Store
from memo_ingest.summarize import summarize_note
from memo_ingest.worker import prepare_reprocess, run_sweep, sweep
from tests.support import FakeTranscriber, make_config


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.cfg = make_config()
        self.store = Store(self.cfg.state_db)
        self.engine = FakeTranscriber()
        self.t = 1_000.0

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp)

    def write_audio(self, name: str, size: int = 20000, payload: bytes | None = None) -> Path:
        path = self.cfg.recordings_dir / name
        data = payload if payload is not None else (b"a" * size)
        path.write_bytes(data)
        return path

    def tick(self, seconds: float = 0, **kwargs):
        self.t += seconds
        return sweep(
            self.cfg,
            self.store,
            transcriber=self.engine,
            now=self.t,
            **kwargs,
        )

    def test_growing_file_is_not_processed_until_stable(self):
        path = self.write_audio("20260923 155900.m4a", 12000)
        first = self.tick()
        self.assertEqual(first.processed, 0)
        self.assertEqual(first.waiting, 1)
        self.assertEqual(list(self.cfg.inbox_dir.glob("*.md")), [])

        path.write_bytes(b"b" * 24000)
        self.tick(30)
        still = self.tick(1)
        self.assertEqual(still.processed, 0)
        self.assertEqual(still.waiting, 1)
        self.assertEqual(self.engine.calls, 0)

        done = self.tick(12)
        self.assertEqual(done.processed, 1)
        self.assertEqual(self.engine.calls, 1)
        notes = list(self.cfg.inbox_dir.glob("*.md"))
        self.assertEqual(len(notes), 1)

    def test_note_created_once_and_second_run_is_a_no_op(self):
        source = self.write_audio("20260923 155900.m4a", 20000)
        self.tick()
        done = self.tick(20)
        self.assertEqual(done.processed, 1)
        note = next(self.cfg.inbox_dir.glob("*.md"))
        text = note.read_text(encoding="utf-8")
        frontmatter = parse_frontmatter(text)
        self.assertEqual(frontmatter["type"], "transcript")
        self.assertEqual(frontmatter["source"], "voice-memos")
        self.assertEqual(frontmatter["status"], "raw")
        self.assertEqual(frontmatter["speakers"], "false")
        self.assertEqual(frontmatter["model"], "mlx-whisper-test")
        self.assertEqual(frontmatter["language"], "en")
        self.assertEqual(frontmatter["audio_sha256"], sha256_file(source))
        self.assertIn("2026-09-23T15:59:00", frontmatter["recorded"])
        self.assertTrue(frontmatter["audio"].startswith("[[attachments/audio/2026/"))
        self.assertIn("Hello from the voice memo.", text)
        self.assertIn("## Summary", text)
        self.assertIn("## Action items", text)
        self.assertTrue(note.name.endswith("-voice-memo.md"))
        self.assertIn("2026-09-23-1559", note.name)

        audio_files = list(self.cfg.audio_dir.rglob("*.m4a"))
        self.assertEqual(len(audio_files), 1)
        self.assertTrue(source.is_file())
        self.assertEqual(audio_files[0].read_bytes(), source.read_bytes())

        again = self.tick(40)
        self.assertEqual(again.processed, 0)
        self.assertEqual(len(list(self.cfg.inbox_dir.glob("*.md"))), 1)
        self.assertEqual(self.engine.calls, 1)

    def test_same_bytes_under_a_new_name_are_skipped(self):
        source = self.write_audio("20260923 155900.m4a", 20000)
        self.tick()
        self.tick(20)
        copy = self.cfg.recordings_dir / "renamed.m4a"
        copy.write_bytes(source.read_bytes())
        self.tick(5)
        skipped = self.tick(20)
        self.assertEqual(skipped.processed, 0)
        self.assertEqual(self.engine.calls, 1)
        self.assertEqual(len(list(self.cfg.inbox_dir.glob("*.md"))), 1)
        self.assertTrue(self.store.is_processed(sha256_file(copy)))

    def test_dry_run_writes_nothing(self):
        self.write_audio("20260923 155900.m4a", 20000)
        self.tick()
        preview = self.tick(20, dry_run=True)
        self.assertEqual(preview.dry_run, 1)
        self.assertEqual(preview.processed, 0)
        self.assertEqual(self.engine.calls, 0)
        self.assertEqual(list(self.cfg.inbox_dir.glob("*.md")), [])
        self.assertEqual(self.store.processed_count(), 0)
        done = self.tick(5)
        self.assertEqual(done.processed, 1)

    def test_note_write_failure_does_not_mark_processed(self):
        source = self.write_audio("20260923 155900.m4a", 20000)
        self.tick()
        with patch("memo_ingest.worker.write_note", side_effect=OSError("disk full")):
            failed = self.tick(20)
        self.assertEqual(failed.failed, 1)
        self.assertEqual(list(self.cfg.inbox_dir.glob("*.md")), [])
        self.assertFalse(self.store.is_processed(sha256_file(source)))
        recovered = self.tick(5)
        self.assertEqual(recovered.processed, 1)
        self.assertEqual(len(list(self.cfg.inbox_dir.glob("*.md"))), 1)

    def test_crash_between_note_and_index_does_not_duplicate(self):
        source = self.write_audio("20260923 155900.m4a", 20000)
        self.tick()
        self.tick(20)
        self.store.conn.execute("DELETE FROM processed")
        self.store.conn.execute("DELETE FROM seen_files")
        self.store.conn.commit()
        self.tick(100)
        adopted = self.tick(20)
        self.assertEqual(adopted.processed, 0)
        self.assertEqual(len(list(self.cfg.inbox_dir.glob("*.md"))), 1)
        self.assertEqual(self.engine.calls, 1)
        self.assertTrue(self.store.is_processed(sha256_file(source)))

    def test_reprocess_writes_a_new_note_and_stops(self):
        source = self.write_audio("20260923 155900.m4a", 20000)
        self.tick()
        self.tick(20)
        prepare_reprocess(self.cfg, self.store, source)
        again = self.tick(0, only_path=source, force=True)
        self.assertEqual(again.processed, 1)
        self.assertEqual(len(list(self.cfg.inbox_dir.glob("*.md"))), 2)
        self.assertEqual(self.engine.calls, 2)
        third = self.tick(30)
        self.assertEqual(third.processed, 0)
        self.assertEqual(len(list(self.cfg.inbox_dir.glob("*.md"))), 2)
        self.assertEqual(self.engine.calls, 2)

    def test_two_memos_in_the_same_minute_get_two_notes(self):
        self.write_audio("20260923 155900.m4a", payload=b"a" * 20000)
        self.write_audio("20260923 155901.m4a", payload=b"b" * 20000)
        self.tick()
        done = self.tick(20)
        self.assertEqual(done.processed, 2)
        names = sorted(path.name for path in self.cfg.inbox_dir.glob("*.md"))
        self.assertEqual(
            names,
            [
                "2026-09-23-1559-voice-memo-2.md",
                "2026-09-23-1559-voice-memo.md",
            ],
        )
        self.assertEqual(len(list(self.cfg.audio_dir.rglob("*.m4a"))), 2)

    def test_junk_and_tiny_files_are_ignored(self):
        recordings = self.cfg.recordings_dir
        (recordings / "Cloud.db").write_bytes(b"a" * 20000)
        (recordings / "draw.waveform").write_bytes(b"a" * 20000)
        (recordings / ".hidden.m4a").write_bytes(b"a" * 20000)
        (recordings / "memo.m4a.icloud").write_bytes(b"a" * 20000)
        (recordings / "clip_SUPPORT_1.m4a").write_bytes(b"a" * 20000)
        self.write_audio("tiny.m4a", 100)
        self.tick()
        result = self.tick(30)
        self.assertEqual(result.processed, 0)
        self.assertEqual(self.engine.calls, 0)
        self.assertEqual(list(self.cfg.inbox_dir.glob("*.md")), [])

    def test_unreadable_watch_folder_fails_clearly(self):
        os.chmod(self.cfg.recordings_dir, 0)
        try:
            with self.assertRaises(IngestError) as caught:
                self.tick()
        finally:
            os.chmod(self.cfg.recordings_dir, 0o755)
        self.assertIn("Full Disk Access", str(caught.exception))

    def test_second_worker_does_not_run(self):
        self.cfg.lock_path.parent.mkdir(parents=True, exist_ok=True)
        held = self.cfg.lock_path.open("a+")
        try:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run_sweep(self.cfg, transcriber=self.engine)
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
        self.assertTrue(any("another worker" in message for message in result.messages))
        self.assertEqual(self.engine.calls, 0)

    def test_summarize_retries_without_whisper(self):
        self.write_audio("20260923 155900.m4a", 20000)
        self.tick()
        self.tick(20)
        note = next(self.cfg.inbox_dir.glob("*.md"))
        before = note.read_text(encoding="utf-8")
        script = self.tmp / "summarize.sh"
        script.write_text(
            "#!/bin/sh\ncat <<'EOF'\n## Summary\n- A short memo.\n\n## Action items\n- [ ] File it\nEOF\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        from dataclasses import replace

        from memo_ingest.note import transcript_section

        cfg = replace(
            self.cfg,
            summarize_enabled=True,
            summarize_command=str(script),
        )
        summarize_note(note, cfg)
        after = note.read_text(encoding="utf-8")
        self.assertIn("status: processed", after)
        self.assertIn("- A short memo.", after)
        self.assertIn("- [ ] File it", after)
        self.assertEqual(transcript_section(before), transcript_section(after))
        self.assertEqual(self.engine.calls, 1)


if __name__ == "__main__":
    unittest.main()
