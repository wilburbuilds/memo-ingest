# Notes for a future agent

Read this before changing memo-ingest. The human-facing setup guide is `README.md`. This file is the operational picture as of 2026-09-30 (diarize + name-speakers added).

## What this product is

A local macOS pipeline. An iPhone Action Button records into the Voice Memos app. iCloud syncs the `.m4a` onto this Mac. A launchd job notices the file only after it has finished downloading, transcribes it with local Whisper, and writes one Obsidian note. The same bytes are never transcribed twice. The Voice Memos original is never deleted.

There is no iOS app, no Shortcut, and no cloud speech API. Do not add one as a fallback.

## This machine

- Repo: `~/memo-ingest`
- Apple M4, macOS 26. Python 3.14 venv at `~/memo-ingest/.venv` (`mlx-whisper` installed there).
- Whisper model: `mlx-community/whisper-large-v3-turbo`, cached under `~/.cache/huggingface`. Weights download once. Audio is never uploaded.
- Vault: `~/vaults/brain-personal`. Notes go to `inbox/transcripts/`. Audio copies go to `attachments/audio/YYYY/`. `brain-work` is the business vault. Do not switch vaults unless the human asks. Do not create a vault. Do not commit the vault or push it to GitHub. The personal vault's own rule is that vault contents stay off third-party hosts.
- Config (not in git): `~/.config/memo-ingest/config.toml`
- State (not in git): `~/.local/share/memo-ingest/state.sqlite`
- Log: `~/Library/Logs/memo-ingest.log`
- LaunchAgent label: `com.local.memo-ingest`
- Plist: `~/Library/LaunchAgents/com.local.memo-ingest.plist`
- It runs `~/memo-ingest/.venv/bin/memo-ingest once` every 45 seconds. One flock at `~/.local/share/memo-ingest/worker.lock`. A long transcription is not started twice.
- Clickable control: `~/Applications/Memo Ingest.app` (source `macos/`, rebuild with `macos/build-app.sh`). The app loads and unloads the agent and can toggle diarize / name-speakers / summarize in `~/.config/memo-ingest/config.toml`. It does not transcribe. Closing the window leaves the agent running.
- Recordings directory that exists: `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings`. The two older Application Support paths are absent. The folder is not readable without Full Disk Access.

## Full Disk Access

Granted to the resolved Homebrew binary, not to Terminal and not to Memo Ingest.app:

`/opt/homebrew/Cellar/python@3.14/3.14.6/Frameworks/Python.framework/Versions/3.14/bin/python3.14`

The venv `python3.14` is a symlink to that binary. The launchd job can list Voice Memos. A shell or a child process whose responsible app is Terminal or Memo Ingest.app often still gets `Operation not permitted`. Judge the agent by its log and by notes in the inbox, not by `ls` from an unprivileged shell.

A Homebrew Python upgrade changes the Cellar path. Full Disk Access must be granted again or the agent goes blind. Do not relocate the venv casually.

## Behavior that is easy to break

- The first time a file is seen it is never ready. iCloud creates a tiny or growing file. Size and mtime must match across two observations at least `stable_seconds` apart (default 12). Observations live in SQLite because each launchd tick is a new process. An in-memory timer will not survive the next tick.
- Ignore non-`.m4a`, hidden files, `.icloud`, `.waveform`, sqlite sidecars, `*_SUPPORT*`, symlinks, and dataless files (`SF_DATALESS`, `0x40000000`). Default minimum size is 10 KB, so a very short memo can be skipped.
- Hash the file only after it is stable. The processed key is SHA-256 of the bytes. A rename of the same bytes is a skip.
- Write the note, then mark processed. If the note write fails, do not mark it. If the process dies after the write and before the mark, the next run finds `audio_sha256` in the inbox and adopts that note instead of writing a second one.
- `reprocess` records the old note in `ignore_notes`, forgets the hash, and writes a new note. The old transcript stays put. Deleting only the note does not cause a re-transcription.
- Copy audio into the vault. Do not hardlink it out of Voice Memos, and do not delete the original.
- Summarize is a separate command (`memo-ingest summarize`). It may replace `## Summary` and `## Action items` and set `status: processed`. It must not change the transcript section. Prompt: `prompts/summarize.md`. After a successful write, fail-soft local notify (`[summarize] notify`, default true) plus append to `~/.local/share/memo-ingest/summarize-events.jsonl`. Do not add Reminders from summarize; Grok handles that in a routine.
- Diarization is optional local sherpa-onnx **after** mlx-whisper (`whisper.diarize`). Labels are `SPEAKER_00` etc. Models live in `~/.cache/memo-ingest/diarize/` (GitHub releases: pyannote-seg onnx + wespeaker EN). No pyannote HF gated path. Audio is never uploaded for diarization. `diarize.fail_soft` (default true) continues without speakers if models/sherpa are missing.
- Speaker naming is a separate optional Grok Build pass (`memo-ingest name-speakers`, `scripts/grok-name-speakers.sh`, `prompts/name-speakers.md`). It rewrites `SPEAKER_XX` labels in the transcript body to inferred names and stores `speaker_names` in frontmatter so summarize sees real names. Text only; no audio upload. May run automatically after note write when `[name_speakers] enabled = true`, before summarize.
- Do not rewrite existing vault notes for diarization/naming unless the user runs `reprocess` (new note) or `name-speakers` on a diarized note.
- `clean_title` rejects Voice Memos auto stems (compact/dashed wall time plus an optional hex id), so the note heading falls back to `Voice memo {YYYY-MM-DD HH:MM}` and the basename uses `voice-memo`. Real human titles still pass through. Five older inbox notes still have the raw filename headings; do not rewrite their transcript bodies. Renaming them is a separate, explicit edit.
- Tests: `python -m unittest discover -s tests -t .` from the repo, using the venv. `tests/test_live.py` runs real mlx-whisper and needs the model cache plus `say` and `ffmpeg`.

## Shortcomings

- Summary and action items stay empty until summarize runs. `status` stays `raw` until then.
- Diarization/naming are implemented but off until `whisper.diarize` / `[name_speakers] enabled` and models/command are set.
- No chat UI. Query the notes from Obsidian or another tool pointed at the vault.
- Whisper on a short or noisy memo can be imperfect. Leave the transcript as heard.
- `memo-ingest status` from Terminal can report the recordings folder as unreadable even while the agent is healthy.
- The Memo Ingest app is ad-hoc signed. Gatekeeper can complain if the bundle is copied from somewhere else.

## Do not

- Upload audio, or fall back to a cloud speech API when mlx-whisper or whisper.cpp is missing. Print install instructions instead.
- Process two long recordings at once. The lock exists because that was not measured to be safe.
- Put secrets in the repo. The default path has none. Machine config, the state database, logs, `.venv`, and the Hugging Face cache stay outside git.
- Push this repo with the deploy key `~/.ssh/github-brain-work`. That key authenticates only as a deploy key for `wil-ErberAuto-commits/brain-work`. It cannot create or update a memo-ingest repo. Do not commit memo-ingest into the brain-work vault mirror.
