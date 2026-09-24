"""One-at-a-time ingest. Safe to re-run after sleep or a crash."""

from __future__ import annotations

import fcntl
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from memo_ingest.audio import probe_audio, recorded_at, recording_title, sha256_file
from memo_ingest.config import Config
from memo_ingest.discover import directory_error, unreadable_recordings_message
from memo_ingest.errors import IngestError
from memo_ingest.gate import consider, is_audio_candidate
from memo_ingest.note import (
    audio_wikilink,
    find_note_by_hash,
    note_basename,
    parse_frontmatter,
    place_audio,
    render_note,
    render_transcript,
    unique_path,
    write_note,
)
from memo_ingest.store import Store
from memo_ingest.summarize import summarize_note
from memo_ingest.transcribe import build_transcriber

LOG = logging.getLogger("memo_ingest")


@dataclass
class SweepResult:
    files_seen: int = 0
    waiting: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    dry_run: int = 0
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.INFO)
    LOG.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    LOG.addHandler(file_handler)
    LOG.addHandler(stream_handler)
    LOG.propagate = False


@contextmanager
def worker_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def sweep(
    cfg: Config,
    store: Store,
    *,
    transcriber=None,
    dry_run: bool = False,
    only_path: Path | None = None,
    force: bool = False,
    now: float | None = None,
) -> SweepResult:
    """Scan the watch folder once. `now` is injectable so tests can move the clock."""
    _require_vault(cfg)
    clock = datetime.now().timestamp() if now is None else now
    result = SweepResult()
    paths = _paths_for_sweep(cfg, only_path)
    result.files_seen = len(paths)
    live = {str(path) for path in paths}
    if only_path is None:
        for stale in store.observation_paths():
            if stale not in live:
                store.delete_observation(stale)

    for path in paths:
        outcome = _handle_file(
            cfg,
            store,
            path,
            transcriber=transcriber,
            dry_run=dry_run,
            force=force and only_path is not None and path == only_path,
            now=clock,
        )
        _tally(result, outcome)
    return result


def run_sweep(
    cfg: Config,
    *,
    transcriber=None,
    dry_run: bool = False,
    only_path: Path | None = None,
    force: bool = False,
    now: float | None = None,
) -> SweepResult:
    """Lock, record the run, sweep. Used by the CLI. Tests call `sweep` directly."""
    configure_logging(cfg.log_file)
    with worker_lock(cfg.lock_path) as acquired:
        if not acquired:
            LOG.info("another worker is running; exiting")
            result = SweepResult()
            result.messages.append("another worker is running")
            return result
        store = Store(cfg.state_db)
        run_id = store.start_run()
        try:
            result = sweep(
                cfg,
                store,
                transcriber=transcriber,
                dry_run=dry_run,
                only_path=only_path,
                force=force,
                now=now,
            )
        except IngestError as exc:
            store.finish_run(run_id, status="error", error=str(exc))
            LOG.error("%s", exc)
            store.close()
            raise
        except Exception as exc:
            store.finish_run(run_id, status="error", error=str(exc))
            LOG.exception("sweep failed")
            store.close()
            raise
        status = "error" if result.failed else "ok"
        error = "\n".join(result.errors) if result.errors else None
        store.finish_run(
            run_id,
            status=status,
            files_seen=result.files_seen,
            files_waiting=result.waiting,
            files_processed=result.processed,
            files_failed=result.failed,
            error=error,
        )
        store.close()
        return result


def _require_vault(cfg: Config) -> None:
    if not cfg.vault_root.is_dir() or not (cfg.vault_root / ".obsidian").is_dir():
        raise IngestError(
            f"Obsidian vault is missing (no .obsidian directory): {cfg.vault_root}\n"
            "memo-ingest will not create a vault. Fix paths.vault_root in the config."
        )


def _paths_for_sweep(cfg: Config, only_path: Path | None) -> list[Path]:
    if only_path is not None:
        path = only_path.expanduser()
        if not path.exists():
            raise IngestError(f"file not found: {path}")
        return [path]
    folder = cfg.recordings_dir
    if not folder.exists():
        raise IngestError(
            f"Voice Memos folder does not exist: {folder}\n"
            "Run `memo-ingest setup` and see the paths it checked.\n"
            "Fallback: export one memo from Voice Memos with Share → Save to Files,\n"
            "then `memo-ingest setup --recordings ~/VoiceMemosExport --force`."
        )
    problem = directory_error(folder)
    if problem:
        raise IngestError(unreadable_recordings_message(folder, problem))
    files: list[Path] = []
    for path in folder.iterdir():
        if is_audio_candidate(path):
            files.append(path)
    files.sort(key=lambda item: (item.stat().st_mtime_ns, item.name))
    return files


