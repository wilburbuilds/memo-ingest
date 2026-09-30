"""Local macOS notification + private event marker after summarize.

Fail-soft: never raise into summarize. Marker file is for Grok Bot sweeps.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger("memo_ingest")


def default_summarize_events_path() -> Path:
    return Path.home() / ".local" / "share" / "memo-ingest" / "summarize-events.jsonl"


def note_heading(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            heading = line[2:].strip()
            if heading:
                return heading
    return fallback


def _applescript_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def show_macos_notification(title: str, body: str) -> None:
    """Best-effort UserNotifications via osascript. Raises on hard failures."""
    script = (
        f'display notification "{_applescript_string(body)}" '
        f'with title "{_applescript_string(title)}"'
    )
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"osascript notification failed: {detail}")


def append_summarize_event(
    note_path: Path,
    status: str,
    *,
    events_path: Path | None = None,
    ts: datetime | None = None,
) -> Path:
    """Append one JSON line. Returns the path written."""
    path = events_path or default_summarize_events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (ts or datetime.now().astimezone()).isoformat(timespec="seconds")
    record = {
        "ts": stamp,
        "note_path": str(note_path),
        "status": status,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def notify_summarize_complete(
    note_path: Path,
    *,
    notify: bool = True,
    note_text: str | None = None,
    events_path: Path | None = None,
) -> None:
    """After a successful summarize write: marker always, notification if enabled.

    Errors are logged and swallowed so summarize stays durable.
    """
    try:
        append_summarize_event(note_path, "ok", events_path=events_path)
    except Exception as exc:
        LOG.warning("summarize event marker failed for %s: %s", note_path.name, exc)

    if not notify:
        return

    try:
        text = note_text
        if text is None:
            try:
                text = note_path.read_text(encoding="utf-8")
            except OSError:
                text = ""
        title_line = note_heading(text or "", note_path.stem)
        body = f"{title_line} — open transcripts"
        show_macos_notification("Memo summarized", body)
    except Exception as exc:
        LOG.warning("summarize notification failed for %s: %s", note_path.name, exc)
