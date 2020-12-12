[![macOS 11.0 Big Sur](https://github.com/jidicula/dotfiles/workflows/macOS%2011.0%20Big%20Sur/badge.svg)](https://github.com/jidicula/dotfiles/actions?query=workflow%3A%22macOS+11.0+Big+Sur%22) [![macOS 10.15 Catalina](https://github.com/jidicula/dotfiles/workflows/macOS%2010.15%20Catalina/badge.svg)](https://github.com/jidicula/dotfiles/actions?query=workflow%3A%22macOS+10.15+Catalina%22)

# dotfiles

A collection of dotfiles I use for config.

# Steps

1. `$ xcode-select --install`
1. `$ echo "Hostname: " &&
read -r HOSTNAME && sudo scutil --set ComputerName "$HOSTNAME" &&
scutil --set HostName "$HOSTNAME" &&
sudo scutil --set LocalHostName "$HOSTNAME"` and restart shell.
1. [Set up your SSH key](https://docs.github.com/en/free-pro-team@latest/github/authenticating-to-github/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent).
1. `$ mkdir prog && cd prog`
1. `git clone git@github.com:jidicula/dotfiles.git`
1. `cd dotfiles`
1. Install [SFMono fonts](https://developer.apple.com/fonts/).
1. Set up Terminal theme.
1. `./setup.sh chosen_hostname`
