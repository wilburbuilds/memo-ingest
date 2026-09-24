"""Load and write ~/.config/memo-ingest/config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from memo_ingest.errors import IngestError

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"


def default_config_path() -> Path:
    return Path.home() / ".config" / "memo-ingest" / "config.toml"


def default_state_db() -> Path:
    return Path.home() / ".local" / "share" / "memo-ingest" / "state.sqlite"


def default_log_file() -> Path:
    return Path.home() / "Library" / "Logs" / "memo-ingest.log"


def repo_root() -> Path:
    # src/memo_ingest/config.py → repo root
    return Path(__file__).resolve().parents[2]


def default_prompt_file() -> Path:
    return repo_root() / "prompts" / "summarize.md"


@dataclass(frozen=True)
class Config:
    recordings_dir: Path
    vault_root: Path
    inbox_dir: Path
    audio_dir: Path
    state_db: Path
    log_file: Path
    poll_interval_sec: int
    stable_seconds: float
    min_bytes: int
    copy_audio: bool
    backend: str
    model: str
    language: str
    diarize: bool
    whisper_cpp_bin: str
    whisper_cpp_model: str
    summarize_enabled: bool
    summarize_command: str
    prompt_file: Path
    config_path: Path | None = None

    @property
    def lock_path(self) -> Path:
        return self.state_db.parent / "worker.lock"


def _as_path(value: object, default: Path) -> Path:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    return Path(text).expanduser()


def load_config(path: Path | None = None) -> Config:
    path = path or default_config_path()
    if not path.is_file():
        raise IngestError(f"Config not found: {path}\nRun: memo-ingest setup")
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    paths = data.get("paths") or {}
    watch = data.get("watch") or {}
    whisper = data.get("whisper") or {}
    summarize = data.get("summarize") or {}
    if not isinstance(paths, dict) or not isinstance(watch, dict):
        raise IngestError(f"{path}: expected [paths] and [watch] tables")
    if not isinstance(whisper, dict) or not isinstance(summarize, dict):
        raise IngestError(f"{path}: expected [whisper] and [summarize] tables")

    vault_raw = paths.get("vault_root")
    recordings_raw = paths.get("recordings_dir")
    if not vault_raw or not str(vault_raw).strip():
        raise IngestError(f"{path}: paths.vault_root is required")
    if not recordings_raw or not str(recordings_raw).strip():
        raise IngestError(f"{path}: paths.recordings_dir is required")
    vault = Path(str(vault_raw)).expanduser()
    recordings = Path(str(recordings_raw)).expanduser()

    stable = float(watch.get("stable_seconds", 12))
    interval = int(watch.get("poll_interval_sec", 45))
    min_bytes = int(watch.get("min_bytes", 10240))
    if stable < 1:
        raise IngestError("watch.stable_seconds must be at least 1")
    if interval < 15:
        raise IngestError("watch.poll_interval_sec must be at least 15")
    if min_bytes < 1:
        raise IngestError("watch.min_bytes must be at least 1")

    backend = str(whisper.get("backend", "auto")).strip() or "auto"
    if backend not in {"auto", "mlx-whisper", "whisper.cpp"}:
        raise IngestError(
            "whisper.backend must be auto, mlx-whisper, or whisper.cpp"
        )

    cfg = Config(
        recordings_dir=recordings,
        vault_root=vault,
        inbox_dir=_as_path(paths.get("inbox_dir"), vault / "inbox" / "transcripts"),
        audio_dir=_as_path(paths.get("audio_dir"), vault / "attachments" / "audio"),
        state_db=_as_path(paths.get("state_db"), default_state_db()),
        log_file=_as_path(paths.get("log_file"), default_log_file()),
        poll_interval_sec=interval,
        stable_seconds=stable,
        min_bytes=min_bytes,
        copy_audio=bool(watch.get("copy_audio", True)),
        backend=backend,
        model=str(whisper.get("model", DEFAULT_MODEL)).strip() or DEFAULT_MODEL,
        language=str(whisper.get("language", "auto")).strip() or "auto",
        diarize=bool(whisper.get("diarize", False)),
        whisper_cpp_bin=str(whisper.get("whisper_cpp_bin", "whisper-cli")).strip()
        or "whisper-cli",
        whisper_cpp_model=str(whisper.get("whisper_cpp_model", "")).strip(),
        summarize_enabled=bool(summarize.get("enabled", False)),
        summarize_command=str(summarize.get("command", "")).strip(),
        prompt_file=_as_path(summarize.get("prompt_file"), default_prompt_file()),
        config_path=path,
    )
    _check_layout(cfg)
    return cfg


def _check_layout(cfg: Config) -> None:
    """Inbox and copied audio must live inside the vault so notes stay portable."""
    if not cfg.copy_audio:
        return
    vault = cfg.vault_root.resolve()
    for label, folder in (("inbox_dir", cfg.inbox_dir), ("audio_dir", cfg.audio_dir)):
        try:
            folder.resolve().relative_to(vault)
        except ValueError as exc:
            raise IngestError(f"{label} must be inside the vault ({vault})") from exc


def write_config(path: Path, cfg: Config) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prompt = str(cfg.prompt_file)
    text = f"""# memo-ingest. Written by `memo-ingest setup`.
# Audio stays on this Mac. Speech-to-text does not use a cloud API.

[paths]
recordings_dir = {_toml_str(cfg.recordings_dir)}
vault_root = {_toml_str(cfg.vault_root)}
inbox_dir = {_toml_str(cfg.inbox_dir)}
audio_dir = {_toml_str(cfg.audio_dir)}
state_db = {_toml_str(cfg.state_db)}
log_file = {_toml_str(cfg.log_file)}

[watch]
poll_interval_sec = {cfg.poll_interval_sec}
stable_seconds = {cfg.stable_seconds:g}
min_bytes = {cfg.min_bytes}
copy_audio = {_toml_bool(cfg.copy_audio)}

[whisper]
backend = {_toml_str(cfg.backend)}
model = {_toml_str(cfg.model)}
language = {_toml_str(cfg.language)}
diarize = {_toml_bool(cfg.diarize)}
whisper_cpp_bin = {_toml_str(cfg.whisper_cpp_bin)}
whisper_cpp_model = {_toml_str(cfg.whisper_cpp_model)}

[summarize]
enabled = {_toml_bool(cfg.summarize_enabled)}
command = {_toml_str(cfg.summarize_command)}
prompt_file = {_toml_str(prompt)}
"""
    path.write_text(text, encoding="utf-8")


def _toml_str(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
