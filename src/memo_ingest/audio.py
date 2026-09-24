"""File identity, duration, and a best-effort recorded time."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_FILENAME_COMPACT = re.compile(
    r"(?P<y>20\d{2})(?P<mo>\d{2})(?P<d>\d{2})[ T_\-](?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})"
)
_FILENAME_DASHED = re.compile(
    r"(?P<y>20\d{2})-(?P<mo>\d{2})-(?P<d>\d{2})[ T_\-](?P<h>\d{2})[:\-.]?(?P<mi>\d{2})(?:[:\-.]?(?P<s>\d{2}))?"
)

_GENERIC_TITLE = re.compile(
    r"(new recording|audio recording|voice memo|recording|untitled)( \d+)?",
    re.IGNORECASE,
)
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Probe:
    duration_sec: float | None
    title: str | None
    creation_time: str | None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def probe_audio(path: Path) -> Probe:
    if shutil.which("ffprobe") is None:
        return Probe(None, None, None)
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Probe(None, None, None)
    if proc.returncode != 0 or not proc.stdout.strip():
        return Probe(None, None, None)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return Probe(None, None, None)
    fmt = data.get("format") or {}
    tags = {str(key).lower(): value for key, value in (fmt.get("tags") or {}).items()}
    duration: float | None = None
    raw_duration = fmt.get("duration")
    if raw_duration is not None:
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = None
    title = tags.get("title")
    if isinstance(title, str):
        title = title.strip() or None
    else:
        title = None
    created = tags.get("creation_time")
    if not isinstance(created, str):
        created = None
    return Probe(duration, title, created)


def clean_title(title: str | None) -> str | None:
    if not title:
        return None
    text = " ".join(title.replace("\x00", " ").split())
    if not text or len(text) > 120:
        return None
    if _GENERIC_TITLE.fullmatch(text):
        return None
    if _UUID.fullmatch(text):
        return None
    if re.fullmatch(r"[\d\s._:\-]+", text):
        return None
    return text


def datetime_from_filename(name: str) -> datetime | None:
    match = _FILENAME_COMPACT.search(name) or _FILENAME_DASHED.search(name)
    if match is None:
        return None
    second = match.group("s") or "00"
    try:
        naive = datetime(
            int(match.group("y")),
            int(match.group("mo")),
            int(match.group("d")),
            int(match.group("h")),
            int(match.group("mi")),
            int(second),
        )
    except ValueError:
        return None
    # Naive wall time is interpreted in the system timezone, including DST.
    return naive.astimezone()


def parse_ffprobe_time(value: str) -> datetime | None:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed.astimezone()


def recorded_at(path: Path, probe: Probe) -> datetime:
    from_name = datetime_from_filename(path.name)
    if from_name is not None:
        return from_name
    if probe.creation_time:
        parsed = parse_ffprobe_time(probe.creation_time)
        if parsed is not None and parsed.year >= 2000:
            return parsed
    try:
        st = path.stat()
    except OSError:
        return datetime.now().astimezone()
    birth = getattr(st, "st_birthtime", None)
    if birth:
        return datetime.fromtimestamp(birth).astimezone()
    return datetime.fromtimestamp(st.st_mtime).astimezone()


def recording_title(path: Path, probe: Probe) -> str | None:
    titled = clean_title(probe.title)
    if titled:
        return titled
    return clean_title(path.stem)
