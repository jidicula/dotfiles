set -o pipefail

if [[ $OSTYPE == darwin* || $CODESPACES ]]; then
	source "$HOME/.shared_shell_configs"
fi

eval "$(starship init bash)"

# Not in shared_shell_configs because it's getting overridden by oh-my-zsh
alias ls="ls --color=always -N"

if [[ $TERM == "dumb" ]]; then
	echo "halting source"
	return
fi

test -e "${HOME}/.iterm2_shell_integration.bash" && source ~/.iterm2_shell_integration.bash || true
