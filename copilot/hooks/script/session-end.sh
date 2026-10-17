#!/bin/bash
set -e

source ~/.shared_shell_configs 2>/dev/null

INPUT=$(cat)
REASON=$(jq -r '.reason' <<<"$INPUT")
PROJECT=$(jq -r '.cwd' <<<"$INPUT")

notify "Ready for next input in $PROJECT. Reason: $REASON."
