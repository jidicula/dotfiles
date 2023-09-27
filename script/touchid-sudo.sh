#!/usr/bin/env bash

# Enables touchID for sudo authentication

if ls /usr/lib/pam/pam_tid.so*; then
	echo "Configuring sudo authentication using TouchID:"
	PAM_LOCAL_FILE="/etc/pam.d/sudo_local"
	TOUCHID_LINE="auth       sufficient     pam_tid.so"
	sudo cp "$PAM_LOCAL_FILE.template" "$PAM_LOCAL_FILE"
	if ! sed -n '3p' "$PAM_LOCAL_FILE" | grep -q "${TOUCHID_LINE}"; then
		echo "$PAM_LOCAL_FILE is not in the expected format!"
	else
		sudo sed -i .bak -e \
			"s/# $TOUCHID_LINE/$TOUCHID_LINE/" \
			"$PAM_LOCAL_FILE"
		sudo rm "$PAM_LOCAL_FILE.bak"
		echo "TouchID sudo configuration complete."
	fi
fi
