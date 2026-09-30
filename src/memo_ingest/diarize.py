"""Local speaker diarization via sherpa-onnx. Runs after STT; audio stays on this Mac."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from memo_ingest.config import Config
from memo_ingest.errors import IngestError
from memo_ingest.transcribe import Segment, Transcript

LOG = logging.getLogger("memo_ingest")

SEG_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
EMB_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx"
)
SEG_DIR_NAME = "sherpa-onnx-pyannote-segmentation-3-0"
SEG_ONNX_NAME = "model.onnx"
EMB_ONNX_NAME = "wespeaker_en_voxceleb_resnet34_LM.onnx"


@dataclass(frozen=True)
class DiarizeTurn:
    start: float
    end: float
    speaker_id: str


def default_models_dir() -> Path:
    return Path.home() / ".cache" / "memo-ingest" / "diarize"


def models_dir_for(cfg: Config) -> Path:
    if cfg.diarize_models_dir:
        return Path(cfg.diarize_models_dir).expanduser()
    return default_models_dir()


def segmentation_path(models_dir: Path) -> Path:
    return models_dir / SEG_DIR_NAME / SEG_ONNX_NAME


def embedding_path(models_dir: Path) -> Path:
    return models_dir / EMB_ONNX_NAME


def models_ready(models_dir: Path) -> bool:
    return segmentation_path(models_dir).is_file() and embedding_path(models_dir).is_file()


def sherpa_importable() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:
        return False
    return True


def install_instructions() -> str:
    return (
        "Speaker diarization needs the optional diarize extra and local models.\n"
        "\n"
        "  cd ~/memo-ingest\n"
        "  .venv/bin/pip install -e \".[diarize]\"\n"
        "  .venv/bin/memo-ingest ensure-diarize-models\n"
        "\n"
        "Then set whisper.diarize = true in ~/.config/memo-ingest/config.toml.\n"
        "Audio is never uploaded; models come from GitHub releases (not the gated HF path)."
    )


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    LOG.info("downloading %s → %s", url, destination)
    try:
        urllib.request.urlretrieve(url, temporary)
        temporary.replace(destination)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def ensure_diarize_models(models_dir: Path | None = None, *, force: bool = False) -> Path:
    """Download pyannote-seg onnx + wespeaker EN into the cache if missing."""
    target = models_dir or default_models_dir()
    target.mkdir(parents=True, exist_ok=True)
    seg = segmentation_path(target)
    emb = embedding_path(target)
    if force or not seg.is_file():
        archive = target / "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
        _download(SEG_URL, archive)
        with tarfile.open(archive, "r:bz2") as handle:
            handle.extractall(path=target)
        archive.unlink(missing_ok=True)
        if not seg.is_file():
            raise IngestError(f"segmentation model missing after extract: {seg}")
    if force or not emb.is_file():
        _download(EMB_URL, emb)
        if not emb.is_file():
            raise IngestError(f"embedding model missing after download: {emb}")
    return target


def _load_mono_16k(path: Path) -> tuple["object", int]:
    """Decode any audio file to float32 mono at 16 kHz via ffmpeg. Returns (numpy array, 16000)."""
    import numpy as np

    if shutil.which("ffmpeg") is None:
        raise IngestError("ffmpeg is required for diarization and was not found on PATH.")
    with tempfile.TemporaryDirectory(prefix="memo-diarize-") as temporary:
        wav = Path(temporary) / "audio.wav"
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
            raise IngestError(f"ffmpeg failed to decode {path.name} for diarization: {tail}")
        import wave

        with wave.open(str(wav), "rb") as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                raise IngestError(f"unexpected wav format from ffmpeg for {path.name}")
            frames = handle.readframes(handle.getnframes())
            sample_rate = handle.getframerate()
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if sample_rate != 16000:
            raise IngestError(f"expected 16 kHz after ffmpeg, got {sample_rate}")
        return samples, 16000


def _build_diarizer(cfg: Config, models_dir: Path):
    import sherpa_onnx

    num_clusters = cfg.diarize_max_speakers if cfg.diarize_max_speakers > 0 else -1
    pyannote_kwargs: dict = {"model": str(segmentation_path(models_dir))}
    # window_shift_ratio exists on newer sherpa-onnx; omit if the ctor rejects it.
    try:
        pyannote = sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
            **pyannote_kwargs, window_shift_ratio=0.1
        )
    except TypeError:
        pyannote = sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(**pyannote_kwargs)
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(pyannote=pyannote),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(embedding_path(models_dir))
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_clusters,
            threshold=cfg.diarize_threshold,
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise IngestError(
            "sherpa-onnx diarization config is invalid; check model paths under "
            f"{models_dir}"
        )
    return sherpa_onnx.OfflineSpeakerDiarization(config)


def diarize_file(path: Path, cfg: Config) -> list[DiarizeTurn]:
    """Return SPEAKER_XX turns for a local audio file. Does not upload audio."""
    if not sherpa_importable():
        raise IngestError(install_instructions())
    models = models_dir_for(cfg)
    if not models_ready(models):
        ensure_diarize_models(models)
    samples, sample_rate = _load_mono_16k(path)
    diarizer = _build_diarizer(cfg, models)
    if sample_rate != diarizer.sample_rate:
        raise IngestError(
            f"diarizer sample rate {diarizer.sample_rate} does not match decoded {sample_rate}"
        )
    result = diarizer.process(samples).sort_by_start_time()
    turns: list[DiarizeTurn] = []
    for item in result:
        turns.append(
            DiarizeTurn(
                start=float(item.start),
                end=float(item.end),
                speaker_id=f"SPEAKER_{int(item.speaker):02d}",
            )
        )
    return turns


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def assign_speakers(segments: list[Segment], turns: list[DiarizeTurn]) -> list[Segment]:
    """Label each Whisper segment with the diarization turn that overlaps it most."""
    if not turns:
        return list(segments)
    labeled: list[Segment] = []
    for segment in segments:
        best_id: str | None = None
        best_overlap = 0.0
        for turn in turns:
            amount = _overlap(segment.start, segment.end, turn.start, turn.end)
            if amount > best_overlap:
                best_overlap = amount
                best_id = turn.speaker_id
        if best_id is None:
            # Fall back to nearest turn by midpoint if no overlap (short Whisper cuts).
            mid = (segment.start + segment.end) / 2.0
            nearest = min(turns, key=lambda turn: abs((turn.start + turn.end) / 2.0 - mid))
            best_id = nearest.speaker_id
        labeled.append(
            Segment(
                start=segment.start,
                end=segment.end,
                text=segment.text,
                speaker_id=best_id,
            )
        )
    return labeled


def apply_diarization(path: Path, transcript: Transcript, cfg: Config) -> Transcript:
    """Attach speaker_id to transcript segments. Raises on hard failure."""
    turns = diarize_file(path, cfg)
    segments = assign_speakers(list(transcript.segments), turns)
    return Transcript(
        text=transcript.text,
        segments=segments,
        language=transcript.language,
        model_name=transcript.model_name,
        runtime_sec=transcript.runtime_sec,
    )


def maybe_diarize(path: Path, transcript: Transcript, cfg: Config) -> Transcript:
    """Run diarization when enabled. fail_soft leaves the transcript unlabeled."""
    if not cfg.diarize:
        return transcript
    try:
        return apply_diarization(path, transcript, cfg)
    except Exception as exc:
        if cfg.diarize_fail_soft:
            LOG.error("diarization failed (fail_soft): %s", exc)
            return transcript
        raise
