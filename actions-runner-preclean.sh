#!/usr/bin/env bash

if [[ $OSTYPE == darwin* ]]; then
	sudo find /Applications ! -name 'Safari.app' -type d -exec rm -rf {} +
	rm "/usr/local/bin/aws*" \
		"/usr/local/bin/go*" \
		"/usr/local/bin/clusterdb" \
		"/usr/local/bin/dotnet" \
		"/usr/local/bin/createdb" \
		"/usr/local/bin/anthoscli"
fi
