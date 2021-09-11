#!/usr/bin/env bash

# Emacs Plus must be removed before reinstalling
brew uninstall emacs-plus

# Clean up hardlinked files
rm "$HOME/.git-templates"
rm '~'
