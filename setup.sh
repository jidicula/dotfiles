#!/usr/bin/env bash

sudo xcodebuild -license accept

# copy Git configs and templates
cp gitconfig ~/.gitconfig
cp gitignore ~/.gitignore
cp -r git-templates ~/.git-templates

# Install Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/master/install.sh)"

# Install Homebrew packages
brew bundle install || echo "Failed with code $?" && exit

# Install Oh My Zsh
sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)"

# Install Pure
npm install --global pure-prompt

# Create GPG key
gpg --full-generate-key

# Set up Emacs config
echo "(load \"~/prog/dotfiles/init.el\")" >>~/.emacs

# Set up ZSH config
echo "source \"$HOME/prog/dotfiles/.zshrc\"" >>~/.zshrc

# Open Karabiner for the first time
open "/Applications/Karabiner-Elements.app"
cp karabiner.json "$HOME/.config/karabiner/"

# Set up macOS system configs
chmod +x system_config.sh
./system_config.sh || echo "Failed with code $?" && exit

# Make user-specific Applications directory
mkdir "$HOME/Applications"

# Reboot machine
sudo reboot