def _handle_file(
    cfg: Config,
    store: Store,
    path: Path,
    *,
    transcriber,
    dry_run: bool,
    force: bool,
    now: float,
) -> dict:
    if not is_audio_candidate(path):
        return {"status": "ignored", "message": f"ignore {path.name}"}

    try:
        st = path.lstat()
    except OSError as exc:
        return {"status": "failed", "message": f"{path}: {exc}"}

    seen = store.seen(path)
    if (
        not force
        and seen is not None
        and seen["size"] == st.st_size
        and seen["mtime_ns"] == st.st_mtime_ns
        and store.is_processed(seen["sha256"])
    ):
        store.delete_observation(path)
        return {"status": "skipped", "message": f"already processed {path.name}"}

    previous = store.get_observation(path)
    decision = consider(
        path,
        previous,
        now,
        min_bytes=cfg.min_bytes,
        stable_seconds=cfg.stable_seconds,
    )
    if decision.state == "ignore":
        store.delete_observation(path)
        return {"status": "ignored", "message": f"ignore {path.name}: {decision.reason}"}
    if decision.state == "wait" and not force:
        if decision.observation is None:
            store.delete_observation(path)
        else:
            store.upsert_observation(path, decision.observation, now)
        LOG.info("waiting %s (%s)", path.name, decision.reason)
        return {"status": "waiting", "message": f"waiting {path.name}: {decision.reason}"}
    if force and decision.state == "wait" and decision.observation is None:
        return {"status": "failed", "message": f"{path.name}: {decision.reason}"}
    if force and st.st_size < cfg.min_bytes:
        return {
            "status": "failed",
            "message": f"{path.name}: size {st.st_size} is below minimum {cfg.min_bytes}",
        }

    try:
        digest = sha256_file(path)
    except OSError as exc:
        return {"status": "failed", "message": f"{path}: {exc}"}

    if not force and store.is_processed(digest):
        row = store.processed_row(digest)
        note = row["note_path"] if row else ""
        store.remember_seen(str(path), digest, st.st_size, st.st_mtime_ns)
        store.delete_observation(path)
        message = f"skip duplicate bytes {path.name} (note {note})"
        LOG.info(message)
        return {"status": "skipped", "message": message}

    if not force:
        existing = find_note_by_hash(cfg.inbox_dir, digest, store.ignored_note_paths())
        if existing is not None:
            frontmatter = parse_frontmatter(existing.read_text(encoding="utf-8"))
            try:
                duration = float(frontmatter.get("duration_sec") or 0)
            except ValueError:
                duration = 0
            store.mark_processed(
                sha256=digest,
                original_path=str(path),
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                note_path=str(existing),
                model=frontmatter.get("model") or "unknown",
                duration_sec=duration,
                language=frontmatter.get("language") or "",
            )
            store.delete_observation(path)
            message = f"adopted existing note for {path.name}: {existing}"
            LOG.info(message)
            return {"status": "skipped", "message": message, "note": str(existing)}

    if dry_run:
        message = f"dry-run: would transcribe {path}"
        LOG.info(message)
        return {"status": "dry-run", "message": message}

    engine = transcriber if transcriber is not None else build_transcriber(cfg)
    try:
        transcript = engine.transcribe(path)
        probe = probe_audio(path)
        recorded = recorded_at(path, probe)
        created = datetime.now().astimezone()
        title = recording_title(path, probe)
        duration = probe.duration_sec
        if duration is None and transcript.segments:
            duration = transcript.segments[-1][1]
        if cfg.copy_audio:
            copied = place_audio(path, cfg.audio_dir, recorded, digest)
            audio_field = audio_wikilink(cfg.vault_root, copied)
        else:
            audio_field = str(path)
        note_path = unique_path(cfg.inbox_dir / note_basename(recorded, title))
        content = render_note(
            created=created,
            recorded=recorded,
            audio_field=audio_field,
            duration_sec=duration,
            size=st.st_size,
            sha256=digest,
            model=transcript.model_name,
            language=transcript.language or "unknown",
            runtime_sec=transcript.runtime_sec,
            title=title,
            transcript_body=render_transcript(transcript.text, transcript.segments),
        )
        write_note(note_path, content)
        # Mark only after the note is in place. A crash here is recovered by
        # find_note_by_hash on the next run.
        store.mark_processed(
            sha256=digest,
            original_path=str(path),
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            note_path=str(note_path),
            model=transcript.model_name,
            duration_sec=duration,
            language=transcript.language or "",
        )
        store.delete_observation(path)
    except IngestError as exc:
        LOG.error("%s: %s", path.name, exc)
        return {"status": "failed", "message": f"{path.name}: {exc}"}
    except Exception as exc:
        LOG.exception("failed %s", path.name)
        return {"status": "failed", "message": f"{path.name}: {exc}"}

    message = (
        f"wrote {note_path} model={transcript.model_name} "
        f"language={transcript.language} runtime={transcript.runtime_sec:.2f}s"
    )
    LOG.info(message)
    if cfg.summarize_enabled and cfg.summarize_command:
        try:
            summarize_note(note_path, cfg)
            LOG.info("summarized %s", note_path.name)
        except Exception as exc:
            # The transcript is already durable. Summary can be retried.
            LOG.error("summary failed for %s: %s", note_path.name, exc)
            message += f" (summary failed: {exc})"
    return {"status": "processed", "message": message, "note": str(note_path)}


def prepare_reprocess(cfg: Config, store: Store, path: Path) -> str:
    """Drop the processed hash and stop the old note from being adopted."""
    digest = sha256_file(path)
    existing = find_note_by_hash(cfg.inbox_dir, digest, store.ignored_note_paths())
    if existing is not None:
        store.ignore_note(str(existing), digest)
    store.forget_sha(digest)
    store.delete_observation(path)
    return digest


def _tally(result: SweepResult, outcome: dict) -> None:
    status = outcome.get("status")
    message = outcome.get("message") or ""
    if message and status in {"processed", "failed", "dry-run", "waiting"}:
        result.messages.append(message)
    if status == "waiting":
        result.waiting += 1
    elif status == "processed":
        result.processed += 1
        if outcome.get("note"):
            result.notes.append(outcome["note"])
    elif status == "skipped":
        result.skipped += 1
    elif status == "failed":
        result.failed += 1
        result.errors.append(message)
    elif status == "dry-run":
        result.dry_run += 1
