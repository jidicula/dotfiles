[![Build](https://github.com/jidicula/dotfiles/actions/workflows/setup.yml/badge.svg)](https://github.com/jidicula/dotfiles/actions/workflows/setup.yml)

# dotfiles

A collection of config dotfiles.

# Steps

1. `$ xcode-select --install`
1. `$ echo "Hostname: " &&
read -r HOSTNAME && sudo scutil --set ComputerName "$HOSTNAME" &&
scutil --set HostName "$HOSTNAME" &&
sudo scutil --set LocalHostName "$HOSTNAME"` and restart shell.
1. [Set up your SSH key](https://docs.github.com/en/free-pro-team@latest/github/authenticating-to-github/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent).
1. `git clone git@github.com:jidicula/dotfiles.git`
1. `cd dotfiles`
1. Set up Terminal theme.
1. `script/setup chosen_hostname`
1. Import GPG signing key(s).
