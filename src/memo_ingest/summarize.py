"""Optional second pass. Never calls Whisper. Edits Summary and Action items only."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from memo_ingest.config import Config
from memo_ingest.errors import IngestError
from memo_ingest.note import parse_frontmatter, splice_summary, transcript_section, write_note


def summarize_note(note_path: Path, cfg: Config) -> None:
    if not cfg.summarize_enabled:
        raise IngestError("summarize is disabled in config ([summarize] enabled = false)")
    if not cfg.summarize_command:
        raise IngestError(
            "summarize is enabled but [summarize] command is empty.\n"
            "Set a local command that reads the prompt on stdin, or leave enabled = false."
        )
    if not cfg.prompt_file.is_file():
        raise IngestError(f"summarize prompt file not found: {cfg.prompt_file}")
    original = note_path.read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(original)
    if frontmatter.get("status") == "processed":
        return
    transcript = transcript_section(original)
    prompt = cfg.prompt_file.read_text(encoding="utf-8")
    stdin = f"{prompt.rstrip()}\n\n---\n\nTRANSCRIPT:\n{transcript.strip()}\n"
    env = os.environ.copy()
    env["MEMO_PROMPT_FILE"] = str(cfg.prompt_file)
    env["MEMO_NOTE_PATH"] = str(note_path)
    try:
        proc = subprocess.run(
            cfg.summarize_command,
            input=stdin,
            text=True,
            shell=True,
            capture_output=True,
            env=env,
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise IngestError(f"summarize command timed out for {note_path.name}") from exc
    except OSError as exc:
        raise IngestError(f"summarize command failed to start: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise IngestError(f"summarize command exited {proc.returncode}: {tail}")
    updated = splice_summary(original, proc.stdout or "")
    # Transcript bytes between the two headings must be unchanged.
    if transcript_section(updated) != transcript:
        raise IngestError("summarize would have changed the transcript; note left untouched")
    write_note(note_path, updated)


def raw_notes(inbox: Path) -> list[Path]:
    if not inbox.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(inbox.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if parse_frontmatter(text).get("status") == "raw":
            found.append(path)
    return found
