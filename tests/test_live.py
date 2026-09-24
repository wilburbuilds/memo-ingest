"""One real mlx-whisper pass on a short spoken .m4a. Skipped if whisper is not installed."""

import shutil
import subprocess
import unittest
from pathlib import Path

from memo_ingest.audio import sha256_file
from memo_ingest.note import parse_frontmatter
from memo_ingest.store import Store
from memo_ingest.transcribe import mlx_importable
from memo_ingest.worker import sweep
from tests.support import make_config

SPOKEN = (
    "This is a test voice memo. The pipeline should write one Obsidian note "
    "and skip the same recording on the second run."
)


def _sample_m4a(destination: Path) -> None:
    aiff = destination.with_suffix(".aiff")
    subprocess.run(["say", "-o", str(aiff), SPOKEN], check=True, capture_output=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(aiff), "-c:a", "aac", "-b:a", "64k", str(destination)],
        check=True,
        capture_output=True,
    )
    aiff.unlink(missing_ok=True)


@unittest.skipUnless(mlx_importable(), "mlx-whisper is not installed")
@unittest.skipUnless(shutil.which("say") and shutil.which("ffmpeg"), "say or ffmpeg missing")
class LiveTranscribeTest(unittest.TestCase):
    def test_sample_memo_becomes_one_note(self):
        from memo_ingest.transcribe import build_transcriber

        tmp, cfg = make_config()
        audio = cfg.recordings_dir / "20260923 155900.m4a"
        _sample_m4a(audio)
        self.assertGreater(audio.stat().st_size, cfg.min_bytes)
        store = Store(cfg.state_db)
        engine = build_transcriber(cfg)
        try:
            waiting = sweep(cfg, store, transcriber=engine, now=1_000)
            self.assertEqual(waiting.processed, 0)
            done = sweep(cfg, store, transcriber=engine, now=1_020)
            self.assertEqual(done.failed, 0, done.errors)
            self.assertEqual(done.processed, 1)
            notes = list(cfg.inbox_dir.glob("*.md"))
            self.assertEqual(len(notes), 1)
            text = notes[0].read_text(encoding="utf-8")
            frontmatter = parse_frontmatter(text)
            self.assertEqual(frontmatter["status"], "raw")
            self.assertEqual(frontmatter["source"], "voice-memos")
            self.assertEqual(frontmatter["audio_sha256"], sha256_file(audio))
            self.assertTrue(frontmatter["model"].startswith("mlx-whisper-"))
            self.assertEqual(frontmatter["language"], "en")
            lowered = text.lower()
            hits = sum(word in lowered for word in ("test", "voice", "memo", "note"))
            self.assertGreaterEqual(hits, 2, text)
            copied = list(cfg.audio_dir.rglob("*.m4a"))
            self.assertEqual(len(copied), 1)
            self.assertTrue(audio.is_file())

            again = sweep(cfg, store, transcriber=engine, now=1_040)
            self.assertEqual(again.processed, 0)
            self.assertEqual(len(list(cfg.inbox_dir.glob("*.md"))), 1)
        finally:
            store.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
