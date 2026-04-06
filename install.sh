#!/usr/bin/env bash
set -euo pipefail

if [ ! -d ".git" ]; then
  echo "Run this from the target repository root."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cp -r "$SCRIPT_DIR/.ai" .
cp -r "$SCRIPT_DIR/.claude" .
cp "$SCRIPT_DIR/AGENTS.md" .
cp "$SCRIPT_DIR/CLAUDE.md" .

echo "Scaffold copied into current repository."
echo "Next step: ask Sonnet to run the bootstrap prompt from .ai/templates/bootstrap-prompt.md"
