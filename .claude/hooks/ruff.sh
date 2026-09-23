#!/usr/bin/env bash
# PostToolUse (Edit|Write): format and lint the edited Python file with ruff.
# Remaining lint errors go to stderr with exit 2 so Claude sees and fixes them.
set -uo pipefail

file=$(jq -r '.tool_input.file_path // .tool_response.filePath // empty')
[[ "$file" == *.py && -f "$file" ]] || exit 0

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0
uv run --quiet ruff format --quiet "$file" || exit 0
if ! out=$(uv run --quiet ruff check --fix --quiet --output-format concise "$file" 2>&1); then
  echo "ruff check found issues in $file:" >&2
  echo "$out" >&2
  exit 2
fi
