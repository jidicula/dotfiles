![macOS 11.0 Big Sur](https://github.com/jidicula/dotfiles/workflows/macOS%2011.0%20Big%20Sur/badge.svg) ![macOS 10.15 Catalina](https://github.com/jidicula/dotfiles/workflows/macOS%2010.15%20Catalina/badge.svg)

# dotfiles

A collection of dotfiles I use for config.

# Steps

0. `$ mkdir prog && cd prog`
1. `$ xcode-select --install`
2. `$ echo "Hostname: " &&
read -r HOSTNAME && sudo scutil --set ComputerName "$HOSTNAME" &&
scutil --set HostName "$HOSTNAME" &&
sudo scutil --set LocalHostName "$HOSTNAME"` and restart shell.
3. Set up your SSH key.
4. `cd prog && git clone git@github.com:jidicula/dotfiles.git`
5. `cd dotfiles`
6. Install SFMono fonts.
7. Set up Terminal theme.
8. `./setup.sh`
