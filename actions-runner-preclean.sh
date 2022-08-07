#!/usr/bin/env bash

if [[ $OSTYPE == darwin* ]]; then
	sudo find /Applications ! -path '/Applications/Safari.app*' -type df -exec rm -rf {} +
	rm "/usr/local/bin/aws*" \
		"/usr/local/bin/go*" \
		"/usr/local/bin/clusterdb" \
		"/usr/local/bin/dotnet" \
		"/usr/local/bin/createdb" \
		"/usr/local/bin/anthoscli"
fi
