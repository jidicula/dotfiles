#!/usr/bin/env bash

HOSTNAME="$1"

sudo xcodebuild -license accept

# copy Git configs and templates
cp gitconfig ~/.gitconfig
cp gitignore ~/.gitignore
cp -r git-templates ~/.git-templates

# sudo access until finished
while true; do
	sudo -n true
	sleep 60
	kill -0 "$$" || exit
done 2>/dev/null &

# Install Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/master/install.sh)"

# Install Homebrew packages
brew bundle install
brew bundle check
BREW_STATUS="$?"

# Retry brew bundle install once if anything failed
if [[ "$BREW_STATUS" -ne 0 ]]; then
	./teardown.sh
	brew bundle install
	brew bundle check
	BREW_STATUS="$?"
fi

# Install Oh My Zsh
(sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" && exit)

git clone https://github.com/zsh-users/zsh-syntax-highlighting.git ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting

# Install Pure
npm install --global pure-prompt

# Install bash-language-server
npm install --global bash-language-server

# Install Poetry
curl -sSL https://raw.githubusercontent.com/python-poetry/poetry/master/get-poetry.py | python -

# Set up Emacs config
echo "(load \"~/prog/dotfiles/init.el\")" >"$HOME/.emacs"

# Link Emacs.app to Applications directory
ln -s "/usr/local/opt/emacs-plus/Emacs.app" "/Applications"

# Link Homebrew-installed OpenSSL
ln -s "$HOME/.development/homebrew/opt/openssl/include/openssl" "/usr/local/include"
ln -s "$HOME/.development/homebrew/Cellar/openssl@1.1/[version]/bin/openssl" "/usr/bin/openssl"

# Set up ZSH config
echo "source \"$HOME/prog/dotfiles/.zshrc\"" >~/.zshrc

# Open Karabiner for the first time
open "/Applications/Karabiner-Elements.app" && sleep 60
killall "Karabiner-Elements"
cp karabiner.json "$HOME/.config/karabiner/"

# Set up macOS system configs
chmod +x system_config.sh
./system_config.sh "$HOSTNAME" || exit

# Make user-specific Applications directory
mkdir "$HOME/Applications"

if [[ "$BREW_STATUS" -ne 0 ]]; then
	echo "Homebrew Bundle failed, check logs" && exit "$BREW_STATUS"
fi
