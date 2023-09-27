#!/usr/bin/env bash

# Enables touchID for sudo authentication

if ls /usr/lib/pam/pam_tid.so*; then
	echo "Configuring sudo authentication using TouchID:"
	PAM_LOCAL_FILE="/etc/pam.d/sudo_local"
	TOUCHID_LINE="auth       sufficient     pam_tid.so"
	# macOS versions before 14.0 Sonoma
	if ! sudo cp "$PAM_LOCAL_FILE.template" "$PAM_LOCAL_FILE"; then
		TOP_LEVEL_SUDO_FILE="/etc/pam.d/sudo"
		FIRST_LINE="# sudo: auth account password session"
		if grep -q pam_tid.so "$TOP_LEVEL_SUDO_FILE"; then
			echo "Already configured! No-op exiting."
		elif ! head -n1 "$TOP_LEVEL_SUDO_FILE" | grep -q "$FIRST_LINE"; then
			echo "$TOP_LEVEL_SUDO_FILE is not in the expected format!"
		else
			TOUCHID_LINE="auth       sufficient     pam_tid.so"
			sudo sed -i .bak -e \
				"s/$FIRST_LINE/$FIRST_LINE\n$TOUCHID_LINE/" \
				"$TOP_LEVEL_SUDO_FILE"
			# sudo rm "$TOP_LEVEL_SUDO_FILE.bak"
			echo "TouchID sudo configuration complete."
		fi
	else
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
fi
