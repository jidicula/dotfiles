;;; init.el --- Johanan's Emacs config
;;; Commentary:
;;; Load it from .emacs with `(load "path/to/init.el")`

;; Startup timer
(add-hook 'emacs-startup-hook
          (lambda ()
            (message "Emacs ready in %s with %d garbage collections."
                     (format "%.2f seconds"
                             (float-time
                              (time-subtract after-init-time before-init-time)))
                     gcs-done)))

;; increase gc-cons-threshold to 100mb
(setq gc-cons-threshold 100000000)

;; increase amount of data Emacs reads from process to 1mb
(setq read-process-output-max (* 1024 1024))

(message (format "%s" file-name-handler-alist))
(setq file-name-handler-alist nil)

;; confirm before quitting
(setq confirm-kill-emacs 'y-or-n-p)

;; Confirm before closing frame in daemon mode.
(defun ask-before-closing (&optional arg)
  "Close frame ARG or buffer only if \`y\` was pressed."
  (interactive)
  (if (y-or-n-p (format "Are you sure you want to close this frame?"))
		(save-buffers-kill-terminal)
    (message "Canceled frame close")))

(when (daemonp)
  (global-set-key (kbd "C-x C-c") 'ask-before-closing)
  (add-hook 'close-display-connection 'ask-before-closing)
  (add-to-list 'delete-frame-functions 'ask-before-closing)
  )

;; no welcome screen
(setq inhibit-startup-screen t)

;; no large file warning
(setq large-file-warning-threshold nil)

;; highlight trailing whitespace
(whitespace-mode 1)

;; 80-col ruler
(setq-default fill-column 80)

;; Don't switch frames when changing buffers
(setq ido-default-buffer-method 'selected-window)

;; 4-space tabs
(setq-default tab-width 4)

(setq gnutls-algorithm-priority "NORMAL:-VERS-TLS1.3")

;; Useful for https://github.com/dunn/company-emoji
;; https://www.reddit.com/r/emacs/comments/8ph0hq/i_have_converted_from_the_mac_port_to_the_ns_port/
;; not tested with emacs26 (requires a patched Emacs version for multi-color font support)
(if (version< "27.0" emacs-version)
    (set-fontset-font
     "fontset-default" 'unicode "Apple Color Emoji" nil 'prepend)
  (set-fontset-font
   t 'symbol (font-spec :family "Apple Color Emoji") nil 'prepend))

;;; Code:
;; Adding MELPA to package archives
(require 'package)
(let* ((no-ssl (and (memq system-type '(windows-nt ms-dos))
                    (not (gnutls-available-p))))
       (proto (if no-ssl "http" "https")))
  (when no-ssl
    (warn "\
Your version of Emacs does not support SSL connections,
which is unsafe because it allows man-in-the-middle attacks.
There are two things you can do about this warning:
1. Install an Emacs version that does support SSL and be safe.
2. Remove this warning from your init file so you won't see it again."))

  ;; Use MELPA archive
  (add-to-list 'package-archives '("melpa" . "https://melpa.org/packages/") t)

  (when (< emacs-major-version 24)
    ;; For important compatibility libraries like cl-lib
    (add-to-list 'package-archives
		 (cons
		  "gnu"
		  (concat proto "://elpa.gnu.org/packages/")))
    )
  )
(package-initialize)

;; Install straight.el
(defvar bootstrap-version)
(let ((bootstrap-file
       (expand-file-name "straight/repos/straight.el/bootstrap.el" user-emacs-directory))
      (bootstrap-version 5))
  (unless (file-exists-p bootstrap-file)
    (with-current-buffer
        (url-retrieve-synchronously
         "https://raw.githubusercontent.com/raxod502/straight.el/develop/install.el"
         'silent 'inhibit-cookies)
      (goto-char (point-max))
      (eval-print-last-sexp)))
  (load bootstrap-file nil 'nomessage))

;; Ensure use-package is installed
(straight-use-package 'use-package)

;; This is only needed once, near the top of the file
(eval-when-compile
  (require 'use-package))

;; Keep auto-save/backup files separate from source code:  https://github.com/scalameta/metals/issues/1027
(setq backup-directory-alist `((".*" . ,temporary-file-directory))
      auto-save-file-name-transforms `((".*" ,temporary-file-directory t)))

(use-package straight
  ;; Freeze after installing new packages.
  :hook (straight-vc-git-post-clone-hook . straight-freeze-versions)
  )

;; return to last place in file
(use-package saveplace
  :straight t
  :defer 1
  :config
  (save-place-mode)
  )

(desktop-save-mode 1)
(setq desktop-path '("."))
;; desktop+ for enhancing desktop-save-mode
(use-package desktop+
  :straight (:host github :repo "ffevotte/desktop-plus" :files ("*.el"))
  )


;; auto-revert buffer if changed outside
(use-package autorevert
  :defer 1
  :diminish auto-revert-mode
  :config
  ;; Reverts buffers automatically when files are changed externally.
  (global-auto-revert-mode t)
  )

;; 3rd-party packages

;; Emacs startup profiler
(use-package esup
  :straight t
  :defer 2
  :config
  (setq esup-depth 0)
  ;; To use MELPA Stable use ":pin melpa-stable",
  :pin melpa)

;; Restart Emacs from inside Emacs with `M-x restart-emacs`
(use-package restart-emacs
  :straight t
  :defer 2)

;; use-package-ensure-system-package
;; provides way to define system package dependencies for Emacs packages
(use-package use-package-ensure-system-package
  :straight t
  :defer 2)

;; delight
;; hides modeline displays
(use-package delight
  :straight t
  :defer 2)
(require 'delight)                ;; if you use :delight
(require 'bind-key)                ;; if you use any :bind variant

;; use-package-chords
;; allows chord bindings
(use-package use-package-chords
  :straight t
  :config
  (key-chord-mode 1)
  )

;; set PATH using exec-path-from-shell package
(use-package exec-path-from-shell
  :straight t
  :config
  (setq exec-path-from-shell-check-startup-files nil)
  (exec-path-from-shell-initialize)
  )

;; magit
(use-package magit
  :straight t
  :bind (("C-x g" . magit-status)
         ("C-x G" . magit-status-with-prefix)
		 ("C-c g" . magit-file-dispatch))
  :config
  ;; Show commit signature information when visiting a commit
  ;; See https://emacs.stackexchange.com/a/72887/17419
  (put 'magit-revision-mode 'magit-diff-default-arguments
       `("--show-signature" ,@(get 'magit-diff-mode 'magit-diff-default-arguments)))
  
  (setq auth-sources '("~/.authinfo"))
  (setq magit-diff-refine-hunk (quote all))
  (setq magit-display-buffer-function
	#'magit-display-buffer-fullframe-status-v1)
  ;; magit transient levels, allows GPG option to be visible
  (setq transient-default-level 5)
  ;; Refresh magit buffer on save. This must only be evaluated when in
  ;; magit-mode. DO NOT refactor with use-package `:hook` directive.
  (with-eval-after-load 'magit-mode
    (add-hook 'after-save-hook 'magit-after-save-refresh-status t))
  (put 'magit-todos-exclude-globs 'safe-local-variable #'seqp)
  )

;; Disable emacs native VC (it just slows things down, magit is better)
(setq vc-handled-backends nil)

;; Interact with forges directly
(use-package forge
  :straight t
  :after magit
  :defer 2
  )

(use-package hl-todo
  :straight t
  :defer 2
  :config
  (global-hl-todo-mode 1))

;; git-modes
(use-package git-modes
  :straight t
  )

;; which-key shows all available keybindings
(use-package which-key
  :delight
  :straight t
  :defer 2
  :init
  (which-key-mode)
  )

;; smartparens
(use-package smartparens
  :delight
  :straight t
  :config
  ;; (setq-default sp-escape-quotes-after-insert nil) ; Don't escape quotes
  (smartparens-global-mode t)
  (setq sp-highlight-pair-overlay nil)
  (sp-local-pair 'c-mode "{" nil :post-handlers '(:add my-open-block-brace-mode))
  )

;; browse-at-remote
(use-package browse-at-remote
  :straight t
  :defer 2
  :bind
  (:map global-map
		("C-c g g" browse-at-remote)
		)
  )

;; treemacs
(use-package treemacs
  :straight t
  :defer 2
  :init
  (with-eval-after-load 'winum
    (define-key winum-keymap (kbd "M-0") #'treemacs-select-window))
  :config
  (progn
    (setq treemacs-collapse-dirs                 (if treemacs-python-executable 3 0)
          treemacs-deferred-git-apply-delay      0.5
          treemacs-directory-name-transformer    #'identity
          treemacs-display-in-side-window        t
          treemacs-eldoc-display                 t
          treemacs-file-event-delay              5000
          treemacs-file-extension-regex          treemacs-last-period-regex-value
          treemacs-file-follow-delay             0.2
          treemacs-file-name-transformer         #'identity
          treemacs-follow-after-init             t
          treemacs-git-command-pipe              ""
          treemacs-goto-tag-strategy             'refetch-index
          treemacs-indentation                   2
          treemacs-indentation-string            " "
          treemacs-is-never-other-window         nil
          treemacs-max-git-entries               5000
          treemacs-missing-project-action        'ask
          treemacs-move-forward-on-expand        nil
          treemacs-no-png-images                 nil
          treemacs-no-delete-other-windows       t
          treemacs-project-follow-cleanup        nil
          treemacs-persist-file                  (expand-file-name ".cache/treemacs-persist" user-emacs-directory)
          treemacs-position                      'left
          treemacs-recenter-distance             0.1
          treemacs-recenter-after-file-follow    nil
          treemacs-recenter-after-tag-follow     nil
          treemacs-recenter-after-project-jump   'always
          treemacs-recenter-after-project-expand 'on-distance
          treemacs-show-cursor                   nil
          treemacs-show-hidden-files             t
          treemacs-silent-filewatch              nil
          treemacs-silent-refresh                nil
          treemacs-sorting                       'alphabetic-asc
          treemacs-space-between-root-nodes      t
          treemacs-tag-follow-cleanup            t
          treemacs-tag-follow-delay              1.5
          treemacs-user-mode-line-format         nil
          treemacs-user-header-line-format       nil
          treemacs-width                         35)

    ;; The default width and height of the icons is 22 pixels. If you are
    ;; using a Hi-DPI display, uncomment this to double the icon size.
    ;;(treemacs-resize-icons 44)
    (treemacs-follow-mode t)
    (treemacs-filewatch-mode t)
    (treemacs-fringe-indicator-mode t)
    (pcase (cons (not (null (executable-find "git")))
                 (not (null treemacs-python-executable)))
      (`(t . t)
       (treemacs-git-mode 'deferred))
      (`(t . _)
       (treemacs-git-mode 'simple))))
  :bind
  (:map global-map
        ("M-t"       . treemacs-select-window)
        ("C-x t 1"   . treemacs-delete-other-windows)
        ("C-x t t"   . treemacs)
        ("C-x t B"   . treemacs-bookmark)
        ("C-x t C-t" . treemacs-find-file)
        ("C-x t M-t" . treemacs-find-tag)))

(use-package treemacs-projectile
  :after treemacs projectile
  :straight t
  :defer 2)

(use-package treemacs-icons-dired
  :after treemacs dired
  :straight t
  :defer 2
  :config (treemacs-icons-dired-mode))

(use-package treemacs-magit
  :after treemacs magit
  :straight t
  :defer 2)

(use-package treemacs-persp
  :after treemacs persp-mode
  :straight t
  :defer 2
  :config (treemacs-set-scope-type 'Perspectives))

(use-package copilot
  :after company
  :config
  (delq 'company-preview-if-just-one-frontend company-frontends)
  ;; Kludge - the version is a hardlink pointing to the binary
  (setq copilot-node-executable "17.9.1")
  (unless (executable-find copilot-node-executable)
    nil
    (setq copilot-node-command (format "nodenv install %s && ln -sfv \"$HOME/.nodenv/versions/%s/bin/node\" \"$HOME/.local/bin/%s\""
                                       copilot-node-executable
                                       copilot-node-executable
                                       copilot-node-executable
                                       ))
    (async-shell-command copilot-node-command)
    )
  :straight (:host github :repo "zerolfx/copilot.el" :files ("dist" "*.el"))
  :bind (("C-c e" . copilot-mode)
         :map copilot-completion-map
              ("C-S-<tab>" . #'copilot-accept-completion-by-line)
              ("C-<tab>" . #'copilot-accept-completion)
              ("C-g" . #'copilot-clear-overlay)
              ("C-n" . #'copilot-next-completion)
              ("C-p" . #'copilot-previous-completion)
              )
  )

(use-package logview
  :straight t
  :delight "🪵"
  )

(use-package codespaces
  :straight (:host github :repo "patrickt/codespaces.el" :files ("dist" "*.el"))
  :ensure-system-package gh
  :config
  (codespaces-setup)
  (setq codespaces-default-directory "/workspaces")
  )

(defun my-eglot-organize-imports ()
  "Add an organizeImports code action to eglot."
  (interactive)
  (eglot-code-actions nil nil "source.organizeImports" t))

(use-package eglot
  :straight t
  :hook
  ((before-save . my-eglot-organize-imports)
   (before-save . eglot-format-buffer)
   (go-mode . eglot-ensure)
   (python-mode . eglot-ensure)
   (shell-mode . eglot-ensure)
   (powershell-mode . eglot-ensure)
   (yaml-mode . eglot-ensure)
   (dockerfile-mode . eglot-ensure)
   (ruby-mode . eglot-ensure)
   )
  :config
  ;; (let* ((pses-bundle (getenv "PSES_BUNDLE_PATH"))
  ;; 		 ))
  ;; (add-to-list 'eglot-server-programs
  ;; 			   `(powershell-mode
  ;; 				 . ("pwsh" "-NoLogo" "-NoProfile" "-Command" "$PSES_BUNDLE_PATH/PowerShellEditorServices/Start-EditorServices.ps1 -BundledModulesPath $PSES_BUNDLE_PATH -LogPath $SESSION_TEMP_PATH/logs.log -SessionDetailsPath $SESSION_TEMP_PATH/session.json -FeatureFlags @() -AdditionalModules @() -HostName 'My Client' -HostProfileId 'myclient' -HostVersion 1.0.0 -Stdio -LogLevel Normal")))
  )

(use-package eldoc-box
  :straight t
  :hook
  (eglot-managed-mode . eldoc-box-hover-at-point-mode)
  )

(use-package powershell
  :straight t
  :delight " "
  :hook
  (powershell-mode . (lambda () (indent-tabs-mode -1)))
  )

(use-package csharp-mode
  :straight t
  :delight ""
  )

(use-package css-mode
  :delight scss-mode ""
  :delight ""
  )

(use-package sh-script
  :delight "🐚"
  )

(use-package shell
  :delight "🐚"
  )

(use-package mhtml-mode
  :delight ""
  )

(use-package prog-mode
  :delight typescript-mode ""
  :delight js-mode ""
  :hook
  (prog-mode . display-fill-column-indicator-mode)
  (prog-mode . company-mode)
  )

;; python-black
(use-package python-black
  :delight python-black-on-save-mode "⚫️"
  :straight t
  :hook
  (python-mode . python-black-on-save-mode)
  :init
  (put 'python-black-command 'safe-local-variable #'stringp)
  (put 'python-black-extra-args 'safe-local-variable #'stringp)
  (put 'python-black-on-save-mode 'safe-local-variable #'booleanp)
  )

;; poetry
(use-package poetry
  :straight t
  ;; :init
  ;; imperfect tracking strategy causes lags in builds
  ;; (setq poetry-tracking-strategy 'switch-buffer)
  :hook
  ;; activate poetry-tracking-mode when python-mode is active
  (python-mode . poetry-tracking-mode)
  )

;; disable pyvenv menu
(setq pyvenv-mode nil)
;; end Python configs

(use-package docker
  :ensure t
  :delight ""
  :bind ("C-c d" . docker))

;; dockerfile-mode
(use-package dockerfile-mode
  :straight t
  :delight ""
  :mode "Dockerfile")

;; go-mode
(use-package go-mode
  :straight t
  :delight ""
  :hook
  (before-save . eglot-format)
  )

(use-package ruby-mode
  :delight " "
  :ensure-system-package (solargraph . "gem install solargraph")
  :mode ("\\Brewfile\\'")
  :interpreter "ruby"
  :hook
  (ruby-mode . (lambda ()
                 (add-hook 'before-save-hook #'eglot-format nil t)))
)

;; json-mode
(use-package json-mode
  :straight t
  :ensure-system-package (vscode-json-languageserver . "npm i -g vscode-json-languageserver")
  :defer t)

;; LaTeX configs
(use-package tex-mode
  :ensure auctex
  :config
  (require 'font-latex)
  :init
  ;; Activate nice interface between RefTeX and AUCTeX
  (setq reftex-plug-into-AUCTeX t)
  :hook
  ;; Turn on RefTeX in AUCTeX
  (LaTeX-mode . turn-on-reftex)
  )

(use-package auctex-latexmk
  :straight t
  :config
  (setq auctex-latexmk-inherit-TeX-PDF-mode t)
  (setq TeX-command-list
	 (quote
	  (("latexmk -xelatex"
	    "latexmk %(-xelatex)%S%(mode) %(file-line-error) %(extraopts) %t"
	    TeX-run-latexmk nil
	    (plain-tex-mode latex-mode doctex-mode)
	    :help "Run LatexMk")
	   ("TeX"
	    "%(PDF)%(tex) %(file-line-error) %(extraopts) %`%S%(PDFout)%(mode)%' %t"
	    TeX-run-TeX nil
	    (plain-tex-mode texinfo-mode ams-tex-mode)
	    :help "Run plain TeX")
	   ("LaTeX"
	    "%`%l%(mode)%' %t"
	    TeX-run-TeX nil
	    (latex-mode doctex-mode)
	    :help "Run LaTeX")
	   ("Makeinfo"
	    "makeinfo %(extraopts) %t"
	    TeX-run-compile nil
	    (texinfo-mode)
	    :help "Run Makeinfo with Info output")
	   ("Makeinfo HTML"
	    "makeinfo %(extraopts) --html %t"
	    TeX-run-compile nil
	    (texinfo-mode)
	    :help "Run Makeinfo with HTML output")
	   ("AmSTeX"
	    "amstex %(PDFout) %(extraopts) %`%S%(mode)%' %t"
	    TeX-run-TeX nil
	    (ams-tex-mode)
	    :help "Run AMSTeX")
	   ("ConTeXt"
	    "%(cntxcom) --once --texutil %(extraopts) %(execopts)%t"
	    TeX-run-TeX nil
	    (context-mode)
	    :help "Run ConTeXt once")
	   ("ConTeXt Full"
	    "%(cntxcom) %(extraopts) %(execopts)%t"
	    TeX-run-TeX nil
	    (context-mode)
	    :help "Run ConTeXt until completion")
	   ("BibTeX"
	    "bibtex %s"
	    TeX-run-BibTeX nil t
	    :help "Run BibTeX")
	   ("Biber"
	    "biber %s"
	    TeX-run-Biber nil t
	    :help "Run Biber")
	   ("View"
	    "%V"
	    TeX-run-discard-or-function t t
	    :help "Run Viewer")
	   ("Print" "%p"
	    TeX-run-command t t
	    :help "Print the file")
	   ("Queue"
	    "%q"
	    TeX-run-background nil t
	    :help "View the printer queue"
	    :visible TeX-queue-command)
	   ("File"
	    "%(o?)dvips %d -o %f "
	    TeX-run-dvips t t
	    :help "Generate PostScript file")
	   ("Dvips"
	    "%(o?)dvips %d -o %f "
	    TeX-run-dvips nil t
	    :help "Convert DVI file to PostScript")
	   ("Dvipdfmx"
	    "dvipdfmx %d"
	    TeX-run-dvipdfmx nil t
	    :help "Convert DVI file to PDF with dvipdfmx")
	   ("Ps2pdf"
	    "ps2pdf %f"
	    TeX-run-ps2pdf nil t
	    :help "Convert PostScript file to PDF")
	   ("Glossaries"
	    "makeglossaries %s"
	    TeX-run-command nil t
	    :help "Run makeglossaries to create glossary file")
	   ("Index"
	    "makeindex %s"
	    TeX-run-index nil t
	    :help "Run makeindex to create index file")
	   ("upMendex"
	    "upmendex %s"
	    TeX-run-index t t
	    :help "Run upmendex to create index file")
	   ("Xindy"
	    "texindy %s"
	    TeX-run-command nil t
	    :help "Run xindy to create index file")
	   ("Check"
	    "lacheck %s"
	    TeX-run-compile nil
	    (latex-mode)
	    :help "Check LaTeX file for correctness")
	   ("ChkTeX"
	    "chktex -v6 %s"
	    TeX-run-compile nil
	    (latex-mode)
	    :help "Check LaTeX file for common mistakes")
	   ("Spell"
	    "(TeX-ispell-document \"\")"
	    TeX-run-function nil t
	    :help "Spell-check the document")
	   ("Clean"
	    "TeX-clean"
	    TeX-run-function nil t
	    :help "Delete generated intermediate files")
	   ("Clean All"
	    "(TeX-clean t)"
	    TeX-run-function nil t
	    :help "Delete generated intermediate and output files")
	   ("Other"
	    ""
	    TeX-run-command t t
	    :help "Run an arbitrary command"))))
  )

;; Clang stuff
;; clang-format
(use-package clang-format
  :straight t
  )
(defun clang-format-on-save ()
  "Format buffer with clang-format on save."
  (add-hook 'before-save-hook #'clang-format-buffer nil 'local))
(add-hook 'c++-mode-hook 'clang-format-on-save)
(add-hook 'c-mode-hook 'clang-format-on-save)
;; c-mode hook
(add-hook 'c-mode-hook 'company-mode)
;; Mode-specific configs
(add-hook 'c-mode-hook
	  (lambda ()
	    ;; Bind format command to a key
	    (local-set-key "\C-cf" 'clang-format-region)
	    ;; C indentation style
	    (c-set-style "linux")
	    ;; newline in {} braces
	    )
	  )

(defun my-open-block-brace-mode (id action context)
  "Add newline after a brace and position point.  ARG ID, ARG ACTION, and ARG CONTEXT are needed for this function to be used by smartparens."
  (when (eq action 'insert)
    (newline)
    (newline)
    (indent-according-to-mode)
    (previous-line)
    (indent-according-to-mode)))

;; rg.el (ripgrep tool)
(use-package rg
  :straight t
  :init
  (rg-enable-menu)
  :config
  (setq rg-default-alias-fallback "everything")
  )

;; dumb-jump
(use-package dumb-jump
  :straight t
  :config
  ;; dumb-jump-go (C-M-g) jumps to the definition of thing under point
  (dumb-jump-mode)
  )

;; yasnippet
(use-package yasnippet
  :delight yas-minor-mode "🆈"
  :straight t
  :defer 2
  )
(use-package yasnippet-snippets
  :after yasnippet
  :straight t
  :defer 2
  :config
  (yas-global-mode t)
  )

;; flycheck
(use-package flycheck
  :delight "✅"
  :straight t
  :config
  (global-flycheck-mode t)
  (global-set-key (kbd "C-c n") 'flycheck-next-error)
  (global-set-key (kbd "C-c p") 'flycheck-previous-error)
  (put 'flycheck-python-mypy-executable 'safe-local-variable #'stringp)
  )

(use-package all-the-icons
  :straight t
  :defer 2)

;; company
(use-package company
  :straight t
  :delight "⏭"
  :config
  ;; company-mode global settings
  ;; Show suggestions after entering one character.
  (setq company-minimum-prefix-length 1)
  (setq company-idle-delay 0.05)
  ;; Use tab key to cycle through suggestions.
  ;; ('tng' means 'tab and go')
  ;; disabled because it doesn't expand function signatures >:|
  ;; (company-tng-configure-default)
  ;; (company-tng--supress-post-completion nil)
  )

;; company-box is a Company frontend with icons
(use-package company-box
  :delight
  :after company
  :straight t
  :hook (company-mode . company-box-mode)
  :config
  (set-face-background 'company-tooltip "#555555")
  (set-face-background 'company-tooltip-selection "#999999")
  )

;; company-quickhelp
(use-package company-quickhelp
  :after company
  :straight t
  :init
  (company-quickhelp-mode)
  (define-key company-active-map (kbd "C-c h") #'company-quickhelp-manual-begin)
  )

;; multiple-cursors
(use-package multiple-cursors
  :straight t
  :bind (
	 ("C-c m c" . mc/edit-lines)
	 ("C->" . mc/mark-next-like-this)
	 ("C-<" . mc/mark-previous-like-this)
	 ("C-c C-<" . mc/mark-all-like-this)
	 ("s-<mouse-1>" . mc/add-cursor-on-click)
	 )
  )

;; projectile
(use-package projectile
  :straight t
  :delight
  :defer 2
  :init
  (projectile-mode t)
  :bind (
	 ("s-p" . projectile-command-map)
	 )
  )

;; toml-mode
(use-package toml-mode
  :straight t
  :defer t)

;; yaml-mode
(use-package yaml-mode
  :straight t
  :ensure-system-package (yaml-language-server . "npm i -g yaml-language-server")
  :mode ("\\.yml\\'"
         "\\.yaml\\'")
  )

;; highlight-indent-guides.el
(use-package highlight-indent-guides
  :delight
  :straight t
  :init
  :custom
  (highlight-indent-guides-responsive "stack" "Highlight ancestral guides of current guide")
  (highlight-indent-guides-delay 0 "Remove indentation highlight delay")
  (highlight-indent-guides-auto-odd-face-perc 10)
  (highlight-indent-guides-auto-even-face-perc 15)
  :hook
  (prog-mode . highlight-indent-guides-mode)
  (yaml-mode . highlight-indent-guides-mode)
  )

;; emacs-lisp
(use-package emacs-lisp
  :defer t)

;; xkcd
(use-package xkcd
  :straight t
  :defer 2)

(use-package typescript-mode
  :straight t
  :mode ("\\.ts\\'"
         "\\.tsx\\'"
         "\\.vue\\'"
         )
  )

;; markdown-mode
(use-package markdown-mode
  :delight markdown-mode ""
  :straight t
  :mode ("\\.md\\'"
         "\\.mkd\\'"
         "\\.markdown\\'"
	 "\\.mdx\\'"
	 )
  )

;; shfmt.el for autoformatting shell scripts
(use-package shfmt
  :straight t
  :hook (
	 (sh-mode . shfmt-on-save-mode)
	 )
  )

;; end of 3rd-party packages
(put 'upcase-region 'disabled nil)

;; Use SSH as default tramp method
(setq tramp-default-method "ssh")

;; Disable tramp's default ControlMaster settings
(setq tramp-ssh-controlmaster-options "")

;; Use remote host's local path
(add-to-list 'tramp-remote-path 'tramp-own-remote-path)

;; set option key as Meta
(setq mac-option-modifier 'meta)

;; No scrollbars!
(scroll-bar-mode -1)

;; set scratch buffer mode to Markdown mode
(setq initial-major-mode 'markdown-mode)
(setq initial-scratch-message "\
This buffer is for notes you don't want to save, with Markdown formatting.
If you want to create a file, visit that file with C-x C-f,
then enter the text in that file's own buffer.")

;; enable autocomplete in emacs-lisp-mode
(add-hook 'emacs-lisp-mode-hook
	  'company-mode
	  )

;; set window size
;; 25 is the height of the menu bar in pixels.
(defvar frame-height (/ (- (x-display-pixel-height) 25)
						(frame-char-height)))

(setq initial-frame-alist
      `((width . 84) (height . ,frame-height)))
(setq default-frame-alist
      `((width . 84) (height . ,frame-height)))

;; converting region to lowercase
(put 'downcase-region 'disabled nil)

;; use Common Lisp
(require 'cl-lib)
(eval-when-compile (require 'cl-lib))

;; Fancy titlebar for macOS
(add-to-list 'default-frame-alist '(ns-transparent-titlebar . t))
(add-to-list 'default-frame-alist '(ns-appearance . dark))
(setq ns-use-proxy-icon nil)

;; sgml
(setq sgml-quick-keys 'indent)

;; delete all whitespace when untabifying
(setq backward-delete-char-untabify-method 'hungry)

;; makefile-mode
;; always use GNU make mode in makefile mode
(add-hook 'makefile-mode-hook
	  'makefile-gmake-mode)

;; standard selection-highlighting behaviour
(setq transient-mark-mode t)

;; typed text replaces what's selected
(delete-selection-mode 1)

;; always display line numbers
(when
    (version<= "26.0.50" emacs-version )
  (global-display-line-numbers-mode))
(add-hook 'term-mode-hook (lambda () (display-line-numbers-mode -1)))
(when (not 'term-mode-hook)
  (if
      (version<= "26.0.50" emacs-version)
      (display-line-numbers-mode t)
    (linum-mode t)
    )
  )

;; disable in-window menu bar
(tool-bar-mode -1)

;; Show parens instantly
(setq show-paren-delay 0)
(show-paren-mode 1)

;; macOS command key (s for super) keybinds
(global-set-key (kbd "s-<left>") 'beginning-of-line)
(global-set-key (kbd "s-<right>") 'end-of-line)
(global-set-key (kbd "s-<up>") 'beginning-of-buffer)
(global-set-key (kbd "s-<down>") 'end-of-buffer)
(global-set-key (kbd "s-<kp-delete>") 'kill-word)
(global-set-key (kbd "s-<backspace>") 'kill-word)
(global-set-key (kbd "s-/") 'comment-line)

;; other keybinds
(global-set-key (kbd "M-<kp-delete>") 'kill-word)
(global-set-key (kbd "C-x C-b") 'ibuffer)

;; Incremental file-opening
(ido-mode 1)

;; Word count
(defun wc () "Return a word count of the current buffer." (interactive) (shell-command (concat "wc " buffer-file-name)))
(global-set-key "\C-cw" 'wc)


;; FOR GPG
(require 'epa-file)
(setq epa-file-select-keys nil)

;; Always show column numbers
(column-number-mode)

;; ESS config
(setq ess-use-company t)
(setq ess-eldoc-show-on-symbol t)
(add-hook 'ess-mode-hook
	  (lambda ()
	    (setq company-mode t)
	    )
	  )

;; gdb settings
(setq gud-gdb-command-name "gdb --annotate=1")

;; List variable value pairs that are considered safe
(setq safe-local-variable-values
      (quote (
	      ;; use lexical binding when evaluating code
	      (lexical-binding . t)
	      )
	     )
      )

;; mark custom theme as safe
(setq custom-enabled-themes '(jidiculous-dark))
(setq custom-safe-themes
      '("7011ad67608e4ff9aa40db2c1ab063999cf68db507466d7d5cdbac6e8cd2c1ae"
	"884b27b0905eb742baf6fdd013dfb28d01c59701e3ceaaa727a380c86309e206"
	"8ab0d715ae6fbfc75c924a239697f91243fe487d96c8e3645453c43291b10364"
	default
	)
      )

;; set ansi color faces
(setq ansi-color-faces-vector
   [default default default italic underline success warning error])
(setq ansi-color-names-vector
   ["#212526" "#ff4b4b" "#b4fa70" "#fce94f" "#729fcf" "#e090d7" "#8cc4ff" "#eeeeec"])

;; custom theme path
;; (setq custom-safe-themes t)
(add-to-list 'custom-theme-load-path "~/dotfiles/")
(load-theme 'jidiculous-dark t)

;; set dir local vars as safe
(setq safe-local-variable-values
      '((python-black-extra-args "-S")
	(eval remove-hook 'elpy-mode-hook 'elpy-format-on-save t)
	(lexical-binding . t)))

;; Make gc pauses faster by decreasing the threshold.
(setq gc-cons-threshold (* 2 1000 1000))
;;; init.el ends here
(custom-set-variables
 ;; custom-set-variables was added by Custom.
 ;; If you edit it by hand, you could mess it up, so be careful.
 ;; Your init file should contain only one such instance.
 ;; If there is more than one, they won't work right.
 '(warning-suppress-types '((comp))))
(custom-set-faces
 ;; custom-set-faces was added by Custom.
 ;; If you edit it by hand, you could mess it up, so be careful.
 ;; Your init file should contain only one such instance.
 ;; If there is more than one, they won't work right.
 )
