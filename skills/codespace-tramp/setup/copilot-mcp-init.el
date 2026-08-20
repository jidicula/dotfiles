;;; copilot-mcp-init.el --- Throwaway Emacs daemon for Copilot CLI MCP -*- lexical-binding: t; -*-

;;; Commentary:

;; Boots a minimal, disposable Emacs daemon that exposes the MCP server plus
;; just enough TRAMP/codespaces machinery to drive a Codespace over `/ghcs:'.
;;
;; Why: Emacs is single-threaded, so one synchronous TRAMP operation (a sync,
;; a reconnect) blocks the whole instance.  Sharing one daemon across several
;; Copilot CLI sessions therefore causes stop-the-world lockups.  Giving each
;; session its own daemon removes the contention entirely, and keeps the
;; interactive Emacs daemon out of the blast radius.
;;
;; Loaded with `emacs -Q' so it never reads init.el and never touches the
;; interactive daemon's state.  Launched by the sibling copilot-emacs-mcp
;; wrapper as:
;;
;;   EMACS_MCP_SOCKET_NAME=<id> COPILOT_MCP_PARENT_PID=<pid> \
;;     emacs -Q --bg-daemon=<id> -l copilot-mcp-init.el

;;; Code:

(defconst copilot-mcp-setup-dir
  (file-name-directory (or load-file-name buffer-file-name default-directory))
  "Directory holding this file and its companion libraries.")

