#!/usr/bin/env zsh

source "$ZSH/oh-my-zsh.sh"
mkdir -p "$ZSH_CUSTOM/plugins/poetry"
poetry completions zsh >"$ZSH_CUSTOM/plugins/poetry/_poetry"
