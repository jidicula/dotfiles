#!/usr/bin/env bash
shopt -s extglob

if [[ $OSTYPE == darwin* ]]; then
	sudo rm -rf /Applications/!(Safari.app|Xcode*)
	shopt -u extglob
	rm /usr/local/bin/aws* \
		/usr/local/bin/dotnet
fi
