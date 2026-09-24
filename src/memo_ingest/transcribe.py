"""Local speech-to-text. Audio is never sent to a cloud API."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Model weights may download once. Do not print a progress bar into launchd logs.
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

from memo_ingest.config import Config
from memo_ingest.errors import IngestError


@dataclass(frozen=True)
class Transcript:
    text: str
    segments: list[tuple[float, float, str]]
    language: str
    model_name: str
    runtime_sec: float


def model_label(backend: str, model: str) -> str:
    name = model.rstrip("/").split("/")[-1]
    name = name.removesuffix(".bin")
    for prefix in ("ggml-", "whisper-"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    if backend == "mlx-whisper":
        return f"mlx-whisper-{name}"
    if backend == "whisper.cpp":
        return f"whisper.cpp-{name}"
    return f"{backend}-{name}"


def install_instructions() -> str:
    return (
        "Local Whisper is not available. memo-ingest will not fall back to a cloud API.\n"
        "\n"
        "Apple Silicon (this project's default):\n"
        "  cd ~/memo-ingest\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/pip install -e \".[mlx]\"\n"
        "\n"
        "Or, inside the project venv:\n"
        "  .venv/bin/pip install mlx-whisper\n"
        "\n"
        "whisper.cpp fallback (Intel, or if you prefer it):\n"
        "  brew install whisper-cpp\n"
        "  # download a ggml model, then set in ~/.config/memo-ingest/config.toml:\n"
        "  # backend = \"whisper.cpp\"\n"
        "  # whisper_cpp_bin = \"whisper-cli\"\n"
        "  # whisper_cpp_model = \"/absolute/path/ggml-large-v3-turbo.bin\"\n"
    )


def mlx_importable() -> bool:
    try:
        import mlx_whisper  # noqa: F401
    except Exception:
        return False
    return True


def whisper_cpp_ready(cfg: Config) -> bool:
    if not cfg.whisper_cpp_model:
        return False
    if not Path(cfg.whisper_cpp_model).expanduser().is_file():
        return False
    return _which_bin(cfg.whisper_cpp_bin) is not None


def _which_bin(name: str) -> str | None:
    path = Path(name).expanduser()
    if path.is_file():
        return str(path)
    return shutil.which(name)


def resolve_backend(cfg: Config) -> str:
    """Pick mlx-whisper or whisper.cpp. Raise if neither is actually installed."""
    requested = cfg.backend
    arm = platform.machine().lower() in {"arm64", "aarch64"}
    if requested == "mlx-whisper":
        if mlx_importable():
            return "mlx-whisper"
        raise IngestError(install_instructions())
    if requested == "whisper.cpp":
        if whisper_cpp_ready(cfg):
            return "whisper.cpp"
        raise IngestError(
            "whisper.cpp is selected but the binary or model file is missing.\n"
            + install_instructions()
        )
    # auto
    if arm and mlx_importable():
        return "mlx-whisper"
    if whisper_cpp_ready(cfg):
        return "whisper.cpp"
    if mlx_importable():
        return "mlx-whisper"
    raise IngestError(install_instructions())


def install_mlx_whisper() -> None:
    print("Installing mlx-whisper into this Python...", flush=True)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "mlx-whisper"])


def ensure_backend(cfg: Config, *, install: bool) -> str:
    if cfg.backend == "whisper.cpp":
        return resolve_backend(cfg)
    arm = platform.machine().lower() in {"arm64", "aarch64"}
    want_mlx = cfg.backend == "mlx-whisper" or (cfg.backend == "auto" and arm)
    if want_mlx and not mlx_importable():
        if not install:
            return resolve_backend(cfg)
        if not arm and cfg.backend != "mlx-whisper":
            return resolve_backend(cfg)
        install_mlx_whisper()
    return resolve_backend(cfg)


class MlxWhisperTranscriber:
    def __init__(self, model: str, language: str):
        self.model = model
        self.language = language
        self.model_name = model_label("mlx-whisper", model)

    def transcribe(self, path: Path) -> Transcript:
        try:
            import mlx_whisper
        except Exception as exc:
            raise IngestError(install_instructions()) from exc
        # verbose=None hides both the tqdm bar and the "Detected language" line.
        kwargs: dict = {"path_or_hf_repo": self.model}
        if self.language and self.language != "auto":
            kwargs["language"] = self.language
        started = time.perf_counter()
        try:
            result = mlx_whisper.transcribe(str(path), **kwargs)
        except Exception as exc:
            raise IngestError(
                f"mlx-whisper failed: {exc}\n"
                "The audio file was not uploaded. On first use the model weights "
                f"download from Hugging Face ({self.model}) into the local cache. "
                "Retry when that download can finish."
            ) from exc
        runtime = time.perf_counter() - started
        return _from_whisper_dict(result, self.model_name, runtime)


class WhisperCppTranscriber:
    def __init__(self, binary: str, model: str, language: str):
        resolved = _which_bin(binary)
        if resolved is None:
            raise IngestError(install_instructions())
        self.binary = resolved
        self.model = str(Path(model).expanduser())
        self.language = language
        self.model_name = model_label("whisper.cpp", model)

    def transcribe(self, path: Path) -> Transcript:
        if shutil.which("ffmpeg") is None:
            raise IngestError("ffmpeg is required to decode audio for whisper.cpp and was not found on PATH.")
        with tempfile.TemporaryDirectory(prefix="memo-ingest-") as temporary:
            wav = Path(temporary) / "audio.wav"
            outbase = Path(temporary) / "out"
            decode = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(path),
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-c:a",
                    "pcm_s16le",
                    str(wav),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if decode.returncode != 0:
                tail = (decode.stderr or "")[-500:]
                raise IngestError(f"ffmpeg failed to decode {path.name}: {tail}")
            command = [
                self.binary,
                "-m",
                self.model,
                "-f",
                str(wav),
                "-oj",
                "-of",
                str(outbase),
            ]
            if self.language and self.language != "auto":
                command.extend(["-l", self.language])
            started = time.perf_counter()
            proc = subprocess.run(command, check=False, capture_output=True, text=True)
            runtime = time.perf_counter() - started
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "")[-800:]
                raise IngestError(f"whisper.cpp failed: {tail}")
            json_path = Path(str(outbase) + ".json")
            if not json_path.is_file():
                raise IngestError(f"whisper.cpp did not write {json_path.name}")
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise IngestError(f"whisper.cpp JSON was unreadable: {exc}") from exc
        return _from_whisper_cpp(payload, self.model_name, runtime)


def build_transcriber(cfg: Config):
    backend = resolve_backend(cfg)
    if cfg.diarize:
        # v1 keeps the flag so config does not have to change later.
        # Diarization is intentionally not run.
        print("speaker diarization is off in v1; continuing without speakers", flush=True)
    if backend == "mlx-whisper":
        return MlxWhisperTranscriber(cfg.model, cfg.language)
    return WhisperCppTranscriber(cfg.whisper_cpp_bin, cfg.whisper_cpp_model, cfg.language)


def _from_whisper_dict(result: dict, model_name: str, runtime: float) -> Transcript:
    segments: list[tuple[float, float, str]] = []
    for segment in result.get("segments") or []:
        try:
            start = float(segment.get("start", 0))
            end = float(segment.get("end", start))
        except (TypeError, ValueError):
            continue
        segments.append((start, end, str(segment.get("text") or "")))
    language = str(result.get("language") or "unknown")
    text = str(result.get("text") or "")
    return Transcript(text=text, segments=segments, language=language, model_name=model_name, runtime_sec=runtime)


def _from_whisper_cpp(payload: dict, model_name: str, runtime: float) -> Transcript:
    if "segments" in payload and "text" in payload:
        return _from_whisper_dict(payload, model_name, runtime)
    language = "unknown"
    result = payload.get("result") or {}
    if isinstance(result, dict) and result.get("language"):
        language = str(result["language"])
    segments: list[tuple[float, float, str]] = []
    chunks = payload.get("transcription") or []
    for chunk in chunks:
        offsets = chunk.get("offsets") or {}
        try:
            start = float(offsets.get("from", 0)) / 1000.0
            end = float(offsets.get("to", 0)) / 1000.0
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        segments.append((start, end, str(chunk.get("text") or "")))
    text = " ".join(part[2].strip() for part in segments if part[2].strip())
    return Transcript(text=text, segments=segments, language=language, model_name=model_name, runtime_sec=runtime)
