"""Decide when a Voice Memos file is finished downloading.

iCloud often creates a tiny file and then grows it. A single create event is
not enough. The file is ready only after two observations, in this process or
a later one, see the same size and mtime for at least `stable_seconds`.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

# macOS stat.h: file has no local data yet (iCloud placeholder).
SF_DATALESS = 0x40000000


@dataclass(frozen=True)
class Observation:
    size: int
    mtime_ns: int
    first_seen: float


@dataclass(frozen=True)
class Decision:
    state: str  # ignore | wait | ready
    observation: Observation | None
    reason: str


def is_audio_candidate(path: Path) -> bool:
    name = path.name
    if not name or name.startswith("."):
        return False
    lower = name.lower()
    if not lower.endswith(".m4a"):
        return False
    if lower.endswith(".icloud"):
        return False
    if "_support" in lower:
        return False
    return True


def consider(
    path: Path,
    previous: Observation | None,
    now: float,
    *,
    min_bytes: int,
    stable_seconds: float,
) -> Decision:
    if not is_audio_candidate(path):
        return Decision("ignore", None, "not an .m4a voice memo")
    try:
        st = path.lstat()
    except OSError as exc:
        return Decision("ignore", None, f"stat failed: {exc}")
    if stat.S_ISLNK(st.st_mode):
        return Decision("ignore", None, "symlink")
    if not stat.S_ISREG(st.st_mode):
        return Decision("ignore", None, "not a regular file")
    flags = getattr(st, "st_flags", 0) or 0
    if flags & SF_DATALESS:
        # Drop any previous sighting so the clock starts when bytes arrive.
        return Decision("wait", None, "iCloud placeholder (dataless)")

    size = st.st_size
    mtime_ns = st.st_mtime_ns
    if size < min_bytes:
        return Decision(
            "wait",
            Observation(size, mtime_ns, now),
            f"size {size} is below minimum {min_bytes}",
        )

    same = (
        previous is not None
        and previous.size == size
        and previous.mtime_ns == mtime_ns
        and previous.size >= min_bytes
    )
    if not same or now < previous.first_seen:
        why = "first sight" if previous is None else "size or mtime changed"
        if previous is not None and now < previous.first_seen:
            why = "clock moved backwards"
        return Decision("wait", Observation(size, mtime_ns, now), f"{why}; waiting for the file to settle")

    elapsed = now - previous.first_seen
    observation = Observation(size, mtime_ns, previous.first_seen)
    if elapsed < stable_seconds:
        return Decision(
            "wait",
            observation,
            f"unchanged for {elapsed:.1f}s; need {stable_seconds:g}s",
        )
    return Decision("ready", observation, f"unchanged for {elapsed:.1f}s")
