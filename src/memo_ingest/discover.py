"""Find the Voice Memos library and an Obsidian vault. Do not invent either."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from memo_ingest.errors import IngestError

RECORDING_CANDIDATES = (
    Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings",
    Path.home() / "Library/Application Support/com.apple.voicememos/Recordings",
    Path.home() / "Library/Application Support/com.apple.voice-memos/Recordings",
)

VAULT_ROOTS = (
    Path.home() / "vaults",
    Path.home() / "Documents",
    Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents",
)


@dataclass(frozen=True)
class RecordingsProbe:
    path: Path
    exists: bool
    readable: bool
    m4a_count: int
    newest_mtime: float | None
    error: str | None


def recording_candidates() -> tuple[Path, ...]:
    return RECORDING_CANDIDATES


def probe_recordings(paths: tuple[Path, ...] | None = None) -> list[RecordingsProbe]:
    probes: list[RecordingsProbe] = []
    for path in paths or RECORDING_CANDIDATES:
        probes.append(_probe_one(path))
    return probes


def _probe_one(path: Path) -> RecordingsProbe:
    if not path.exists():
        return RecordingsProbe(path, False, False, 0, None, "not found")
    if not path.is_dir():
        return RecordingsProbe(path, True, False, 0, None, "not a directory")
    try:
        names = os.listdir(path)
    except PermissionError as exc:
        return RecordingsProbe(path, True, False, 0, None, f"Operation not permitted ({exc})")
    except OSError as exc:
        return RecordingsProbe(path, True, False, 0, None, str(exc))
    newest: float | None = None
    count = 0
    for name in names:
        if not name.lower().endswith(".m4a") or name.startswith("."):
            continue
        file_path = path / name
        try:
            st = file_path.stat()
        except OSError:
            continue
        if not file_path.is_file():
            continue
        count += 1
        if newest is None or st.st_mtime > newest:
            newest = st.st_mtime
    return RecordingsProbe(path, True, True, count, newest, None)


def choose_recordings(probes: list[RecordingsProbe]) -> RecordingsProbe | None:
    """Prefer a readable folder that already has .m4a files. Else the first that exists."""
    with_audio = [p for p in probes if p.readable and p.m4a_count > 0]
    if with_audio:
        return max(with_audio, key=lambda p: p.newest_mtime or 0)
    for probe in probes:
        if probe.exists:
            return probe
    return None


def directory_error(path: Path) -> str | None:
    """None when the directory can be listed."""
    if not path.exists():
        return "not found"
    if not path.is_dir():
        return "not a directory"
    try:
        os.listdir(path)
    except PermissionError as exc:
        return f"Operation not permitted ({exc})"
    except OSError as exc:
        return str(exc)
    return None


def find_vaults(roots: tuple[Path, ...] | None = None) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots or VAULT_ROOTS:
        if not root.is_dir():
            continue
        candidates = [root]
        try:
            candidates.extend(child for child in root.iterdir() if child.is_dir())
        except OSError:
            pass
        for folder in candidates:
            marker = folder / ".obsidian"
            try:
                is_vault = marker.is_dir()
            except OSError:
                continue
            if not is_vault:
                continue
            resolved = folder.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(folder)
    return found


def choose_vault(vaults: list[Path], explicit: Path | None) -> Path:
    if explicit is not None:
        explicit = explicit.expanduser()
        if not explicit.is_dir() or not (explicit / ".obsidian").is_dir():
            raise IngestError(
                f"Not an Obsidian vault (no .obsidian directory): {explicit}\n"
                "memo-ingest will not create a vault."
            )
        return explicit
    if not vaults:
        searched = "\n".join(f"  - {root}" for root in VAULT_ROOTS)
        raise IngestError(
            "No Obsidian vault found. Searched:\n"
            f"{searched}\n"
            "Re-run with the vault that already exists:\n"
            "  memo-ingest setup --vault /path/to/vault"
        )
    personal = [vault for vault in vaults if "personal" in vault.name.lower()]
    if len(personal) == 1:
        return personal[0]
    if len(vaults) == 1:
        return vaults[0]
    lines = "\n".join(f"  - {vault}" for vault in vaults)
    raise IngestError(
        "More than one Obsidian vault found. Pass one explicitly:\n"
        f"{lines}\n"
        "  memo-ingest setup --vault /path/to/vault"
    )


def missing_recordings_message(probes: list[RecordingsProbe]) -> str:
    lines = ["Voice Memos library was not found. Paths checked:"]
    for probe in probes:
        state = probe.error or ("ok" if probe.exists else "not found")
        lines.append(f"  - {probe.path} ({state})")
    lines.append("")
    lines.append("Turn on iCloud Voice Memos, then open the Voice Memos app on this Mac once:")
    lines.append("  iPhone: Settings → [your name] → iCloud → Voice Memos → On")
    lines.append("  Mac: System Settings → [your name] → iCloud → Voice Memos → On")
    lines.append("")
    lines.append("Fallback watch folder, if the library stays empty or unreadable:")
    lines.append("  Open Voice Memos on the Mac, select a memo, Share → Save to Files,")
    lines.append("  and point setup at that folder:")
    lines.append("  memo-ingest setup --recordings ~/VoiceMemosExport --force")
    return "\n".join(lines)


def unreadable_recordings_message(path: Path, detail: str) -> str:
    python_bin = Path(sys.executable).resolve()
    return (
        f"Voice Memos folder exists but this process cannot read it:\n"
        f"  {path}\n"
        f"  {detail}\n"
        "\n"
        "Grant Full Disk Access, then run `memo-ingest once`:\n"
        "  System Settings → Privacy & Security → Full Disk Access\n"
        f"  Add: {python_bin}\n"
        "  Also add Terminal (or iTerm) if you run memo-ingest from a shell.\n"
        "  If the venv python is a symlink, add the path shown above (it is resolved).\n"
        "\n"
        "Fallback watch folder:\n"
        "  Open Voice Memos, select a memo, Share → Save to Files,\n"
        "  then: memo-ingest setup --recordings ~/VoiceMemosExport --force"
    )
