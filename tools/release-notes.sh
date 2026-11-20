#!/usr/bin/env bash
# Rewrites a release's title and notes in this fork's style (tools/release_notes.py).
#   tools/release-notes.sh TAG ["what changed"]
# Needs gh signed in (GH_TOKEN) and GITHUB_REPOSITORY (default: Tomba247/Henge).
set -euo pipefail
TAG=$1
CHANGE=${2:-}
REPO=${GITHUB_REPOSITORY:-Tomba247/Henge}
HERE=$(cd "$(dirname "$0")" && pwd)
TMP=$(mktemp -d)
gh release view "$TAG" --repo "$REPO" --json name,body,assets > "$TMP/release.json"
python3 "$HERE/release_notes.py" "$TMP/release.json" "$TAG" "$REPO" "$CHANGE" > "$TMP/out.md"
head -1 "$TMP/out.md" > "$TMP/title"
tail -n +2 "$TMP/out.md" > "$TMP/notes.md"
gh release edit "$TAG" --repo "$REPO" --title "$(cat "$TMP/title")" --notes-file "$TMP/notes.md"
echo "release $TAG: $(cat "$TMP/title")"
rm -rf "$TMP"
