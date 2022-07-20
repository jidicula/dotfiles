#!/usr/bin/env bash

# Enables touchID for sudo authentication

if ls /usr/lib/pam/pam_tid.so*; then
	echo "Configuring sudo authentication using TouchID:"
	PAM_FILE="/etc/pam.d/sudo"
	FIRST_LINE="# sudo: auth account password session"
	if grep -q pam_tid.so "$PAM_FILE"; then
		echo "OK"
	elif ! head -n1 "$PAM_FILE" | grep -q "$FIRST_LINE"; then
		echo "$PAM_FILE is not in the expected format!"
	else
		TOUCHID_LINE="auth       sufficient     pam_tid.so"
		sudo sed -i .bak -e \
			"s/$FIRST_LINE/$FIRST_LINE\n$TOUCHID_LINE/" \
			"$PAM_FILE"
		sudo rm "$PAM_FILE.bak"
		echo "OK"
	fi
fi
