# memo-ingest

When a Voice Memo lands on this Mac, wait until the file has finished downloading, transcribe it locally, and write one Obsidian note. The same recording is never processed twice. The Voice Memos original is never deleted.

```
iPhone Action Button → Voice Memos → iCloud → Mac Voice Memos library
  → memo-ingest (poll + stability gate)
  → mlx-whisper
  → vault/inbox/transcripts/YYYY-MM-DD-HHmm-voice-memo.md
```

Speech-to-text stays on this machine. The only network use is the one-time Whisper weight download. Audio is not uploaded. There is no cloud speech API fallback.

## Where this stands

The pipeline is in use on this Mac. The background checker is a launchd agent. **Memo Ingest** in `~/Applications` starts and stops that agent. Five existing Voice Memos were transcribed into `~/vaults/brain-personal/inbox/transcripts/` with local `mlx-whisper` `large-v3-turbo`.

What works:

- Stable-file gate, so an iCloud download is not transcribed while it is still growing.
- One note per recording, keyed by SHA-256, including after sleep or a crash between the note write and the index update.
- Audio copied into the vault and wikilinked. The Voice Memos original stays where it is.
- Segment timestamps, model name, language, and runtime in YAML frontmatter.
- A separate summarize command, off until a local model command is configured.
- Unit tests plus one live Whisper test.

What is not done:

- Note titles currently use the raw Voice Memos filename (`# 20250525 062225-112164E2`) instead of the date and time. See `AGENTS.md`.
- Summary and action items stay empty. `status` stays `raw`.
- No speaker names.
- No chat app over the vault.

`AGENTS.md` is the longer note for whoever changes this next: paths on this Mac, Full Disk Access, and the invariants that are easy to break.

## Confirm iCloud Voice Memos is on

1. iPhone: Settings → [your name] → iCloud → Voice Memos → On.
2. Mac: System Settings → [your name] → iCloud → Voice Memos → On.
3. Open the Voice Memos app on the Mac once so it creates the library and can download recordings.

The watch folder on current macOS is:

`~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings`

`memo-ingest setup` checks that path first, then the two older Application Support locations, and records the one that exists. If none of them exist, setup stops and lists the paths it checked.

Reading that folder needs Full Disk Access:

System Settings → Privacy & Security → Full Disk Access → add the resolved path of `~/memo-ingest/.venv/bin/python3` (setup prints it). Add Terminal as well if you run the command from a shell.

Fallback, if sync never produces files there: open Voice Memos, select a memo, Share → Save to Files, then:

```bash
memo-ingest setup --recordings ~/VoiceMemosExport --force
```

## Setup

```bash
cd ~/memo-ingest
python3 -m venv .venv
.venv/bin/pip install -e ".[mlx]"
.venv/bin/memo-ingest setup
```

Setup writes `~/.config/memo-ingest/config.toml`, creates `{vault}/inbox/transcripts/` and `{vault}/attachments/audio/`, installs mlx-whisper if it is missing, and loads a launchd agent that runs `memo-ingest once` every 45 seconds.

On this Mac the vault default is `~/vaults/brain-personal` (the personal vault, not brain-work). To use another vault that already exists:

```bash
.venv/bin/memo-ingest setup --vault ~/vaults/brain-work --force
```

Setup will not create a vault.

## Start and stop

Double-click **Memo Ingest** in `~/Applications`. That starts the background checker. Closing the window leaves it running, including after a reboot. **Stop** in the window turns it off. Rebuild the app with `macos/build-app.sh` after you change the Swift source.

The command is `~/memo-ingest/.venv/bin/memo-ingest`. launchd is the normal runner. `run` is the same loop in the foreground.

```bash
# stop
launchctl bootout gui/$(id -u)/com.local.memo-ingest

# start, after Full Disk Access is granted
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.memo-ingest.plist

# foreground, for debugging
~/memo-ingest/.venv/bin/memo-ingest run
```

Ctrl-C stops `run`. One worker holds a lock, so a long transcription is not started twice when the next 45-second tick arrives.

```bash
~/memo-ingest/.venv/bin/memo-ingest once            # process the queue, then exit
~/memo-ingest/.venv/bin/memo-ingest once --dry-run  # report stable files; do not transcribe or write
~/memo-ingest/.venv/bin/memo-ingest status          # last run, queue depth, last error
```

