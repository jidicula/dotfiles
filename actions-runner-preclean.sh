#!/usr/bin/env bash
shopt -s extglob

if [[ $OSTYPE == darwin* ]]; then
	sudo rm -rf /Applications/!(Safari.app)
	# rm "$(
	which "aws" \
		"go" \
		"clusterdb" \
		"dotnet" \
		"createdb"
	# )"
fi
