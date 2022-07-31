#!/usr/bin/env bash

if [[ $OSTYPE == darwin* ]]; then
	rm '/usr/local/bin/aws' \
		'/usr/local/bin/go' \
		'/usr/local/bin/clusterdb' \
		'/usr/local/bin/dotnet'

fi
