#!/bin/bash
# Build Memo Ingest.app and install it into ~/Applications.
set -euo pipefail
cd "$(dirname "$0")"
APP="$HOME/Applications/Memo Ingest.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -o "$APP/Contents/MacOS/MemoIngest" MemoIngestApp.swift
cp Info.plist "$APP/Contents/Info.plist"
codesign -s - --force "$APP"
echo "Built $APP"
