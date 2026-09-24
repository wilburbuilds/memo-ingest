"""Obsidian markdown for one recording. The transcript section is written once."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from memo_ingest.audio import sha256_file
from memo_ingest.errors import IngestError


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60].strip("-")


def note_basename(when: datetime, title: str | None) -> str:
    stamp = when.strftime("%Y-%m-%d-%H%M")
    slug = slugify(title) if title else ""
    if not slug:
        slug = "voice-memo"
    return f"{stamp}-{slug}.md"


def audio_basename(when: datetime) -> str:
    return when.strftime("%Y-%m-%d-%H%M") + ".m4a"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    number = 2
    while True:
        candidate = path.with_name(f"{stem}-{number}{suffix}")
        if not candidate.exists():
            return candidate
        number += 1
        if number > 1000:
            raise IngestError(f"too many files named like {path.name}")


def format_number(value: float | None) -> str:
    if value is None:
        return "0"
    if abs(value - round(value)) < 0.05:
        return str(int(round(value)))
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def format_timestamp(seconds: float) -> str:
    whole = max(0, int(seconds))
    minutes, secs = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def render_transcript(text: str, segments: list[tuple[float, float, str]]) -> str:
    lines: list[str] = []
    for start, _end, segment_text in segments:
        cleaned = " ".join(segment_text.split())
        if not cleaned:
            continue
        lines.append(f"[{format_timestamp(start)}] {cleaned}")
    if lines:
        return "\n\n".join(lines)
    return " ".join(text.split())


def render_note(
    *,
    created: datetime,
    recorded: datetime,
    audio_field: str,
    duration_sec: float | None,
    size: int,
    sha256: str,
    model: str,
    language: str,
    runtime_sec: float,
    title: str | None,
    transcript_body: str,
) -> str:
    heading = title if title else f"Voice memo {recorded.strftime('%Y-%m-%d %H:%M')}"
    language_value = language if re.fullmatch(r"[A-Za-z0-9_-]+", language or "") else "unknown"
    body = transcript_body.rstrip()
    lines = [
        "---",
        "type: transcript",
        "source: voice-memos",
        f"created: {created.astimezone().isoformat(timespec='seconds')}",
        f"recorded: {recorded.astimezone().isoformat(timespec='seconds')}",
        f"audio: {json.dumps(audio_field)}",
        f"duration_sec: {format_number(duration_sec)}",
        f"bytes: {size}",
        f"audio_sha256: {sha256}",
        f"model: {model}",
        f"language: {language_value}",
        f"runtime_sec: {runtime_sec:.2f}",
        "speakers: false",
        "status: raw",
        "---",
        f"# {heading}",
        "",
        "## Transcript",
        "",
        body,
        "",
        "## Summary",
        "",
        "<!-- filled by optional second pass; leave heading in place -->",
        "",
        "## Action items",
        "",
        "- [ ]",
        "",
    ]
    return "\n".join(lines)


def write_note(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    data: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def find_note_by_hash(inbox: Path, sha256: str, ignored: set[str]) -> Path | None:
    if not inbox.is_dir():
        return None
    for path in sorted(inbox.glob("*.md")):
        if str(path) in ignored or path.name.endswith(".tmp"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if parse_frontmatter(text).get("audio_sha256") == sha256:
            return path
    return None


def audio_wikilink(vault_root: Path, audio_path: Path) -> str:
    relative = audio_path.resolve().relative_to(vault_root.resolve()).as_posix()
    return f"[[{relative}]]"


def place_audio(src: Path, audio_root: Path, when: datetime, sha256: str) -> Path:
    folder = audio_root / when.strftime("%Y")
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / audio_basename(when)
    candidate = base
    number = 2
    while candidate.exists():
        try:
            if sha256_file(candidate) == sha256:
                return candidate
        except OSError:
            pass
        candidate = base.with_name(f"{base.stem}-{number}{base.suffix}")
        number += 1
        if number > 1000:
            raise IngestError(f"too many audio files named like {base.name}")
    shutil.copy2(src, candidate)
    return candidate


def transcript_section(text: str) -> str:
    start = text.find("\n## Transcript\n")
    end = text.find("\n## Summary\n")
    if start < 0 or end < 0 or end <= start:
        raise IngestError("note is missing ## Transcript or ## Summary")
    return text[start:end]


def replace_status(text: str, status: str) -> str:
    if not text.startswith("---\n"):
        raise IngestError("note is missing frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise IngestError("note frontmatter is not closed")
    lines = []
    replaced = False
    for line in text[4:end].splitlines():
        if not replaced and line.startswith("status:"):
            lines.append(f"status: {status}")
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        raise IngestError("note frontmatter has no status field")
    return "---\n" + "\n".join(lines) + text[end:]


def splice_summary(note_text: str, summary_md: str) -> str:
    """Replace everything from ## Summary onward. The transcript bytes stay put."""
    cleaned = summary_md.strip()
    marker = cleaned.find("## Summary")
    if marker < 0 or "## Action items" not in cleaned:
        raise IngestError(
            "summarizer output must contain the headings ## Summary and ## Action items"
        )
    replacement = cleaned[marker:].strip() + "\n"
    position = note_text.find("\n## Summary\n")
    if position < 0:
        raise IngestError("note has no ## Summary heading")
    # Keep every byte before the summary heading, including the newline that
    # starts it, so the transcript section is unchanged.
    head = replace_status(note_text[:position], "processed")
    return head + "\n" + replacement
