#!/usr/bin/env bash
shopt -s extglob

if [[ $OSTYPE == darwin* ]]; then
	sudo rm -rf /Applications/!(Safari.app|Xcode*)
	rm "/usr/local/bin/aws*" \
		"/usr/local/bin/go*" \
		"/usr/local/bin/dotnet"
fi
