# macOS loads its terminal-session hooks before .zshrc.
if [[ ! -t 0 || ! -t 1 || $TERM == dumb ]]; then
	SHELL_SESSIONS_DISABLE=1
fi
