[![macOS 11.0 Big Sur](https://github.com/jidicula/dotfiles/workflows/macOS%2011.0%20Big%20Sur/badge.svg)](https://github.com/jidicula/dotfiles/actions?query=workflow%3A%22macOS+11.0+Big+Sur%22) [![macOS 10.15 Catalina](https://github.com/jidicula/dotfiles/workflows/macOS%2010.15%20Catalina/badge.svg)](https://github.com/jidicula/dotfiles/actions?query=workflow%3A%22macOS+10.15+Catalina%22)

# dotfiles

A collection of dotfiles I use for config.

# Steps

0. `$ mkdir prog && cd prog`
1. `$ xcode-select --install`
2. `$ echo "Hostname: " &&
read -r HOSTNAME && sudo scutil --set ComputerName "$HOSTNAME" &&
scutil --set HostName "$HOSTNAME" &&
sudo scutil --set LocalHostName "$HOSTNAME"` and restart shell.
3. [Set up your SSH key](https://docs.github.com/en/free-pro-team@latest/github/authenticating-to-github/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent).
4. `cd prog && git clone git@github.com:jidicula/dotfiles.git`
5. `cd dotfiles`
6. Install SFMono fonts.
7. Set up Terminal theme.
8. `./setup.sh chosen_hostname`
