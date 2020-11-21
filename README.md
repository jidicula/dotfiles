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
4. `git clone git@github.com:jidicula/dotfiles.git`
5. `cd dotfiles`
6. Install SFMono fonts.
7. Set up Terminal theme.
8. `./setup.sh`
