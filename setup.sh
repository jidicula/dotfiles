#!/usr/bin/env bash

HOSTNAME="$1"
DOTFILESDIR="$(pwd -P)"

sudo xcodebuild -license accept

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

# Install Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Homebrew packages
brew bundle install
brew bundle check --verbose
BREW_STATUS="$?"

# Retry brew bundle install once if anything failed
if [[ "$BREW_STATUS" -ne 0 ]]; then
	./teardown.sh
	brew bundle install
	brew bundle check --verbose
	BREW_STATUS="$?"
fi

# Install Oh My Zsh
(sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" && exit)

git clone https://github.com/zsh-users/zsh-syntax-highlighting.git "${ZSH_CUSTOM:-~/.oh-my-zsh/custom}"/plugins/zsh-syntax-highlighting

# Install Pure
npm install --global pure-prompt

# Install bash-language-server
npm install --global bash-language-server

# Set up Emacs config
echo "(load \"$HOME/dotfiles/init.el\")" >"$HOME/.emacs"

# Link Emacs.app to Applications directory
ln -s "/usr/local/opt/emacs-plus/Emacs.app" "/Applications"

# Link Homebrew-installed OpenSSL
ln -s "$HOME/.development/homebrew/opt/openssl/include/openssl" "/usr/local/include"
ln -s "$HOME/.development/homebrew/Cellar/openssl@1.1/[version]/bin/openssl" "/usr/bin/openssl"

# Set up ZSH config
echo "source \"$HOME/dotfiles/.zshrc\"" >"$HOME/.zshrc"
source "$HOME/.zshrc"

# Open Karabiner for the first time
open "/Applications/Karabiner-Elements.app" && sleep 60
killall "Karabiner-Elements"
ln -sfv "$DOTFILESDIR/karabiner.json" "$HOME/.config/karabiner/karabiner.json"

# Set up macOS system configs
chmod +x system_config.sh
./system_config.sh "$HOSTNAME" || exit

# Install Poetry
curl -sSL https://raw.githubusercontent.com/python-poetry/poetry/master/get-poetry.py | python -
mkdir -p "$ZSH_CUSTOM/plugins/poetry"
poetry completions zsh >"$ZSH_CUSTOM/plugins/poetry/_poetry"
mkdir -p "$HOME/Library/Application Support/pypoetry/"
ln -sfv "$DOTFILESDIR/config.toml" "$HOME/Library/Application Support/pypoetry/config.toml"

# Install gopls
mkdir $HOME/go
GO111MODULE=on go get -u golang.org/x/tools/...

# Make user-specific Applications directory
mkdir "$HOME/Applications"

if [[ "$BREW_STATUS" -ne 0 ]]; then
	echo "Homebrew Bundle failed, check logs" && exit "$BREW_STATUS"
fi