;; straight.el installs packages under straight/build/.  Add every build dir to
;; `load-path' so transitive dependencies resolve without re-running straight.
(let ((build (expand-file-name "straight/build/" user-emacs-directory)))
  (unless (file-directory-p build)
    (error "No straight build directory at %s" build))
  (dolist (d (directory-files build t "\\`[^.]"))
    (when (file-directory-p d)
      (add-to-list 'load-path d))))

(add-to-list 'load-path copilot-mcp-setup-dir)

(defvar copilot-mcp-id (or (getenv "EMACS_MCP_SOCKET_NAME") "copilot")
  "Unique id for this daemon, used to namespace its socket and on-disk state.")

;;; Isolation
;; Every daemon is disposable and several may run at once, so opt out of any
;; shared or persistent on-disk state that they would otherwise fight over.

(setq auto-save-list-file-prefix nil
      auto-save-default nil
      create-lockfiles nil
      make-backup-files nil
      inhibit-startup-screen t
      vc-handled-backends nil)

;;; TRAMP
;; Mirrors the ghcs tuning in init.el.  Keep the two in sync when either moves.

(require 'tramp)

(setq tramp-persistency-file-name
      (expand-file-name (format "tramp-%s" copilot-mcp-id)
                        temporary-file-directory))

(connection-local-set-profile-variables
 'tramp-ghcs-direct-async-profile
 '((tramp-direct-async-process . t)))
(connection-local-set-profiles
 '(:application tramp :protocol "ghcs")
 'tramp-ghcs-direct-async-profile)

;; Mirrors init.el.  Note this is inert for ghcs: TRAMP only ever reads the
;; *variable* `tramp-copy-size-limit', never a "copy-size-limit" connection
;; property.  The working equivalent is set below, once ghcs has been taught
;; how to copy out of band at all.  Kept for parity, and it is harmless.
(add-to-list 'tramp-connection-properties
             (list (regexp-quote "/ghcs:") "copy-size-limit" (* 1024 1024)))

(setq tramp-use-ssh-controlmaster-options nil)

;; TRAMP forces ControlMaster off in compilation buffers to dodge an
;; `accept-process-output' conflict, which costs a fresh connection each time.
;; Undo that.  Registered after TRAMP's own hook, so this one wins.  Also inert
;; for ghcs, whose login-args carry no "%c" for TRAMP to substitute into, but
;; it applies if this daemon ever talks to a plain /ssh: or /scp: host.
(with-eval-after-load 'compile
  (remove-hook 'compilation-mode-hook
               #'tramp-compile-disable-ssh-controlmaster-options))

(add-to-list 'tramp-remote-path 'tramp-own-remote-path)

(setq tramp-inline-compress-start-size 128
      tramp-verbose 0
      remote-file-name-inhibit-locks t
      tramp-allow-unsafe-temporary-files t)

;; Reading or writing a large file inline pulls it through a buffer, which asks
;; "really open?" above this threshold.  There is nobody here to answer, so a
;; prompt would hang the daemon outright.
(setq large-file-warning-threshold nil)

;; init.el also sets `magit-tramp-pipe-stty-settings'; that is deliberately not
;; mirrored here, as this daemon never loads Magit.

(require 'codespaces)
(codespaces-setup)

;;; Out-of-band file copying
;; codespaces.el registers ghcs as a login-only method, so every transfer is
;; inline: the file is base64'd through the shell connection.  That is the
;; right trade for ordinary source files, but it scales linearly and badly --
;; measured against a real Codespace, a write costs roughly 8s/MB, so a 4MB
;; file takes over 30s.  That alone can exceed Copilot CLI's per-call budget.
;;
;; Teaching ghcs to copy out of band with `gh codespace cp' gives TRAMP a
;; second option.  It has a flat ~6.5s setup cost and is then near-instant, so
;; it wins above roughly 1MB and gets dramatically better from there (a 16MB
;; write: ~5s out of band, versus minutes inline).
;;
;; `-e' asks gh to evaluate remote names as a shell would, which is what TRAMP
;; assumes when it quotes them.
(let ((ghcs (assoc "ghcs" tramp-methods)))
  (when (and ghcs (null (assq 'tramp-copy-program (cdr ghcs))))
    (setcdr ghcs
            (append (cdr ghcs)
                    '((tramp-copy-program "gh")
                      (tramp-copy-args (("codespace") ("cp") ("-c" "%h")
                                        ("-e") ("-r")))
                      (tramp-copy-file-name (("remote:") ("%f")))
                      (tramp-copy-recursive t))))))

;; The size above which copying out of band is worth its setup cost, measured
;; rather than guessed.  This is the variable TRAMP actually consults; the
;; connection property of the same name above is never read.
(with-eval-after-load 'tramp-sh
  (setq tramp-copy-size-limit (* 1024 1024)))

;; `gh codespace cp' cannot express remote paths containing spaces or shell
;; metacharacters: it either fails, or silently writes to a literally
;; backslashed name.  Rather than risk landing a file in the wrong place, keep
;; such paths on the inline route, which handles them correctly.  Inline is
;; slower for big files, but it is never wrong.
(defconst copilot-mcp-ghcs-oob-safe-path-regexp
  (rx bos (* (any "A-Za-z0-9" "_./+-@,=:")) eos)
  "Remote paths `gh codespace cp' is known to handle without mangling.")

(defun copilot-mcp--ghcs-oob-safe-p (orig vec size)
  "Allow out-of-band copying only for paths ORIG approves and gh can express."
  (and (funcall orig vec size)
       (or (not (equal (tramp-file-name-method vec) "ghcs"))
           (string-match-p copilot-mcp-ghcs-oob-safe-path-regexp
                           (tramp-file-name-unquote-localname vec)))))

(with-eval-after-load 'tramp-sh
  (advice-add 'tramp-method-out-of-band-p :around
              #'copilot-mcp--ghcs-oob-safe-p))

;;; Codespace command runner
;; Commands must never be run with TRAMP's synchronous primitives: they block
;; single-threaded Emacs, and a call that overruns Copilot CLI's per-call budget
;; costs the whole session.  copilot-cs-jobs runs everything detached and
;; non-blocking instead.  See that file's commentary.

(require 'copilot-cs-jobs)

;; Optional: point the runner at a Codespace up front, so the SSH handshake is
;; already paid for by the time the first command arrives.
(let ((cs (getenv "COPILOT_CS_ID"))
      (dir (getenv "COPILOT_CS_DIR")))
  (when (and cs (not (string-empty-p cs)))
    (copilot-cs-use cs dir)))

;;; Lifecycle
;; The daemon is owned by the wrapper process that spawned it, and shuts itself
;; down once that owner is gone for good.  Polling the parent covers SIGKILL,
;; which no exit trap can.
;;
;; The grace window is deliberately long.  The wrapper does NOT only die at the
;; end of a session: Copilot CLI also kills it when a tool call overruns its
;; budget.  A short window turned that into a cascade -- one slow call reaped
;; the daemon, and every later call failed with `Transport closed' until the
;; whole session was restarted.  Outliving the wrapper by a wide margin means a
;; replacement wrapper can reattach (refreshing `copilot-mcp-parent-pid') and
;; find its daemon, and its jobs, still there.

(defvar copilot-mcp-parent-pid
  (let ((raw (getenv "COPILOT_MCP_PARENT_PID")))
    (and raw (string-to-number raw)))
  "PID of the wrapper process this daemon serves.
Refreshed by the wrapper when a new client reattaches to an existing daemon.")

(defconst copilot-mcp-parent-poll-interval 20
  "Seconds between parent liveness checks.")

(defvar copilot-mcp-orphan-grace
  (let ((raw (getenv "COPILOT_MCP_ORPHAN_GRACE")))
    (if (and raw (> (string-to-number raw) 0))
        (string-to-number raw)
      900))
  "Seconds to keep running after the owning wrapper disappears.
Long enough that a wrapper killed mid-session can be respawned and reattach
without losing this daemon.  Override with COPILOT_MCP_ORPHAN_GRACE.")

(defvar copilot-mcp--orphaned-since nil
  "When the owning wrapper was first seen to be gone, or nil if it is alive.")

(defun copilot-mcp-watch-parent ()
  "Shut down once the owning wrapper has been gone for the full grace window."
  (if (or (null copilot-mcp-parent-pid)
          (process-attributes copilot-mcp-parent-pid))
      (setq copilot-mcp--orphaned-since nil)
    (unless copilot-mcp--orphaned-since
      (setq copilot-mcp--orphaned-since (float-time)))
    (when (>= (- (float-time) copilot-mcp--orphaned-since)
              copilot-mcp-orphan-grace)
      (kill-emacs 0))))

(run-with-timer copilot-mcp-parent-poll-interval
                copilot-mcp-parent-poll-interval
                #'copilot-mcp-watch-parent)

;;; MCP server

(require 'mcp-server)

(setq mcp-server-socket-name copilot-mcp-id
      mcp-server-socket-conflict-resolution 'error)

(mcp-server-start-unix)

;; Record the pid next to the socket.  A daemon wedged inside a synchronous
;; remote operation stops answering `emacsclient' altogether, so the wrapper
;; cannot ask it to quit and has no reliable way to find it in `ps' (the daemon
;; name is mangled there).  The pid file gives the wrapper something it can
;; SIGKILL, so a wedged daemon gets replaced instead of blocking the session.

(defconst copilot-mcp-pid-file
  (expand-file-name (format "emacs-mcp-server-%s.pid" copilot-mcp-id)
                    mcp-server-socket-directory))

(with-temp-file copilot-mcp-pid-file
  (insert (number-to-string (emacs-pid)) "\n"))

(add-hook 'kill-emacs-hook
          (lambda ()
            (when (file-exists-p copilot-mcp-pid-file)
              (delete-file copilot-mcp-pid-file))))

(provide 'copilot-mcp-init)
;;; copilot-mcp-init.el ends here
