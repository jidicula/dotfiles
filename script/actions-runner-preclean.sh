#!/usr/bin/env bash
shopt -s extglob

if [[ $OSTYPE == darwin* ]]; then
	sudo rm -rf /Applications/!(Safari.app|Xcode*)
	shopt -u extglob
	rm /usr/local/bin/aws* \
		/usr/local/bin/go* \
		/usr/local/bin/dotnet \
		\
		/usr/local/bin/2to3 # /usr/local/bin/docker-compose \
	# /usr/local/bin/kubectl \
fi