Every command takes `--config PATH`. The default is `~/.config/memo-ingest/config.toml`.

Logs: `~/Library/Logs/memo-ingest.log`. launchd stdout/stderr: `~/Library/Logs/memo-ingest.launchd.out.log` and `memo-ingest.launchd.err.log`. State: `~/.local/share/memo-ingest/state.sqlite`.

## What gets written

`{vault}/inbox/transcripts/2026-09-23-1559-voice-memo.md`

The note has YAML frontmatter (`type: transcript`, `status: raw`), the transcript with segment timestamps, and empty `## Summary` and `## Action items` sections. The transcript is not edited later. A copied audio file is `{vault}/attachments/audio/YYYY/YYYY-MM-DD-HHmm.m4a`, wikilinked from the note. A real title from metadata is used when it is a human title. A plain Voice Memos filename is still being used as the title too; that is the known gap described above.

`status: raw` means transcribed only. `status: processed` is set only by the optional summarize step.

## Change the Whisper model

Edit `~/.config/memo-ingest/config.toml`:

```toml
[whisper]
backend = "auto"   # or mlx-whisper, or whisper.cpp
model = "mlx-community/whisper-large-v3-turbo"
language = "auto"  # or en
```

`auto` on Apple Silicon uses mlx-whisper. A lighter model is `mlx-community/whisper-small`. Already processed recordings stay done. Weights download on first use into the Hugging Face cache.

whisper.cpp, if you want it instead:

```bash
brew install whisper-cpp
```

```toml
backend = "whisper.cpp"
whisper_cpp_bin = "whisper-cli"
whisper_cpp_model = "/absolute/path/ggml-large-v3-turbo.bin"
```

Speaker diarization (`diarize`) is accepted in config and does nothing in v1.

## Reprocess one file

The processed index is the SHA-256 of the audio bytes. Renaming a file does not transcribe it again. Deleting the note is not enough.

```bash
~/memo-ingest/.venv/bin/memo-ingest reprocess "/path/to/recording.m4a"
```

That drops the hash, leaves the old note where it is, and writes a new note. `reprocess --dry-run` does not change the index.

## Summaries

Summarizing is a separate command so it can be retried without running Whisper again. It is off until you set a local command. The prompt is `prompts/summarize.md` (edit that file; no code change).

```toml
[summarize]
enabled = true
command = "ollama run llama3.2"
prompt_file = "/Users/you/memo-ingest/prompts/summarize.md"
```

The command receives the prompt plus the transcript on stdin. Stdout must contain `## Summary` and `## Action items`. Then:

```bash
~/memo-ingest/.venv/bin/memo-ingest summarize
```

That fills those two sections and sets `status: processed`.

## Asking questions about the notes

This project does not include a chat app. The notes are plain markdown in the vault you already use:

- Obsidian search for `type: transcript`, or open `inbox/transcripts`.
- Obsidian Copilot, Smart Connections, or any plugin pointed at this vault.
- Claude, ChatGPT, or another tool that can read a local folder, pointed at `inbox/transcripts` or the whole vault.

## Stability rules

A file is transcribed only when all of these are true:

- the name ends in `.m4a` (`.waveform`, `.db`, `.icloud`, `*_SUPPORT*`, and hidden files are ignored)
- it is a regular file, not an iCloud dataless placeholder
- size is at least `min_bytes` (default 10 KB)
- size and mtime have been unchanged for `stable_seconds` (default 12) across two checks
- its SHA-256 is not already in the state database

The first time a file is seen it is never ready. Each launchd run and each `memo-ingest once` scans the whole folder, so a memo that arrived while the Mac was asleep is still picked up. If transcription works and writing the note fails, the file is not marked done. If the note is written and the process dies before the index update, the next run adopts that note instead of writing a second one.

## Tests

```bash
cd ~/memo-ingest
.venv/bin/python -m unittest discover -s tests -t . -v
```

The tests use a fake transcriber and a temporary vault. They check the stability gate (including a file that grows, the way iCloud does), valid frontmatter, no duplicate note on a second run, and crash recovery. A live mlx-whisper run is separate:

```bash
.venv/bin/python -m unittest tests.test_live -v
```
