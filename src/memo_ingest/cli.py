"""memo-ingest setup | run | once | status | reprocess | summarize"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from memo_ingest import __version__
from memo_ingest.config import (
    DEFAULT_MODEL,
    Config,
    default_config_path,
    default_log_file,
    default_prompt_file,
    default_state_db,
    load_config,
    write_config,
)
from memo_ingest.discover import (
    choose_recordings,
    choose_vault,
    directory_error,
    find_vaults,
    missing_recordings_message,
    probe_recordings,
    unreadable_recordings_message,
)
from memo_ingest.errors import IngestError
from memo_ingest.store import Store
from memo_ingest.summarize import raw_notes, summarize_note
from memo_ingest.transcribe import ensure_backend, resolve_backend
from memo_ingest.worker import configure_logging, prepare_reprocess, run_sweep

LAUNCHD_LABEL = "com.local.memo-ingest"


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    parser = argparse.ArgumentParser(prog="memo-ingest")
    parser.add_argument("--version", action="version", version=f"memo-ingest {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="detect paths, write config, install launchd")
    _add_config_arg(setup)
    setup.add_argument("--vault", type=Path, default=None)
    setup.add_argument("--recordings", type=Path, default=None)
    setup.add_argument("--force", action="store_true", help="overwrite an existing config")
    setup.add_argument("--no-launchd", action="store_true")
    setup.add_argument("--no-install-whisper", action="store_true")
    setup.set_defaults(func=cmd_setup)

    once = sub.add_parser("once", help="process whatever is waiting, then exit")
    _add_config_arg(once)
    once.add_argument("--dry-run", action="store_true", help="do not transcribe or write notes")
    once.set_defaults(func=cmd_once)

    run = sub.add_parser("run", help="stay running and poll (foreground)")
    _add_config_arg(run)
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="last run, queue, last error")
    _add_config_arg(status)
    status.set_defaults(func=cmd_status)

    reprocess = sub.add_parser("reprocess", help="transcribe one file again")
    _add_config_arg(reprocess)
    reprocess.add_argument("path", type=Path)
    reprocess.add_argument("--dry-run", action="store_true")
    reprocess.set_defaults(func=cmd_reprocess)

    summarize = sub.add_parser("summarize", help="fill Summary on notes that are still raw")
    _add_config_arg(summarize)
    summarize.add_argument("--note", type=Path, default=None, help="one note; default is every raw note")
    summarize.set_defaults(func=cmd_summarize)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except IngestError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="config file (default: ~/.config/memo-ingest/config.toml)",
    )


def cmd_setup(args: argparse.Namespace) -> int:
    path = args.config or default_config_path()
    if path.is_file() and not args.force:
        cfg = load_config(path)
        print(f"config already exists, leaving it in place: {path}")
    else:
        cfg = _build_setup_config(args, path)
        write_config(path, cfg)
        cfg = load_config(path)
        print(f"wrote {path}")
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    cfg.audio_dir.mkdir(parents=True, exist_ok=True)
    _print_discovery(cfg)
    if not args.no_install_whisper:
        backend = ensure_backend(cfg, install=True)
        print(f"transcription backend: {backend} model={cfg.model}")
    else:
        try:
            backend = resolve_backend(cfg)
            print(f"transcription backend: {backend} model={cfg.model}")
        except IngestError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    problem = directory_error(cfg.recordings_dir)
    if args.no_launchd:
        print("launchd not installed (--no-launchd)")
        return 0
    plist = _write_plist(cfg)
    print(f"wrote {plist}")
    if problem:
        print(unreadable_recordings_message(cfg.recordings_dir, problem), file=sys.stderr)
        print("launchd plist is installed but not loaded, because the watch folder is not readable yet.")
        print(f"After granting access: launchctl bootstrap gui/{os.getuid()} {plist}")
        return 1
    _bootstrap(plist)
    print(f"launchd loaded ({LAUNCHD_LABEL}, every {cfg.poll_interval_sec}s)")
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    try:
        result = run_sweep(cfg, dry_run=args.dry_run)
    except IngestError:
        # run_sweep already wrote the message to the log and stderr.
        return 1
    _print_result(result)
    if any(message == "another worker is running" for message in result.messages):
        return 0
    return 1 if result.failed else 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    configure_logging(cfg.log_file)
    print(f"watching {cfg.recordings_dir} every {cfg.poll_interval_sec}s (ctrl-c to stop)")
    while True:
        try:
            result = run_sweep(cfg, dry_run=args.dry_run)
        except IngestError:
            time.sleep(cfg.poll_interval_sec)
            continue
        _print_result(result)
        time.sleep(cfg.poll_interval_sec)


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    problem = directory_error(cfg.recordings_dir)
    print(f"recordings: {cfg.recordings_dir}")
    if problem:
        print(f"  readable: no ({problem})")
    else:
        print("  readable: yes")
    print(f"vault:      {cfg.vault_root}")
    print(f"inbox:      {cfg.inbox_dir}")
    print(f"audio:      {cfg.audio_dir}")
    print(f"state:      {cfg.state_db}")
    print(f"log:        {cfg.log_file}")
    print(f"backend:    {cfg.backend} model={cfg.model} language={cfg.language}")
    print(f"stability:  {cfg.stable_seconds:g}s unchanged, min {cfg.min_bytes} bytes, poll {cfg.poll_interval_sec}s")
    print(f"summarize:  {'on' if cfg.summarize_enabled else 'off'}")
    if not cfg.state_db.is_file():
        print("processed:  0 (no state database yet)")
        print("last run:   never")
        return 0
    store = Store(cfg.state_db)
    try:
        print(f"processed:  {store.processed_count()}")
        print(f"queue:      {store.observation_count()} file(s) seen but not ready")
        last = store.last_run()
        if last is None:
            print("last run:   never")
        else:
            print(
                "last run:   "
                f"{last['started_at']} status={last['status']} "
                f"seen={last['files_seen']} waiting={last['files_waiting']} "
                f"processed={last['files_processed']} failed={last['files_failed']}"
            )
        error = store.last_error()
        print(f"last error: {error or 'none'}")
    finally:
        store.close()
    locked = _lock_held(cfg.lock_path)
    print(f"worker:     {'busy' if locked else 'idle'}")
    return 0


def cmd_reprocess(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    path = args.path.expanduser().resolve()
    if not path.is_file():
        raise IngestError(f"file not found: {path}")
    if path.suffix.lower() != ".m4a":
        raise IngestError(f"reprocess expects an .m4a file: {path}")
    if args.dry_run:
        print(f"dry-run: would reprocess {path}")
    else:
        store = Store(cfg.state_db)
        try:
            digest = prepare_reprocess(cfg, store, path)
        finally:
            store.close()
        print(f"cleared processed state for {digest[:12]}…")
    # force skips the stability wait and the already-processed check.
    # dry-run does not clear the index.
    result = run_sweep(cfg, dry_run=args.dry_run, only_path=path, force=True)
    _print_result(result)
    return 1 if result.failed else 0


def cmd_summarize(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    configure_logging(cfg.log_file)
    if args.note is not None:
        notes = [args.note.expanduser()]
    else:
        notes = raw_notes(cfg.inbox_dir)
    if not notes:
        print("no raw transcript notes")
        return 0
    failed = 0
    for note in notes:
        try:
            summarize_note(note, cfg)
            print(f"summarized {note}")
        except IngestError as exc:
            print(f"{note}: {exc}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def _build_setup_config(args: argparse.Namespace, config_path: Path) -> Config:
    if args.recordings is not None:
        recordings = args.recordings.expanduser()
        if not recordings.is_dir():
            raise IngestError(
                f"Recordings folder does not exist: {recordings}\n"
                "Create it, or export a memo there, then rerun setup. "
                "memo-ingest will not invent a Voice Memos library."
            )
    else:
        probes = probe_recordings()
        print("Voice Memos paths checked:")
        for probe in probes:
            detail = probe.error or "ok"
            print(
                f"  - {probe.path} exists={probe.exists} readable={probe.readable} "
                f"m4a={probe.m4a_count} ({detail})"
            )
        chosen = choose_recordings(probes)
        if chosen is None:
            raise IngestError(missing_recordings_message(probes))
        recordings = chosen.path
        if chosen.m4a_count:
            print(f"using recordings: {recordings} ({chosen.m4a_count} .m4a file(s))")
        elif chosen.readable:
            print(f"using recordings: {recordings} (folder exists, no .m4a files yet)")
        else:
            print(f"using recordings: {recordings} (exists, not readable yet: {chosen.error})")
    vault = choose_vault(find_vaults(), args.vault)
    print(f"vault:      {vault}")
    others = [item for item in find_vaults() if item.resolve() != vault.resolve()]
    if others:
        print("other vaults (pass --vault to use one of these instead):")
        for item in others:
            print(f"  - {item}")
    return Config(
        recordings_dir=recordings,
        vault_root=vault,
        inbox_dir=vault / "inbox" / "transcripts",
        audio_dir=vault / "attachments" / "audio",
        state_db=default_state_db(),
        log_file=default_log_file(),
        poll_interval_sec=45,
        stable_seconds=12,
        min_bytes=10240,
        copy_audio=True,
        backend="auto",
        model=DEFAULT_MODEL,
        language="auto",
        diarize=False,
        whisper_cpp_bin="whisper-cli",
        whisper_cpp_model="",
        summarize_enabled=False,
        summarize_command="",
        prompt_file=default_prompt_file(),
        config_path=config_path,
    )


def _print_discovery(cfg: Config) -> None:
    print(f"inbox:      {cfg.inbox_dir}")
    print(f"audio:      {cfg.audio_dir}")
    print(f"state:      {cfg.state_db}")
    print(f"log:        {cfg.log_file}")
    print(f"machine:    {platform.machine()} ({platform.processor() or 'cpu'})")


def _print_result(result) -> None:
    if not (result.processed or result.failed or result.waiting or result.dry_run or result.messages):
        return
    print(
        f"seen={result.files_seen} waiting={result.waiting} "
        f"processed={result.processed} skipped={result.skipped} "
        f"failed={result.failed} dry_run={result.dry_run}"
    )
    for message in result.messages:
        print(message)


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _memo_ingest_program() -> Path:
    # Do not resolve sys.executable first: the venv python is a symlink into
    # Homebrew, and the console script lives beside the symlink.
    candidates = [Path(sys.executable).parent / "memo-ingest"]
    found = shutil.which("memo-ingest")
    if found:
        candidates.append(Path(found))
    for program in candidates:
        if program.is_file():
            return program
    raise IngestError(
        "Cannot find the memo-ingest executable.\n"
        "Run setup from the project venv: ~/memo-ingest/.venv/bin/memo-ingest setup"
    )


def _write_plist(cfg: Config) -> Path:
    program = _memo_ingest_program()
    path_env = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{program}</string>
    <string>once</string>
  </array>
  <key>StartInterval</key>
  <integer>{cfg.poll_interval_sec}</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>{Path.home()}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>{Path.home()}</string>
    <key>PATH</key>
    <string>{path_env}</string>
    <key>HF_HUB_DISABLE_TELEMETRY</key>
    <string>1</string>
    <key>HF_HUB_DISABLE_PROGRESS_BARS</key>
    <string>1</string>
  </dict>
  <key>StandardOutPath</key>
  <string>{Path.home() / "Library" / "Logs" / "memo-ingest.launchd.out.log"}</string>
  <key>StandardErrorPath</key>
  <string>{Path.home() / "Library" / "Logs" / "memo-ingest.launchd.err.log"}</string>
</dict>
</plist>
"""
    destination = _plist_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(plist, encoding="utf-8")
    destination.chmod(0o644)
    return destination


def _bootstrap(plist: Path) -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"],
        check=False,
        capture_output=True,
        text=True,
    )
    proc = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist)],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise IngestError(
            f"launchctl bootstrap failed: {detail}\n"
            f"Load it yourself with: launchctl bootstrap {domain} {plist}"
        )


def _lock_held(path: Path) -> bool:
    if not path.exists():
        return False
    import fcntl

    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()
