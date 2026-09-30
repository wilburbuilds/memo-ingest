"""Unit tests for diarization labeling and speaker naming (no sherpa models required)."""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp

from memo_ingest.diarize import DiarizeTurn, assign_speakers
from memo_ingest.name_speakers import name_speakers_note, parse_speaker_mapping
from memo_ingest.note import (
    parse_frontmatter,
    render_note,
    render_transcript,
    rewrite_speaker_labels,
    speaker_ids_in_segments,
    transcript_section,
)
from memo_ingest.transcribe import Segment
from tests.support import make_config


class DiarizeLabelTest(unittest.TestCase):
    def test_assign_speakers_by_overlap(self):
        segments = [
            Segment(0.0, 1.0, "Hello"),
            Segment(1.2, 2.5, "This is Alex"),
            Segment(3.0, 4.0, "Thanks"),
        ]
        turns = [
            DiarizeTurn(0.0, 1.1, "SPEAKER_00"),
            DiarizeTurn(1.1, 2.8, "SPEAKER_01"),
            DiarizeTurn(2.8, 5.0, "SPEAKER_00"),
        ]
        labeled = assign_speakers(segments, turns)
        self.assertEqual([s.speaker_id for s in labeled], ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"])

    def test_assign_speakers_nearest_when_no_overlap(self):
        segments = [Segment(10.0, 10.2, "gap")]
        turns = [
            DiarizeTurn(0.0, 1.0, "SPEAKER_00"),
            DiarizeTurn(20.0, 21.0, "SPEAKER_01"),
        ]
        labeled = assign_speakers(segments, turns)
        self.assertEqual(labeled[0].speaker_id, "SPEAKER_00")

    def test_render_includes_speaker_labels(self):
        segments = [
            Segment(0.0, 1.0, "Hello", "SPEAKER_00"),
            Segment(1.5, 2.0, "Hi", "SPEAKER_01"),
        ]
        body = render_transcript("Hello Hi", segments)
        self.assertIn("[00:00] SPEAKER_00: Hello", body)
        self.assertIn("[00:01] SPEAKER_01: Hi", body)

    def test_frontmatter_speakers_true_and_count(self):
        recorded = datetime(2026, 9, 30, 12, 0, 0).astimezone()
        segments = [
            Segment(0.0, 1.0, "A", "SPEAKER_00"),
            Segment(1.0, 2.0, "B", "SPEAKER_01"),
        ]
        text = render_note(
            created=recorded,
            recorded=recorded,
            audio_field="[[attachments/audio/2026/x.m4a]]",
            duration_sec=2,
            size=100,
            sha256="ab" * 32,
            model="mlx-whisper-test",
            language="en",
            runtime_sec=0.1,
            title=None,
            transcript_body=render_transcript("A B", segments),
            speakers=True,
            speaker_count=len(speaker_ids_in_segments(segments)),
        )
        frontmatter = parse_frontmatter(text)
        self.assertEqual(frontmatter["speakers"], "true")
        self.assertEqual(frontmatter["speaker_count"], "2")
        self.assertIn("SPEAKER_00:", text)
        self.assertNotIn("speaker_names:", text)


class NameSpeakersTest(unittest.TestCase):
    def test_parse_mapping_lines_and_json(self):
        self.assertEqual(
            parse_speaker_mapping("SPEAKER_00: Wilbur\nSPEAKER_01: Alex\n"),
            {"SPEAKER_00": "Wilbur", "SPEAKER_01": "Alex"},
        )
        self.assertEqual(
            parse_speaker_mapping('{"SPEAKER_00": "Wilbur", "SPEAKER_01": "Alex"}'),
            {"SPEAKER_00": "Wilbur", "SPEAKER_01": "Alex"},
        )
        self.assertEqual(
            parse_speaker_mapping("SPEAKER_00: unknown\nSPEAKER_01: Sam\n")["SPEAKER_00"],
            "SPEAKER_00",
        )

    def test_rewrite_labels_keeps_spoken_text(self):
        recorded = datetime(2026, 9, 30, 12, 0, 0).astimezone()
        body = "[00:00] SPEAKER_00: Hello there\n\n[00:02] SPEAKER_01: Hi Wilbur"
        original = render_note(
            created=recorded,
            recorded=recorded,
            audio_field="[[a.m4a]]",
            duration_sec=3,
            size=10,
            sha256="cd" * 32,
            model="test",
            language="en",
            runtime_sec=0.1,
            title=None,
            transcript_body=body,
            speakers=True,
            speaker_count=2,
        )
        mapping = {"SPEAKER_00": "Wilbur", "SPEAKER_01": "Alex"}
        updated = rewrite_speaker_labels(original, mapping)
        section = transcript_section(updated)
        self.assertIn("[00:00] Wilbur: Hello there", section)
        self.assertIn("[00:02] Alex: Hi Wilbur", section)
        self.assertNotIn("SPEAKER_00", section)
        self.assertIn("Hello there", section)
        self.assertIn("Hi Wilbur", section)

    def test_name_speakers_command_rewrites_note(self):
        tmp, cfg = make_config()
        recorded = datetime(2026, 9, 30, 12, 0, 0).astimezone()
        note = cfg.inbox_dir / "note.md"
        cfg.inbox_dir.mkdir(parents=True)
        content = render_note(
            created=recorded,
            recorded=recorded,
            audio_field="[[a.m4a]]",
            duration_sec=3,
            size=10,
            sha256="ef" * 32,
            model="test",
            language="en",
            runtime_sec=0.1,
            title=None,
            transcript_body="[00:00] SPEAKER_00: Hello\n\n[00:01] SPEAKER_01: Hi",
            speakers=True,
            speaker_count=2,
        )
        note.write_text(content, encoding="utf-8")
        script = tmp / "name.sh"
        script.write_text(
            "#!/bin/sh\ncat <<'EOF'\nSPEAKER_00: Wilbur\nSPEAKER_01: Alex\nEOF\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        from dataclasses import replace

        cfg = replace(
            cfg,
            name_speakers_enabled=True,
            name_speakers_command=str(script),
        )
        self.assertTrue(name_speakers_note(note, cfg))
        text = note.read_text(encoding="utf-8")
        frontmatter = parse_frontmatter(text)
        self.assertIn("Wilbur: Hello", text)
        self.assertIn("Alex: Hi", text)
        self.assertNotIn("SPEAKER_00", transcript_section(text))
        names = json.loads(frontmatter["speaker_names"])
        self.assertEqual(names["SPEAKER_00"], "Wilbur")
        self.assertEqual(names["SPEAKER_01"], "Alex")


if __name__ == "__main__":
    unittest.main()
