#!/usr/bin/env bash

# Enables touchID for sudo authentication

if ! [[ -d "${RESOLVER_PATH:=/etc/resolver}" ]]; then
	sudo mkdir -p "$RESOLVER_PATH"
	if ! [[ -f "${RESOLVER_CONFIG_LOCALHOST:=/etc/resolver/localhost}" ]]; then
		sudo ln -sfv "$HOME/dotfiles/dns/localhost" "$RESOLVER_CONFIG_LOCALHOST"
	fi
	if ! [[ -f "${RESOLVER_CONFIG_CODEDEV:=/etc/resolver/codedev.ms}" ]]; then
		sudo ln -sfv "$HOME/dotfiles/dns/codedev.ms" "$RESOLVER_CONFIG_CODEDEV"
	fi
	brew services restart launchdns
fi
