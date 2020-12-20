;;; init.el --- Johanan's Emacs config
;;; Commentary:
;;; Load it from .emacs with `(load "path/to/init.el")`

(defconst emacs-start-time (current-time))

;; increase gc-cons-threshold to 100mb
(setq gc-cons-threshold 100000000)

;; increase amount of data Emacs reads from process to 1mb
(setq read-process-output-max (* 1024 1024))

;; confirm before quitting
(setq confirm-kill-emacs 'y-or-n-p)

;; no welcome screen
(setq inhibit-startup-screen t)

;; no large file warning
(setq large-file-warning-threshold nil)

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
  (add-to-list 'package-archives (cons
				  "melpa"
				  (concat proto "://melpa.org/packages/")
				  )
	       t)

  (when (< emacs-major-version 24)
    ;; For important compatibility libraries like cl-lib
    (add-to-list 'package-archives
		 (cons
		  "gnu"
		  (concat proto "://elpa.gnu.org/packages/")))
    )
  )
(package-initialize)

;; Ensure use-package is installed
(when (not (package-installed-p 'use-package))
  (package-refresh-contents)
  (package-install 'use-package))

;; This is only needed once, near the top of the file
(eval-when-compile
  (require 'use-package))

;; Keep auto-save/backup files separate from source code:  https://github.com/scalameta/metals/issues/1027
(setq use-package-always-ensure t
      backup-directory-alist `((".*" . ,temporary-file-directory))
      auto-save-file-name-transforms `((".*" ,temporary-file-directory t)))

;; 3rd-party packages

;; Restart Emacs from inside Emacs with `M-x restart-emacs`
(use-package restart-emacs
  :defer t)

;; use-package-ensure-system-package
;; provides way to define system package dependencies for Emacs packages
(use-package use-package-ensure-system-package
  :ensure t)

;; delight
;; hides modeline displays
(use-package delight
  :ensure t)
(require 'delight)                ;; if you use :delight
(require 'bind-key)                ;; if you use any :bind variant

;; use-package-chords
;; allows chord bindings
(use-package use-package-chords
  :ensure t
  :config
  (key-chord-mode 1)
  )

;; set PATH using exec-path-from-shell package
(use-package exec-path-from-shell
  :if (memq window-system '(mac ns))
  :ensure t
  :config
  (setq exec-path-from-shell-check-startup-files nil)
  (exec-path-from-shell-initialize)
  )

;; magit
(use-package magit
  :ensure t
  :bind (("C-x g" . magit-status)
         ("C-x G" . magit-status-with-prefix)
	 ("C-c g" . magit-file-dispatch))
  :config
  ;; Disable emacs native VC (it just slows things down, magit is better)
  (setq vc-handled-backends nil)
  (setq magit-display-buffer-function
	#'magit-display-buffer-fullframe-status-v1)
  ;; magit transient levels, allows GPG option to be visible
  (setq transient-default-level 5)
  ;; Refresh magit buffer on save. This must only be evaluated when in
  ;; magit-mode. DO NOT refactor with use-package `:hook` directive.
  (with-eval-after-load 'magit-mode
  (add-hook 'after-save-hook 'magit-after-save-refresh-status t))
  )

;; Interact with forges directly
(use-package forge
  :ensure t
  :after magit
  )

;; Magit todos
(use-package magit-todos
  :requires (magit)
  :hook (magit-mode . magit-todos-mode)
  :custom
  (magit-todos-exclude-globs '("**/node_modules/**"))
  :init
  (unless (executable-find "nice") ; don't break Magit on systems that don't have `nice'
    (setq magit-todos-nice nil)))

(use-package hl-todo
  :config
  (global-hl-todo-mode +1))

;; gitattributes-mode
(use-package gitattributes-mode
  :ensure t
  )

;; gitconfig-mode
(use-package gitconfig-mode
  :ensure t
  )

;; gitignore-mode
(use-package gitignore-mode
  :ensure t
  )

;; which-key shows all available keybindings
(use-package which-key
  :delight
  :ensure t
  :init
  (which-key-mode)
  )

;; smartparens
(use-package smartparens
  :delight
  :ensure t
  :config
  ;; (setq-default sp-escape-quotes-after-insert nil) ; Don't escape quotes
  (smartparens-global-mode t)
  (setq sp-highlight-pair-overlay nil)
  (sp-local-pair 'c-mode "{" nil :post-handlers '(:add my-open-block-c-mode))
  )

;; column-enforce-mode
;; highlights text extending beyond a certain column
(use-package column-enforce-mode
  :ensure t
  :init
  ;; activate mode in all prog-mode
  :hook
  (prog-mode . column-enforce-mode)
  )

;; treemacs
(use-package treemacs
  :ensure t
  :defer t
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

(use-package treemacs-evil
  :after treemacs evil
  :ensure t)

(use-package treemacs-projectile
  :after treemacs projectile
  :ensure t)

(use-package treemacs-icons-dired
  :after treemacs dired
  :ensure t
  :config (treemacs-icons-dired-mode))

(use-package treemacs-magit
  :after treemacs magit
  :ensure t)

(use-package treemacs-persp
  :after treemacs persp-mode
  :ensure t
  :config (treemacs-set-scope-type 'Perspectives))

;; lsp-mode configs
(use-package lsp-mode
  :ensure t
  :init
  ;; set prefix for lsp-command-keymap (few alternatives - "C-l", "C-c l")
  (setq lsp-keymap-prefix "C-c l")
  :custom
  (lsp-auto-guess-root +1)
  :config
  (lsp-enable-imenu)
  (setq lsp-prefer-flymake nil)
  (setq lsp-headerline-breadcrumb-enable t)
  :hook (;; replace XXX-mode with concrete major-mode(e. g. python-mode)
         (python-mode . lsp-deferred)
	 (scala-mode . lsp-deferred)
         ;; if you want which-key integration
         (lsp-mode . lsp-enable-which-key-integration)
	 (lsp-mode . lsp-lens-mode)
	 (lsp-after-open . 'lsp-enable-imenu)
	 )
  :commands (lsp lsp-deferred))

;; optionally
(use-package lsp-ui
  :after lsp-mode
  :ensure t
  :commands lsp-ui-mode
  :hook
  (lsp-mode . lsp-ui-mode)
  :config
  ;; (setq lsp-ui-doc-use-webkit t) 	;very buggy webkit doc windows
  )

(use-package lsp-treemacs
  :after lsp-mode treemacs
  :ensure t
  :commands lsp-treemacs-errors-list
  )

(use-package company-lsp)

;; Use the Debug Adapter Protocol for running tests and debugging
(use-package posframe
  :ensure t
  ;; Posframe is a pop-up tool that must be manually installed for dap-mode
  )


;; optionally if you want to use debugger
(use-package dap-mode
  :after lsp-mode
  :ensure t
  :hook
  (lsp-mode . dap-mode)
  (lsp-mode . dap-ui-mode)
  )
;; (use-package dap-LANGUAGE) to load the dap adapter for your language

;; lsp Python
(use-package lsp-python-ms
  :delight python-mode "🐍"
  :after poetry
  :ensure t
  :init
  (setq lsp-python-ms-auto-install-server t)
  :config
  (put 'lsp-python-ms-python-executable 'safe-local-variable #'stringp)
  ;; attempt to activate Poetry env first
  (when (stringp (poetry-find-project-root))
    (poetry-venv-workon)
    )
  :hook
  (
   (python-mode . (lambda ()
                    (require 'lsp-python-ms)
                    (lsp-deferred)
		    ))
   ;; if .dir-locals exists, read it first, then activate mspyls
   (hack-local-variables . (lambda ()
			     (setq indent-tabs-mode nil)  ; disable tabs
			     (when (derived-mode-p 'python-mode)
			       (require 'lsp-python-ms)
			       (lsp-deferred))
			     ))
   )
  )

;; python-black
(use-package python-black
  :delight python-black-on-save-mode "⚫️"
  :ensure t
  :hook
  (python-mode . python-black-on-save-mode)
  :init
  (put 'python-black-command 'safe-local-variable #'stringp)
  (put 'python-black-extra-args 'safe-local-variable #'stringp)
  (put 'python-black-on-save-mode 'safe-local-variable #'booleanp)
  )

;; dockerfile-mode
(use-package dockerfile-mode
  :mode "Dockerfile")

;; json-mode
(use-package json-mode
  :ensure-system-package (vscode-json-languageserver . "npm i -g vscode-json-languageserver")
  :defer t)

;; Scala configs
;; Enable scala-mode for highlighting, indentation and motion commands
(use-package scala-mode
  :ensure t
  :mode "\\.s\\(cala\\|bt\\)$"
  :hook
  (before-save . lsp-format-buffer)
  )

;; Enable sbt mode for executing sbt commands
(use-package sbt-mode
  :ensure t
  :commands sbt-start sbt-command
  :config
  ;; WORKAROUND: https://github.com/ensime/emacs-sbt-mode/issues/31
  ;; allows using SPACE when in the minibuffer
  (substitute-key-definition
   'minibuffer-complete-word
   'self-insert-command
   minibuffer-local-completion-map)
   ;; sbt-supershell kills sbt-mode:  https://github.com/hvesalai/emacs-sbt-mode/issues/152
   (setq sbt:program-options '("-Dsbt.supershell=false"))
)

;; Add metals backend for lsp-mode
(use-package lsp-metals
  :ensure t
  :config
  (setq lsp-metals-treeview-show-when-views-received t)
  )

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
  :ensure t
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

;; clang-format
(use-package clang-format
  :ensure t
  )
;; Clang stuff
(defun clang-format-on-save ()
  (add-hook 'before-save-hook #'clang-format-buffer nil 'local))
(add-hook 'c++-mode-hook 'clang-format-on-save)
(add-hook 'c-mode-hook 'clang-format-on-save)
;; c-mode hook
(add-hook 'c-mode-hook 'company-mode)
;; Mode-specific configs
(add-hook 'c-mode-hook
	  '(lambda ()
	     ;; Bind format command to a key
	     (local-set-key "\C-cf" 'clang-format-region)
	     ;; C indentation style
	     (c-set-style "linux")
	     ;; newline in {} braces
	     )
	  )

;; newline after a brace and position point
(defun my-open-block-c-mode (id action context)
  (when (eq action 'insert)
    (newline)
    (newline)
    (indent-according-to-mode)
    (previous-line)
    (indent-according-to-mode)))

;; rg (ripgrep tool)
(use-package rg
  :ensure t
  :init
  (rg-enable-default-bindings)
  )

;; dumb-jump
(use-package dumb-jump
  :ensure t
  :config
  ;; dumb-jump-go (C-M-g) jumps to the definition of thing under point
  (dumb-jump-mode)
  )

;; yasnippet
(use-package yasnippet
  :delight yas-minor-mode "🆈"
  :ensure t
  )
(use-package yasnippet-snippets
  :after yasnippet
  :ensure t
  :config
  (yas-global-mode t)
  )

;; flycheck
(use-package flycheck
  :delight "✅"
  :ensure t
  :init
  (global-flycheck-mode t)
  :config
  (global-set-key (kbd "C-c n") 'flycheck-next-error)
  (global-set-key (kbd "C-c p") 'flycheck-previous-error)
  (put 'flycheck-python-mypy-executable 'safe-local-variable #'stringp)
  )

(use-package all-the-icons
  :ensure t)

;; company
(use-package company
  :ensure t
  :delight "⏭"
  :config
  ;; company-mode global settings
  ;; Show suggestions after entering one character.
  (setq company-minimum-prefix-length 1)
  (setq company-idle-delay 0.5)
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
  :ensure t
  :hook (company-mode . company-box-mode)
  :config
  (set-face-background 'company-tooltip "#555555")
  (set-face-background 'company-tooltip-selection "#999999")
  )

;; company-quickhelp
(use-package company-quickhelp
  :after company
  :ensure t
  :init
  (company-quickhelp-mode)
  (define-key company-active-map (kbd "C-c h") #'company-quickhelp-manual-begin)
  )

;; multiple-cursors
(use-package multiple-cursors
  :ensure t
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
  :ensure t
  :delight
  :init
  (projectile-mode t)
  :bind (
	 ("s-p" . projectile-command-map)
	 )
  )

;; toml-mode
(use-package toml-mode
  :defer t)

;; yaml-mode
(use-package yaml-mode
  :ensure t
  :ensure-system-package (yaml-language-server . "npm i -g yaml-language-server")
  :mode ("\\.yml\\'"
         "\\.yaml\\'")
  )

;; elpy
(use-package elpy
  :after poetry
  :ensure t
  :config
  ;; (elpy-enable)
  (setq elpy-rpc-virtualenv-path 'current)
  ;; elpy format on save
  ;; formats using whatever formatter is installed. if file is in a Poetry
  ;; project it will use the formatter installed in that project. Else it
  ;; defaults to black, which is installed systemwide.
  (defun elpy-format-on-save ()
    (add-hook 'before-save-hook #'elpy-format-code nil t)
    )
  ;; clear Elpy keybindings for shifting lines
  (cl-dolist
      (key '("M-<up>" "M-<down>" "M-<left>" "M-<right>"))
    (define-key elpy-mode-map (kbd key) nil)
    )
  ;; Remap Elpy keybindings for shifting lines
  (define-key elpy-mode-map (kbd "M-p") 'elpy-nav-move-line-or-region-up)
  (define-key elpy-mode-map (kbd "M-n") 'elpy-nav-move-line-or-region-down)
  (define-key elpy-mode-map (kbd "M-f") 'elpy-nav-move-indent-shift-right)
  (define-key elpy-mode-map (kbd "M-b") 'elpy-nav-move-indent-shift-left)
  ;; allows Elpy to see virtualenv
  (add-hook 'elpy-mode-hook
	    ;; pyvenv-mode
	    '(lambda ()
	       (pyvenv-mode +1)
	       )
	    )
  ;; use flycheck instead of flymake
  (when (load "flycheck" t t)
    (setq elpy-modules (delq 'elpy-module-flymake elpy-modules))
    (add-hook 'elpy-mode-hook 'flycheck-mode))
  :hook
  (elpy-mode . poetry-tracking-mode)
  (elpy-mode . elpy-format-on-save)
  )

;; poetry
(use-package poetry
  :ensure t
  ;; :init
  ;; imperfect tracking strategy causes lags in builds
  ;; (setq poetry-tracking-strategy 'switch-buffer)
  :hook
  ;; activate poetry-tracking-mode when python-mode is active
  (python-mode . poetry-tracking-mode)
  )

;; disable pyvenv menu
(setq pyvenv-mode nil)

;; highlight-indent-guides.el
(use-package highlight-indent-guides
  :delight
  :ensure t
  :init
  :custom
  (highlight-indent-guides-responsive "stack" "Highlight ancestral guides of current guide")
  (highlight-indent-guides-delay 0 "Remove indentation highlight delay")
  (highlight-indent-guides-auto-odd-face-perc 10)
  (highlight-indent-guides-auto-even-face-perc 15)
  :hook
  (prog-mode . highlight-indent-guides-mode)
  )

;; html5-schema
;; This is sourced from GNU ELPA
(use-package html5-schema
  :ensure t
  )

;; emacs-lisp
(use-package emacs-lisp
  :ensure nil
  :defer t)

;; xkcd
(use-package xkcd
  :ensure t)

;; markdown-mode
(use-package markdown-mode
  :delight markdown-mode ".md"
  :ensure t
  :mode ("\\.md\\'"
         "\\.mkd\\'"
         "\\.markdown\\'"
	 "\\.mdx\\'"
	 )
  )

;; react-snippets
(use-package react-snippets
  :ensure t)

;; tide (for Typescript)
(use-package tide
  :ensure t
  :after (typescript-mode company flycheck)
  :hook ((typescript-mode . tide-setup)
	 (web-mode . my/activate-tide-mode)
         (typescript-mode . tide-hl-identifier-mode)
         (before-save . tide-format-before-save)))

;; activate tide automatically
(defun my/activate-tide-mode ()
  "Use hl-identifier-mode only on js or ts buffers."
  (when (and (stringp buffer-file-name)
             (string-match "\\.[tj]sx?\\'" buffer-file-name))
    (tide-setup)
    (tide-hl-identifier-mode)))

;; web-mode
(use-package web-mode
  :ensure t
  :mode
  ("\\.ejs\\'"
   "\\.hbs\\'"
   "\\.html\\'"
   "\\.php\\'"
   "\\.[jt]sx?\\'"
   "\\.css?\\'")
  :hook (
	 (web-mode . company-mode)
	 (scss-mode . web-mode)
	 )
  :config
  (setq web-mode-content-types-alist '(("jsx" . "\\.[jt]sx?\\'")))
  (setq web-mode-markup-indent-offset 2)
  (setq web-mode-css-indent-offset 2)
  (setq web-mode-code-indent-offset 2)
  (setq web-mode-script-padding 2)
  (setq web-mode-block-padding 2)
  (setq web-mode-style-padding 2)
  (setq web-mode-enable-auto-pairing t)
  (setq web-mode-enable-auto-closing t)
  (setq web-mode-enable-current-element-highlight t)
  )

;; prettier.el for linting web files
(use-package prettier
  :ensure t
  :config
  (put 'prettier-mode 'safe-local-variable #'fixnump)
  :hook (
	 (web-mode . prettier-mode)
	 )
  )

;; shfmt.el for autoformatting shell scripts
(use-package shfmt
  :ensure t
  :hook (
	 (sh-mode . shfmt-on-save-mode)
	 )
  )

;; end of 3rd-party packages
(put 'upcase-region 'disabled nil)

;; set option key as Meta
(setq mac-option-modifier 'meta)

;; No scrollbars!
(scroll-bar-mode -1)

;; set init default directory
(setq default-directory "~/")

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
(setq initial-frame-alist
      '(
	(top . 1) (left . 300)
	(width . 84) (height . 50)
	)
      )
(setq default-frame-alist
      '(
	;; (top . 1) (left . 1)
	(width . 84) (height . 50)
	)
      )

;; converting region to lowercase
(put 'downcase-region 'disabled nil)

;; use Common Lisp
(require 'cl-lib)
(eval-when-compile (require 'cl-lib))

;; Fancy titlebar for macOS
(add-to-list 'default-frame-alist '(ns-transparent-titlebar . t))
(add-to-list 'default-frame-alist '(ns-appearance . dark))
(setq ns-use-proxy-icon  nil)

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

;; other keybinds
(global-set-key (kbd "M-<kp-delete>") 'kill-word)
(global-set-key (kbd "C-x C-b") 'ibuffer)

(ido-mode 1)

;; Word count
(defun wc () (interactive) (shell-command (concat "wc " buffer-file-name)))
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
	  '(lambda ()
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
(add-to-list 'custom-theme-load-path (file-name-directory load-file-name))
(load-theme 'jidiculous-dark t)

;; set dir local vars as safe
(setq safe-local-variable-values
      '((python-black-extra-args "-S")
	(eval remove-hook 'elpy-mode-hook 'elpy-format-on-save t)
	(lexical-binding . t)))

;;; show package load time
(let ((elapsed (float-time (time-subtract (current-time)
                                          emacs-start-time))))
  (message "Loaded packages in %.3fs" elapsed))
;;; init.el ends here
