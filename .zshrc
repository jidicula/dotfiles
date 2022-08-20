set -o pipefail
# If you come from bash you might have to change your $PATH.
# export PATH=$HOME/bin:/usr/local/bin:$PATH

# Path to your oh-my-zsh installation.
export ZSH="$HOME/.oh-my-zsh"

# Set name of the theme to load --- if set to "random", it will
# load a random theme each time oh-my-zsh is loaded, in which case,
# to know which specific one was loaded, run: echo $RANDOM_THEME
# See https://github.com/robbyrussell/oh-my-zsh/wiki/Themes
# ZSH_THEME="jidiculous"

# Set list of themes to pick from when loading at random
# Setting this variable when ZSH_THEME=random will cause zsh to load
# a theme from this variable instead of looking in ~/.oh-my-zsh/themes/
# If set to an empty array, this variable will have no effect.
# ZSH_THEME_RANDOM_CANDIDATES=( "robbyrussell" "agnoster" )

# Uncomment the following line to use case-sensitive completion.
# CASE_SENSITIVE="true"

# Uncomment the following line to use hyphen-insensitive completion.
# Case-sensitive completion must be off. _ and - will be interchangeable.
# HYPHEN_INSENSITIVE="true"

# Uncomment the following line to disable bi-weekly auto-update checks.
# DISABLE_AUTO_UPDATE="true"

# Uncomment the following line to automatically update without prompting.
# DISABLE_UPDATE_PROMPT="true"

# Uncomment the following line to change how often to auto-update (in days).
# export UPDATE_ZSH_DAYS=13

# Uncomment the following line if pasting URLs and other text is messed up.
# DISABLE_MAGIC_FUNCTIONS=true

# Uncomment the following line to disable colors in ls.
# DISABLE_LS_COLORS="true"

# Uncomment the following line to disable auto-setting terminal title.
# DISABLE_AUTO_TITLE="true"

# Uncomment the following line to enable command auto-correction.
# ENABLE_CORRECTION="true"

# Uncomment the following line to display red dots whilst waiting for completion.
# COMPLETION_WAITING_DOTS="true"

# Uncomment the following line if you want to disable marking untracked files
# under VCS as dirty. This makes repository status check for large repositories
# much, much faster.
DISABLE_UNTRACKED_FILES_DIRTY="true"

# Uncomment the following line if you want to change the command execution time
# stamp shown in the history command output.
# You can set one of the optional three formats:
# "mm/dd/yyyy"|"dd.mm.yyyy"|"yyyy-mm-dd"
# or set a custom format using the strftime function format specifications,
# see 'man strftime' for details.
# HIST_STAMPS="mm/dd/yyyy"

# Would you like to use another custom folder than $ZSH/custom?
# ZSH_CUSTOM=/path/to/new-custom-folder

# User configuration

# export MANPATH="/usr/local/man:$MANPATH"

# You may need to manually set your language environment
# export LANG=en_US.UTF-8

# Preferred editor for local and remote sessions
if [[ -z $SSH_CONNECTION ]]; then
	export EDITOR="$HOME/Applications/Emacs.app/Contents/MacOS/Emacs"
else
	export EDITOR="/usr/bin/env nano"
fi

# Compilation flags
# export ARCHFLAGS="-arch x86_64"

function dir() {
	mkdir -p -- "$1" &&
		cd -P -- "$1"
}

# shellcheck source=/dev/null
if [[ $OSTYPE == darwin* ]]; then
	source "/opt/homebrew/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh"
else
	source "/usr/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh"
fi

# PATH
export PATH="/usr/local/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/X11/bin:/Library/TeX/texbin"

# Homebrew shellenv
if [[ $(arch) == "arm64" ]]; then
	ARCH="arm64"
fi
if [[ $ARCH == "arm64" ]]; then
	if [[ $OSTYPE == darwin* ]]; then
		eval "$(/opt/homebrew/bin/brew shellenv)"
	fi
fi

DOTFILESDIR="$HOME/dotfiles"
if [[ $CODESPACES ]]; then
	DOTFILESDIR="/workspaces/.codespaces/.persistedshare/dotfiles"
fi
# pipenv should be created in the project dir
export PIPENV_VENV_IN_PROJECT=1

# Poetry
export PATH="$HOME/.local/bin:$PATH"

# Python Tcl-Tk options for pyenv
export PYTHON_CONFIGURE_OPTS="--with-tcltk-includes='-I/usr/local/opt/tcl-tk/include' --with-tcltk-libs='-L/usr/local/opt/tcl-tk/lib -ltcl8.6 -ltk8.6'"
export TK_SILENCE_DEPRECATION=1

