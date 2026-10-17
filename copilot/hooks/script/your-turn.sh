#!/bin/bash
set -e

source ~/.shared_shell_configs 2>/dev/null

INPUT=$(cat)
SESSION_ID=$(jq -r '.sessionId' <<<"$INPUT")
IS_SUBAGENT=$(jq -r '[.agentId, .parentAgentId] | any(. != null and . != "")' <<<"$INPUT")
[[ "$IS_SUBAGENT" == false ]] || exit 0

TRANSCRIPT_PATH=$(jq -r '.transcriptPath // ""' <<<"$INPUT")

# Older subagents omit identity fields and reuse the parent's transcript.
[[ "$TRANSCRIPT_PATH" == */"$SESSION_ID"/events.jsonl ]] || exit 0

STOP_TIMESTAMP=$(jq -er '.timestamp | if type == "number" then . else error("agentStop timestamp must be a number") end' <<<"$INPUT")
# A stop can be an automatic handoff rather than a completed reply to the user.
SHOULD_NOTIFY=$(python3 "$(dirname "${BASH_SOURCE[0]}")/should-notify.py" "$TRANSCRIPT_PATH" "$SESSION_ID" "$STOP_TIMESTAMP")
[[ "$SHOULD_NOTIFY" == true ]] || exit 0

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

notify "Your turn in $LABEL."
