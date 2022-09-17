set -o pipefail

if [[ $OSTYPE == darwin* || $CODESPACES ]]; then
	source "$HOME/.shared_shell_configs"
fi

eval "$(starship init bash)"
alias ls="ls --color=always"

if [[ $TERM == "dumb" ]]; then
	echo "halting source"
	return
fi