# go
export GOPATH="$HOME/go"
export PATH="$PATH:$GOPATH/bin"
export PATH="$PATH:$GOROOT/bin"
gt() {
	go test "$@" ./... | sed ''/PASS/s//$(printf "\033[32mPASS\033[0m")/'' | sed ''/FAIL/s//$(printf "\033[31mFAIL\033[0m")/''
}
gtv() {
	go test -v "$@" ./... | sed ''/PASS/s//$(printf "\033[32mPASS\033[0m")/'' | sed ''/FAIL/s//$(printf "\033[31mFAIL\033[0m")/''
}
gtb() {
	go test -bench=. "$@" ./... | sed ''/PASS/s//$(printf "\033[32mPASS\033[0m")/'' | sed ''/FAIL/s//$(printf "\033[31mFAIL\033[0m")/''
}
gtbv() {
	go test -v -bench=. "$@" ./... | sed ''/PASS/s//$(printf "\033[32mPASS\033[0m")/'' | sed ''/FAIL/s//$(printf "\033[31mFAIL\033[0m")/''
}
gtc() {
	local t=$(mktemp -t cover)
	go test $COVERFLAGS -coverprofile="$t" "$@" ./... | sed ''/PASS/s//$(printf "\033[32mPASS\033[0m")/'' | sed ''/FAIL/s//$(printf "\033[31mFAIL\033[0m")/'' &&
		go tool cover -html="$t" &&
		unlink "$t"
}
gtch() {
	local t=$(mktemp -t cover)
	go test $COVERFLAGS -covermode=count -coverprofile="$t" "$@" ./... | sed ''/PASS/s//$(printf "\033[32mPASS\033[0m")/'' | sed ''/FAIL/s//$(printf "\033[31mFAIL\033[0m")/'' &&
		go tool cover -html="$t" &&
		unlink "$t"
}
alias gl="golangci-lint run"

# source zprofile in case any programs have added configs in there (e.g. FSL)
touch "$HOME/.zprofile"
# shellcheck source=/dev/null
source "$HOME/.zprofile"

# Starship configs
export STARSHIP_CONFIG="$HOME/.starship.toml"
eval "$(starship init zsh)"

if [[ $OSTYPE == darwin* ]]; then
	if command -v pyenv 1>/dev/null 2>&1; then
		export PYENV_ROOT="$HOME/.pyenv"
		export PATH="$PYENV_ROOT/bin:$PATH"
		eval "$(pyenv init --path)"
		export ZSH_PYENV_VIRTUALENV=true
	fi
fi

if [[ $OSTYPE == darwin* ]]; then
	if command -v nodenv 1>/dev/null 2>&1; then
		export NODENV_ROOT="$HOME/.nodenv"
		export PATH="$NODENV_ROOT/bin:$PATH"
		eval "$(nodenv init -)"
	fi
fi

if command -v rbenv 1>/dev/null 2>&1; then
	export RBENV_ROOT="$HOME/.rbenv"
	export PATH="$RBENV_ROOT/bin:$PATH"
	eval "$(rbenv init -)"
fi

PLAN9=/usr/local/plan9
export PLAN9
export PATH="$PATH:$PLAN9/bin"

# dotnet
export DOTNET_CLI_TELEMETRY_OPTOUT=1
export PATH="$HOME/.dotnet/tools:$PATH"

# GNU dependencies for building some Homebrew formulae, like Emacs
export PATH="/opt/homebrew/opt/gnu-tar/libexec/gnubin:$PATH"
export PATH="/opt/homebrew/opt/coreutils/libexec/gnubin:$PATH"

# Example format: plugins=(rails git textmate ruby lighthouse)
# Add wisely, as too many plugins slow down shell startup.
plugins=(
	macos
	brew
	kubectl
	python
	pyenv
	pip
	rbenv
	ruby
	gpg-agent
	poetry
	golang
	dotnet
)

# shellcheck source=/dev/null
source "$ZSH/oh-my-zsh.sh"

# Set personal aliases, overriding those provided by oh-my-zsh libs,
# plugins, and themes. Aliases can be placed here, though oh-my-zsh
# users are encouraged to define aliases within the ZSH_CUSTOM folder.
# For a full list of active aliases, run `alias`.
#
# Example aliases
# alias zshconfig="mate ~/.zshrc"
# alias ohmyzsh="mate ~/.oh-my-zsh"
# shellcheck source=/dev/null
alias cdr='cd $(git rev-parse --show-toplevel)'
alias gcce="gcc -Wextra -Wpedantic"
alias ls="ls --color=auto"
alias cs="gh codespace ssh"
if [[ $OSTYPE == darwin* && -e "$HOME/Documents/dev_env/dotfiles/.zsh_aliases" ]]; then
	source "$HOME/Documents/dev_env/dotfiles/.zsh_aliases"
fi

if [[ $OSTYPE == darwin* ]]; then
	# GCloud
	export USE_GKE_GCLOUD_AUTH_PLUGIN=True
	source "/opt/homebrew/Caskroom/google-cloud-sdk/latest/google-cloud-sdk/path.zsh.inc"
fi
