"""Optional Grok Build pass: map SPEAKER_XX → names from transcript context. Text only."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from memo_ingest.config import Config
from memo_ingest.errors import IngestError
from memo_ingest.note import (
    parse_frontmatter,
    replace_frontmatter_fields,
    rewrite_speaker_labels,
    transcript_section,
    write_note,
)

SPEAKER_RE = re.compile(r"\bSPEAKER_\d+\b")
MAP_LINE_RE = re.compile(
    r"^\s*(SPEAKER_\d+)\s*[:=→\-]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def name_speakers_note(note_path: Path, cfg: Config) -> bool:
    """Rewrite SPEAKER_XX labels using the configured naming command.

    Returns True when the note was updated. Leaves spoken text intact; only labels change.
    """
    if not cfg.name_speakers_enabled:
        raise IngestError(
            "name-speakers is disabled in config ([name_speakers] enabled = false)"
        )
    if not cfg.name_speakers_command:
        raise IngestError(
            "name-speakers is enabled but [name_speakers] command is empty.\n"
            "Set scripts/grok-name-speakers.sh, or leave enabled = false."
        )
    if not cfg.name_speakers_prompt_file.is_file():
        raise IngestError(
            f"name-speakers prompt file not found: {cfg.name_speakers_prompt_file}"
        )
    original = note_path.read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(original)
    if frontmatter.get("speakers") != "true":
        return False
    transcript = transcript_section(original)
    if not SPEAKER_RE.search(transcript):
        return False
    # Already named (labels rewritten away, or map present and no SPEAKER_ left).
    existing_names = frontmatter.get("speaker_names") or ""
    if existing_names and existing_names not in {"{}", ""} and not SPEAKER_RE.search(
        transcript
    ):
        return False

    prompt = cfg.name_speakers_prompt_file.read_text(encoding="utf-8")
    stdin = f"{prompt.rstrip()}\n\n---\n\nTRANSCRIPT:\n{transcript.strip()}\n"
    env = os.environ.copy()
    env["MEMO_PROMPT_FILE"] = str(cfg.name_speakers_prompt_file)
    env["MEMO_NOTE_PATH"] = str(note_path)
    try:
        proc = subprocess.run(
            cfg.name_speakers_command,
            input=stdin,
            text=True,
            shell=True,
            capture_output=True,
            env=env,
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise IngestError(f"name-speakers command timed out for {note_path.name}") from exc
    except OSError as exc:
        raise IngestError(f"name-speakers command failed to start: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise IngestError(f"name-speakers command exited {proc.returncode}: {tail}")

    mapping = parse_speaker_mapping(proc.stdout or "")
    if not mapping:
        raise IngestError(
            "name-speakers output had no SPEAKER_XX → name lines; note left untouched"
        )

    updated_body = rewrite_speaker_labels(original, mapping)
    fields = {
        "speaker_names": json.dumps(
            {key: mapping[key] for key in sorted(mapping)},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    }
    updated = replace_frontmatter_fields(updated_body, fields)
    # Spoken text (without labels) must stay the same length of content aside from labels.
    if _strip_speaker_labels(transcript_section(updated)) != _strip_speaker_labels(
        transcript
    ):
        raise IngestError(
            "name-speakers would have changed spoken transcript text; note left untouched"
        )
    write_note(note_path, updated)
    return True


def parse_speaker_mapping(text: str) -> dict[str, str]:
    """Accept JSON object or SPEAKER_XX: Name lines."""
    cleaned = text.strip()
    if not cleaned:
        return {}
    # Try fenced or raw JSON first.
    candidate = cleaned
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        brace = re.search(r"\{[^{}]+\}", cleaned, re.DOTALL)
        if brace:
            candidate = brace.group(0)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        payload = None
    mapping: dict[str, str] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            label = str(key).strip().upper()
            if not label.startswith("SPEAKER_"):
                # Allow bare "00" keys.
                if re.fullmatch(r"\d+", label):
                    label = f"SPEAKER_{int(label):02d}"
                else:
                    continue
            name = " ".join(str(value).split())
            if name and name.lower() not in {"unknown", "n/a", "none", "-"}:
                mapping[label] = name
            elif name:
                mapping[label] = label  # keep SPEAKER_XX if model says unknown
        if mapping:
            return mapping
    for match in MAP_LINE_RE.finditer(cleaned):
        label = match.group(1).upper()
        name = " ".join(match.group(2).split()).strip(" \"'")
        if not name or name.lower() in {"unknown", "n/a", "none", "-"}:
            mapping[label] = label
        else:
            mapping[label] = name
    return mapping


def _strip_speaker_labels(section: str) -> str:
    """Remove SPEAKER_XX or replaced Name labels after timestamps for integrity check."""
    # [mm:ss] LABEL: text  →  [mm:ss] text
    return re.sub(
        r"(\[\d{1,2}:\d{2}(?::\d{2})?\])\s+[^:\n]+:\s*",
        r"\1 ",
        section,
    )


def notes_needing_names(inbox: Path) -> list[Path]:
    if not inbox.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(inbox.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter = parse_frontmatter(text)
        if frontmatter.get("speakers") != "true":
            continue
        if not SPEAKER_RE.search(transcript_section(text) if "## Transcript" in text else text):
            continue
        found.append(path)
    return found
