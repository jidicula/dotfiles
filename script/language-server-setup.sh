#!/usr/bin/env bash

# Install bash-language-server
npm install --global bash-language-server

# Install gopls
go install golang.org/x/tools/gopls@latest

# Install Solargraph (Ruby)
gem install solargraph

# Install C# tools
if [[ $OSTYPE == darwin* ]]; then
	"$(brew --prefix)/bin/dotnet" tool install -g csharp-ls
fi
mkdir "$HOME/.omnisharp"
ln -sfv "$DOTFILESDIR/omnisharp.json" "$HOME/.omnisharp/omnisharp.json"

# Install python LSP
npm install --global pyright

# Powershell LS
PSES_BUNDLE_PATH="$HOME/.pses"
mkdir -p "$PSES_BUNDLE_PATH/session"
(
	cd "$PSES_BUNDLE_PATH" || exit
	curl https://raw.githubusercontent.com/coc-extensions/coc-powershell/master/downloadPSES.ps1 | pwsh
)

# Docker LS
npm install -g dockerfile-language-server-nodejs

# YAML
npm install -g yaml-language-server
