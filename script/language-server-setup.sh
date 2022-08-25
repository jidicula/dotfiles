#!/usr/bin/env bash

# Install bash-language-server
npm install --global bash-language-server

# Install gopls
go install golang.org/x/tools/gopls@latest

# Install C# tools
if [[ $OSTYPE == darwin* ]]; then
	"$(brew --prefix)/bin/dotnet" tool install -g csharp-ls
fi
