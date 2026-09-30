#!/bin/bash
# Read memo-ingest summarize prompt+transcript from stdin; print Grok's markdown to stdout.
# Requires ~/.grok/bin/grok (Grok Build) logged in.
set -euo pipefail
GROK="${GROK_BIN:-$HOME/.grok/bin/grok}"
if [[ ! -x "$GROK" ]]; then
  echo "grok not found at $GROK" >&2
  exit 127
fi
tmp="$(mktemp -t memo-grok-summarize.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
cat >"$tmp"
exec "$GROK" \
  --prompt-file "$tmp" \
  --verbatim \
  --max-turns 1 \
  --no-subagents \
  --disable-web-search \
  --output-format plain
