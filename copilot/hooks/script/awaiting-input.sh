#!/bin/bash
set -e

source ~/.shared_shell_configs 2>/dev/null

INPUT=$(cat)
TITLE=$(jq -r '.title' <<<"$INPUT")
SESSION_ID=$(jq -r '.sessionId' <<<"$INPUT")
PROJECT=$(jq -r '.cwd' <<<"$INPUT")

case "$PROJECT" in
"$HOME") PROJECT="~" ;;
"$HOME"/*) PROJECT="~/${PROJECT#"$HOME"/}" ;;
esac

COPILOT_DIR=${COPILOT_HOME:-"$HOME/.copilot"}
WORKSPACE="$COPILOT_DIR/session-state/$SESSION_ID/workspace.yaml"
SESSION_NAME=$(yq -r '.name // ""' "$WORKSPACE" 2>/dev/null || true)
LABEL=$PROJECT
[[ -n "$SESSION_NAME" ]] && LABEL="$PROJECT: $SESSION_NAME"

notify "Awaiting further input in $LABEL. Reason: $TITLE."
