#!/bin/bash
set -e

INPUT=$(cat)
TOOL_NAME=$(jq -r '.toolName' <<<"$INPUT")
TOOL_ARGS=$(jq -r '.toolArgs' <<<"$INPUT")
PROJECT=$(jq -r '.cwd' <<<"$INPUT")

case "$TOOL_NAME" in
bash | edit | create | ask_user)
	source ~/.shared_shell_configs 2>/dev/null
	notify "Copilot CLI awaiting approval in $PROJECT: $TOOL_NAME $TOOL_ARGS"
	;;
esac
