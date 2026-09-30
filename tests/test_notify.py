"""Unit tests for summarize notification and event marker (fail-soft)."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from memo_ingest.notify import (
    append_summarize_event,
    note_heading,
    notify_summarize_complete,
)
from memo_ingest.summarize import summarize_note
from tests.support import make_config


class NotifyTests(unittest.TestCase):
    def test_note_heading_prefers_h1(self):
        self.assertEqual(note_heading("# Weekly sync\n\nbody", "fallback"), "Weekly sync")
        self.assertEqual(note_heading("no heading here", "stem-name"), "stem-name")

    def test_append_summarize_event_writes_jsonl(self):
        with tempfile.TemporaryDirectory(prefix="memo-notify-") as raw:
            events = Path(raw) / "summarize-events.jsonl"
            note = Path(raw) / "note.md"
            written = append_summarize_event(note, "ok", events_path=events)
            self.assertEqual(written, events)
            lines = events.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["note_path"], str(note))
            self.assertEqual(record["status"], "ok")
            self.assertIn("ts", record)
            append_summarize_event(note, "ok", events_path=events)
            self.assertEqual(len(events.read_text(encoding="utf-8").strip().splitlines()), 2)

    def test_notify_failure_does_not_fail_complete(self):
        with tempfile.TemporaryDirectory(prefix="memo-notify-") as raw:
            events = Path(raw) / "events.jsonl"
            note = Path(raw) / "voice.md"
            note.write_text("# Hello\n", encoding="utf-8")
            with patch(
                "memo_ingest.notify.show_macos_notification",
                side_effect=RuntimeError("boom"),
            ):
                notify_summarize_complete(
                    note,
                    notify=True,
                    note_text=note.read_text(encoding="utf-8"),
                    events_path=events,
                )
            record = json.loads(events.read_text(encoding="utf-8").strip())
            self.assertEqual(record["status"], "ok")

    def test_notify_disabled_skips_banner_keeps_marker(self):
        with tempfile.TemporaryDirectory(prefix="memo-notify-") as raw:
            events = Path(raw) / "events.jsonl"
            note = Path(raw) / "voice.md"
            note.write_text("# Hello\n", encoding="utf-8")
            with patch("memo_ingest.notify.show_macos_notification") as banner:
                notify_summarize_complete(
                    note,
                    notify=False,
                    note_text="# Hello\n",
                    events_path=events,
                )
            banner.assert_not_called()
            self.assertTrue(events.is_file())

    def test_summarize_survives_notify_failure(self):
        tmp, cfg = make_config()
        inbox = cfg.inbox_dir
        inbox.mkdir(parents=True)
        note = inbox / "2026-09-30-1200-test.md"
        note.write_text(
            "---\nstatus: raw\n---\n# Test memo\n\n## Transcript\n\nHello there.\n\n"
            "## Summary\n\n<!-- filled -->\n\n## Action items\n\n- [ ]\n",
            encoding="utf-8",
        )
        script = tmp / "summarize.sh"
        script.write_text(
            "#!/bin/sh\ncat <<'EOF'\n## Summary\n- Done.\n\n## Action items\n- [ ] Follow up\nEOF\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        events = tmp / "summarize-events.jsonl"
        cfg = replace(
            cfg,
            summarize_enabled=True,
            summarize_command=str(script),
            summarize_notify=True,
        )
        with patch(
            "memo_ingest.notify.show_macos_notification",
            side_effect=OSError("no display"),
        ), patch(
            "memo_ingest.notify.default_summarize_events_path",
            return_value=events,
        ):
            summarize_note(note, cfg)
        after = note.read_text(encoding="utf-8")
        self.assertIn("status: processed", after)
        self.assertIn("- Done.", after)
        self.assertTrue(events.is_file())
        record = json.loads(events.read_text(encoding="utf-8").strip())
        self.assertEqual(record["note_path"], str(note))
        self.assertEqual(record["status"], "ok")


if __name__ == "__main__":
    unittest.main()
