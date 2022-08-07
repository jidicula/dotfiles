#!/usr/bin/env bash

if [[ $OSTYPE == darwin* ]]; then
	shopt -s extglob
	sudo rm -rf /Applications/!(Safari.app)
	rm "/usr/local/bin/aws*" \
		"/usr/local/bin/go*" \
		"/usr/local/bin/clusterdb" \
		"/usr/local/bin/dotnet" \
		"/usr/local/bin/createdb" \
		"/usr/local/bin/anthoscli"
fi
