#!/usr/bin/env bash

if [[ $(arch) == "arm64" ]]; then
	ARCH="arm64"
fi

if [[ $OSTYPE == darwin* ]]; then
	OS="macos"
fi

HOSTNAME="$1"
DOTFILESDIR="$(pwd -P)"

if [[ -n "$OS" ]]; then
	sudo xcodebuild -license accept
fi

# copy Git configs and templates
ln -sfv "$DOTFILESDIR/gitconfig" "$HOME/.gitconfig"
ln -sfv "$DOTFILESDIR/gitignore" "$HOME/.gitignore"
ln -sfv "$DOTFILESDIR/git-templates" "$HOME/.git-templates"

# sudo access until finished
while true; do
	sudo -n true
	sleep 60
	kill -0 "$$" || exit
done 2>/dev/null &

if [[ -n "$OS" ]]; then

	# Install Homebrew
	/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

	# Install Homebrew packages
	brew bundle install
	brew bundle check --verbose
	BREW_STATUS="$?"

else
	apt-get update
	apt-get upgrade -y
	apt-get install -y emacs npm
fi

# Install Oh My Zsh
(sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" && exit)

git clone https://github.com/zsh-users/zsh-syntax-highlighting.git "${ZSH_CUSTOM:-~/.oh-my-zsh/custom}"/plugins/zsh-syntax-highlighting

# Set up Emacs config
echo "(load \"$HOME/dotfiles/init.el\")" >"$HOME/.emacs"

if [[ -n "$OS" ]]; then

	# Link Emacs.app to Applications directory
	if [[ -z "$ARCH" ]]; then
		ln -s "/usr/local/opt/emacs-plus/Emacs.app" "/Applications"
	else
		ln -s /opt/homebrew/opt/emacs-plus@27/Emacs.app /Applications
	fi
	# Run Emacs as a background launchctl service
	brew services start d12frosted/emacs-plus/emacs-plus@27

# Link Homebrew-installed OpenSSL
# ln -s "$HOME/.development/homebrew/opt/openssl/include/openssl" "/usr/local/include"
# ln -s "$HOME/.development/homebrew/Cellar/openssl@1.1/1.1.1k/bin/openssl" "/usr/bin/openssl"
fi

# Set up ZSH config
ln -sfv "$HOME/dotfiles/.zshrc" "$HOME/.zshrc"
source "$HOME/.zshrc"

if [[ -n "$OS" ]]; then
	# Open Karabiner for the first time
	open "/Applications/Karabiner-Elements.app" && sleep 60
	killall "Karabiner-Elements"
	ln -sfv "$DOTFILESDIR/karabiner.json" "$HOME/.config/karabiner/karabiner.json"

	# Set up macOS system configs
	chmod +x system_config.sh
	./system_config.sh "$HOSTNAME" || exit
fi

# Install bash-language-server
npm install --global bash-language-server

# Install Poetry
curl -sSL https://raw.githubusercontent.com/python-poetry/poetry/master/install-poetry.py | python -
mkdir -p "$ZSH_CUSTOM/plugins/poetry"
poetry completions zsh >"$ZSH_CUSTOM/plugins/poetry/_poetry"
POETRY_CONFIG_PATH="$HOME/pypoetry/"
if [[ -n "$OS" ]]; then
	POETRY_CONFIG_PATH="$HOME/Library/Application Support/pypoetry/"
fi
mkdir -p "$POETRY_CONFIG_PATH"
ln -sfv "$DOTFILESDIR/config.toml" "$POETRY_CONFIG_PATH"

# Install go tools
mkdir "$HOME/go"
GO111MODULE=on go get -u golang.org/x/tools/...

if [[ -n "$OS" ]]; then
	# Make user-specific Applications directory
	mkdir "$HOME/Applications"
	if [[ "$BREW_STATUS" -ne 0 ]]; then
		echo "Homebrew Bundle failed, check logs" && exit "$BREW_STATUS"
	fi
fi
