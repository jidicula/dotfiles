#!/usr/bin/env bash
shopt -s extglob

if [[ $OSTYPE == darwin* ]]; then
	sudo rm -rf /Applications/!(Safari.app)
	rm "/usr/local/bin/aws*" \
		"/usr/local/bin/go*" \
		"/usr/local/bin/clusterdb" \
		"/usr/local/bin/dotnet" \
		"/usr/local/bin/createdb" \
		"/usr/local/bin/anthoscli"
fi
