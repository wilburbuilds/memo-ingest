import tempfile
from pathlib import Path

from memo_ingest.config import DEFAULT_MODEL, Config
from memo_ingest.transcribe import Segment, Transcript


def make_config(root: Path | None = None) -> tuple[Path, Config]:
    tmp = root or Path(tempfile.mkdtemp(prefix="memo-ingest-"))
    vault = tmp / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    recordings = tmp / "recordings"
    recordings.mkdir()
    prompt = tmp / "prompts" / "summarize.md"
    prompt.parent.mkdir()
    prompt.write_text("Summarize the transcript.\n", encoding="utf-8")
    name_prompt = tmp / "prompts" / "name-speakers.md"
    name_prompt.write_text("Name the speakers.\n", encoding="utf-8")
    cfg = Config(
        recordings_dir=recordings,
        vault_root=vault,
        inbox_dir=vault / "inbox" / "transcripts",
        audio_dir=vault / "attachments" / "audio",
        state_db=tmp / "state.sqlite",
        log_file=tmp / "memo-ingest.log",
        poll_interval_sec=45,
        stable_seconds=12,
        min_bytes=10240,
        copy_audio=True,
        backend="mlx-whisper",
        model=DEFAULT_MODEL,
        language="auto",
        diarize=False,
        whisper_cpp_bin="whisper-cli",
        whisper_cpp_model="",
        diarize_models_dir="",
        diarize_threshold=0.5,
        diarize_max_speakers=0,
        diarize_fail_soft=True,
        summarize_enabled=False,
        summarize_command="",
        prompt_file=prompt,
        name_speakers_enabled=False,
        name_speakers_command="",
        name_speakers_prompt_file=tmp / "prompts" / "name-speakers.md",
        config_path=None,
    )
    return tmp, cfg


class FakeTranscriber:
    def __init__(self, text: str = "Hello from the voice memo."):
        self.text = text
        self.calls = 0
        self.model_name = "mlx-whisper-test"
        self.paths: list[Path] = []

    def transcribe(self, path: Path) -> Transcript:
        self.calls += 1
        self.paths.append(path)
        return Transcript(
            text=self.text,
            segments=[Segment(start=0.0, end=1.25, text=self.text)],
            language="en",
            model_name=self.model_name,
            runtime_sec=0.05,
        )
