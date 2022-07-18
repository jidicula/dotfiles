#!/usr/bin/env bash

# sudo access until finished
while true; do
	sudo -n true
	sleep 60
	kill -0 "$$" || exit
done 2>/dev/null &

if [[ $(arch) == "arm64" ]]; then
	ARCH="arm64"
fi

if [[ $OSTYPE == darwin* ]]; then
	OS="macos"
fi

HOSTNAME="$1"
DOTFILESDIR="$(pwd -P)"

if [[ -n "$OS" ]]; then
	sudo xcodebuild -license accept
fi

# Make folder for code
mkdir -p "$HOME/Developer/work"

# Make folder for GnuPG
mkdir -p "$HOME/.gnupg"

# copy GPG config
ln -sfv "$DOTFILESDIR/gpg.conf" "$HOME/.gnupg/gpg.conf"

# copy Git configs and templates
ln -sfv "$DOTFILESDIR/gitconfig" "$HOME/.gitconfig"
ln -sfv "$DOTFILESDIR/gitignore" "$HOME/.gitignore"
ln -sfv "$DOTFILESDIR/git-templates" "$HOME/.git-templates"

if [[ -n "$OS" ]]; then
	if ls /usr/lib/pam | grep -q "pam_tid.so"; then
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
fi

if [[ -n "$OS" ]]; then

	# Install Homebrew
	/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

	# Install Homebrew packages
	brew bundle install
	brew bundle check --verbose
	BREW_STATUS="$?"

else
	sudo apt-get update
	sudo apt-get upgrade -y
	sudo apt-get install -y emacs npm
fi

# Install Oh My Zsh
(sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" && exit)

git clone https://github.com/zsh-users/zsh-syntax-highlighting.git "${ZSH_CUSTOM:-~/.oh-my-zsh/custom}"/plugins/zsh-syntax-highlighting

# Set up Emacs config
echo "(load \"$HOME/dotfiles/init.el\")" >"$HOME/.emacs"

if [[ -n "$OS" ]]; then

	# Link Emacs.app to Applications directory
	if [[ -z "$ARCH" ]]; then
		ln -s "/usr/local/opt/emacs-plus/Emacs.app" "/Applications"
	else
		ln -s /opt/homebrew/opt/emacs-plus@28/Emacs.app /Applications
	fi
	# Run Emacs as a background launchctl service
	brew services start d12frosted/emacs-plus/emacs-plus@28

# Link Homebrew-installed OpenSSL
# ln -s "$HOME/.development/homebrew/opt/openssl/include/openssl" "/usr/local/include"
# ln -s "$HOME/.development/homebrew/Cellar/openssl@1.1/1.1.1k/bin/openssl" "/usr/bin/openssl"
fi

# Set up ZSH config
ln -sfv "$HOME/dotfiles/.zshrc" "$HOME/.zshrc"
if [[ -e "$HOME/.zshrc" ]]; then
	source "$HOME/.zshrc"
fi

if [[ -n "$OS" ]]; then
	# Open Karabiner for the first time
	open "/Applications/Karabiner-Elements.app" && sleep 60
	killall "Karabiner-Elements"
	ln -sfv "$DOTFILESDIR/karabiner.json" "$HOME/.config/karabiner/karabiner.json"

	# Set up macOS system configs
	chmod +x system_config.sh
	./system_config.sh "$HOSTNAME" || exit
fi

# Install bash-language-server
npm install --global bash-language-server

# Install Poetry
curl -sSL https://install.python-poetry.org | python3 -
mkdir -p "$ZSH_CUSTOM/plugins/poetry"
poetry completions zsh >"$ZSH_CUSTOM/plugins/poetry/_poetry"
POETRY_CONFIG_PATH="$HOME/.config/pypoetry/"
if [[ -n "$OS" ]]; then
	POETRY_CONFIG_PATH="$HOME/Library/Application\ Support/pypoetry/"
fi
mkdir -p "$POETRY_CONFIG_PATH"
ln -sfv "$DOTFILESDIR/config.toml" "$POETRY_CONFIG_PATH/config.toml"

# Install go tools
mkdir "$HOME/go"
go install golang.org/x/tools/...@latest

# Install C# tools
"$(brew --prefix)/bin/dotnet" tool install -g csharp-ls

if [[ -n "$OS" ]]; then
	# Make user-specific Applications directory
	mkdir "$HOME/Applications"
	if [[ "$BREW_STATUS" -ne 0 ]]; then
		echo "Homebrew Bundle failed, check logs" && exit "$BREW_STATUS"
	fi
fi
