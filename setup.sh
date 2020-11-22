#!/usr/bin/env bash

sudo xcodebuild -license accept

# copy Git configs and templates
cp gitconfig ~/.gitconfig
cp gitignore ~/.gitignore
cp -r git-templates ~/.git-templates

# Install Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/master/install.sh)"

# Install Homebrew packages
brew bundle install || exit

# Install Oh My Zsh
(sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)")

git clone https://github.com/zsh-users/zsh-syntax-highlighting.git ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting

# Install Pure
npm install --global pure-prompt

# Set up Emacs config
echo "(load \"~/prog/dotfiles/init.el\")" >~/.emacs

# Link Emacs.app to Applications directory
ln -s "/usr/local/opt/emacs-plus/Emacs.app" "/Applications"

# Set up ZSH config
echo "source \"$HOME/prog/dotfiles/.zshrc\"" >~/.zshrc

# Open Karabiner for the first time
open "/Applications/Karabiner-Elements.app"
cp karabiner.json "$HOME/.config/karabiner/"

# Set up macOS system configs
chmod +x system_config.sh
./system_config.sh || exit

# Make user-specific Applications directory
mkdir "$HOME/Applications"

# Reboot machine
sudo reboot
