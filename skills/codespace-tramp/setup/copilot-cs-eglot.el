;;; copilot-cs-eglot.el --- Eglot over Codespace SSH -*- lexical-binding: t; -*-

;;; Commentary:

;; Shared Eglot configuration for the dedicated Copilot daemon and the
;; operator's interactive Emacs.  Source files remain TRAMP buffers, but the
;; language server process is launched locally through `copilot-ghcs'.  This
;; keeps the server inside the Codespace without asking TRAMP to synchronously
;; start a remote process in single-threaded Emacs.

;;; Code:

(require 'cl-lib)
(require 'eglot)
(require 'flymake)
(require 'project)
(require 'subr-x)

(autoload 'go-mode "go-mode" nil t)

(defconst copilot-cs-eglot-setup-dir
  (file-name-directory (or load-file-name buffer-file-name default-directory))
  "Directory holding this file and its companion programs.")

(defvar copilot-cs-eglot-ghcs-program
  (expand-file-name "copilot-ghcs" copilot-cs-eglot-setup-dir)
  "Secretive-only Codespace SSH wrapper used for language servers.")

(defconst copilot-cs-eglot-supported-modes
  '(go-mode go-ts-mode ruby-mode ruby-ts-mode)
  "Major modes supported by the Codespace Eglot helpers.")

(defconst copilot-cs-eglot-request-timeout 12
  "Maximum seconds a semantic request may occupy one MCP tool call.")

(defvar copilot-cs-eglot--states (make-hash-table :test #'equal)
  "Remote path to the latest asynchronous Eglot startup state.")

(defun copilot-cs-eglot--project-root (project)
  "Return PROJECT's root, falling back to the current project."
  (let ((resolved (or project (project-current))))
    (unless resolved
      (error "Eglot could not determine the current project"))
    (file-name-as-directory (project-root resolved))))

(defun copilot-cs-eglot--ghcs-root-p (root)
  "Return non-nil when ROOT is inside a ghcs Codespace."
  (and (file-remote-p root)
       (string-equal (file-remote-p root 'method) "ghcs")))

(defun copilot-cs-eglot-project-find (dir)
  "Return a transient project for a ghcs DIR below `/workspaces/'."
  (when (and (file-remote-p dir)
             (string-equal (file-remote-p dir 'method) "ghcs"))
    (let ((local (file-local-name dir))
          (prefix (file-remote-p dir)))
      (when (string-match "\\`/workspaces/\\([^/]+\\)\\(?:/\\|\\'\\)" local)
        `(transient
          . ,(concat prefix "/workspaces/" (match-string 1 local) "/"))))))

(defun copilot-cs-eglot--login-command (root command)
  "Return a login-shell wrapper that runs COMMAND from remote ROOT."
  (format "bash -lc %s copilot-cs-eglot %s %s"
          (shell-quote-argument "cd \"$1\" && exec sh -c \"$2\"")
          (shell-quote-argument root)
          (shell-quote-argument command)))

(defun copilot-cs-eglot--remote-command (root command login)
  "Return a remote shell command for COMMAND in ROOT.
Use a login shell when LOGIN is non-nil."
  (if login
      (copilot-cs-eglot--login-command root command)
    (format "cd %s && exec sh -c %s"
            (shell-quote-argument root)
            (shell-quote-argument command))))

(defun copilot-cs-eglot--remote-contact (project command &optional login)
  "Return an Eglot process contact for COMMAND in PROJECT.
The process runs inside a ghcs Codespace.  Use its login environment when
LOGIN is non-nil."
  (let* ((root (copilot-cs-eglot--project-root project))
         (codespace (file-remote-p root 'host))
         (remote-root (file-local-name root))
         (remote-command
          (copilot-cs-eglot--remote-command remote-root command login))
         (argv (list copilot-cs-eglot-ghcs-program
                     "ssh" codespace remote-command)))
    (unless (and codespace
                 (string-match-p "\\`[A-Za-z0-9._-]+\\'" codespace))
      (error "Invalid Codespace id in project root `%s'" root))
    (list
     'eglot-lsp-server
     :process
     (lambda (connection)
       (let* ((name (jsonrpc-name connection))
              (default-directory temporary-file-directory)
              (process-connection-type nil))
         (make-process
          :name name
          :command argv
          :connection-type 'pipe
          :coding 'utf-8-emacs-unix
          :noquery t
          :stderr (get-buffer-create (format "*%s stderr*" name))))))))

(defun copilot-cs-eglot-ruby-contact (&optional _interactive project)
  "Return a Sorbet or Ruby LSP contact for PROJECT.
Sorbet is selected when the project contains `sorbet/config'."
  (let ((root (copilot-cs-eglot--project-root project)))
    (if (copilot-cs-eglot--ghcs-root-p root)
        (copilot-cs-eglot--remote-contact
         project
         "if [ -f sorbet/config ]; then if command -v watchman >/dev/null 2>&1; then exec bundle exec srb typecheck --lsp --cache-dir tmp/sorbet; else exec bundle exec srb typecheck --lsp --cache-dir tmp/sorbet --disable-watchman; fi; else exec ruby-lsp; fi"
         t)
      (if (file-exists-p (expand-file-name "sorbet/config" root))
          '("bundle" "exec" "srb" "typecheck" "--lsp"
            "--cache-dir" "tmp/sorbet")
        '("ruby-lsp")))))

(defun copilot-cs-eglot-go-contact (&optional _interactive project)
  "Return a gopls contact for PROJECT."
  (let ((root (copilot-cs-eglot--project-root project)))
    (if (copilot-cs-eglot--ghcs-root-p root)
        (copilot-cs-eglot--remote-contact project "exec gopls")
      '("gopls"))))

(defun copilot-cs-eglot--strip-sorbet-keys (args)
  "Remove Sorbet's non-standard `:requestMethod' from incoming ARGS."
  (cl-destructuring-bind (connection foreign-message) args
    (when (and (listp foreign-message)
               (plist-member foreign-message :requestMethod))
      (cl-remf foreign-message :requestMethod))
    (list connection foreign-message)))

(defun copilot-cs-eglot-configure ()
  "Install Codespace-aware Ruby and Go Eglot configuration."
  (add-hook 'project-find-functions #'copilot-cs-eglot-project-find)
  (add-to-list
   'eglot-server-programs
   '((ruby-mode ruby-ts-mode) . copilot-cs-eglot-ruby-contact))
  (add-to-list
   'eglot-server-programs
   '((go-mode go-dot-mod-mode go-dot-work-mode
      go-ts-mode go-mod-ts-mode go-work-ts-mode)
     . copilot-cs-eglot-go-contact))
  (setq eglot-connect-timeout (max eglot-connect-timeout 60))
  (unless (advice-member-p #'copilot-cs-eglot--strip-sorbet-keys
                           'jsonrpc-connection-receive)
    (advice-add 'jsonrpc-connection-receive :filter-args
                #'copilot-cs-eglot--strip-sorbet-keys)))

(defun copilot-cs-eglot--basic-path-p (path)
  "Return non-nil when PATH names a file in a ghcs `/workspaces/' tree."
  (and (stringp path)
       (file-remote-p path)
       (string-equal (file-remote-p path 'method) "ghcs")
       (string-match-p "\\`/workspaces/[^/]+/"
                       (file-local-name path))))

(defun copilot-cs-eglot--editable-path-p (path)
  "Return non-nil when PATH is canonically safe for the Copilot daemon."
  (if (fboundp 'copilot-mcp--editable-codespace-file-p)
      (copilot-mcp--editable-codespace-file-p path)
    (copilot-cs-eglot--basic-path-p path)))

(defun copilot-cs-eglot--infer-mode (path)
  "Return a supported major mode for PATH."
  (cond
   ((string-suffix-p ".go" path t) 'go-mode)
   ((string-suffix-p ".rb" path t) 'ruby-mode)
   (t (error "Cannot infer an Eglot mode for `%s'" path))))

(defun copilot-cs-eglot--record (path status &rest properties)
  "Record STATUS and PROPERTIES for PATH."
  (puthash path
           (append (list :status status :updated (float-time)) properties)
           copilot-cs-eglot--states))

(defun copilot-cs-eglot--complete-start (path mode buffer deadline)
  "Record PATH as ready once BUFFER is managed, before DEADLINE.
MODE is the major mode requested for the asynchronous startup."
  (condition-case err
      (if (not (buffer-live-p buffer))
          (error "Eglot buffer for `%s' was killed during startup" path)
        (with-current-buffer buffer
          (let ((server (eglot-current-server)))
            (when (and server
                       (jsonrpc-running-p server)
                       (not (eglot-managed-p)))
              (eglot--maybe-activate-editing-mode)
              (setq server (eglot-current-server)))
            (cond
             ((and (eglot-managed-p)
                   server
                   (jsonrpc-running-p server))
              (copilot-cs-eglot--record
               path 'ready :mode mode :buffer buffer :server server))
             ((>= (float-time) deadline)
              (error "Eglot did not begin managing `%s' before timeout" path))
             (t
              (run-at-time
               0.25 nil #'copilot-cs-eglot--complete-start
               path mode buffer deadline))))))
    (error
     (copilot-cs-eglot--record
      path 'error :mode mode :error (error-message-string err)))))

(defun copilot-cs-eglot--start-now (path mode)
  "Open PATH in MODE and connect Eglot.
This function runs from a timer so the initiating MCP call returns first."
  (condition-case err
      (progn
        (unless (copilot-cs-eglot--editable-path-p path)
          (error "Refusing Eglot access outside a Codespace /workspaces tree"))
        (unless (fboundp mode)
          (error "Major mode `%s' is unavailable" mode))
        (let ((buffer (find-file-noselect path)))
          (with-current-buffer buffer
            (unless (eq major-mode mode)
              (funcall mode))
            (let ((server (eglot-current-server)))
              (if (and server (jsonrpc-running-p server))
                  (unless (eglot-managed-p)
                    (eglot--maybe-activate-editing-mode))
                (setq eglot--cached-server nil
                      server (apply #'eglot (eglot--guess-contact))))
              (copilot-cs-eglot--complete-start
               path mode buffer (+ (float-time) eglot-connect-timeout))))))
    (error
     (copilot-cs-eglot--record
      path 'error :mode mode :error (error-message-string err)))))

(defun copilot-cs-eglot-start (path &optional mode)
  "Start Eglot asynchronously for remote PATH.
MODE defaults from PATH and must be a member of
`copilot-cs-eglot-supported-modes'."
  (unless (copilot-cs-eglot--basic-path-p path)
    (error "PATH must be a ghcs file below /workspaces/"))
  (let ((resolved-mode (or mode (copilot-cs-eglot--infer-mode path))))
    (unless (memq resolved-mode copilot-cs-eglot-supported-modes)
      (error "Unsupported Eglot mode `%s'" resolved-mode))
    (copilot-cs-eglot--record path 'starting :mode resolved-mode)
    (run-at-time 0 nil #'copilot-cs-eglot--start-now path resolved-mode)
    (format "eglot state=starting mode=%s path=%s" resolved-mode path)))

(defun copilot-cs-eglot--state-buffer (path)
  "Return PATH's recorded live buffer, or nil."
  (let ((buffer (plist-get (gethash path copilot-cs-eglot--states) :buffer)))
    (and (buffer-live-p buffer) buffer)))

(defun copilot-cs-eglot--server (path)
  "Return PATH's running Eglot server, or signal a useful error."
  (let ((entry (gethash path copilot-cs-eglot--states)))
    (unless entry
      (error "No Eglot session recorded for `%s'" path))
    (when (eq (plist-get entry :status) 'error)
      (error "%s" (plist-get entry :error)))
    (let ((buffer (copilot-cs-eglot--state-buffer path)))
      (unless buffer
        (error "Eglot is not ready for `%s'" path))
      (with-current-buffer buffer
        (unless (eglot-managed-p)
          (error "Eglot is not managing `%s'" path))
        (let ((server (eglot-current-server)))
          (unless (and server (jsonrpc-running-p server))
            (error "Eglot server is not running for `%s'" path))
          server)))))

(defun copilot-cs-eglot-status (path)
  "Return the current Eglot startup and connection status for PATH."
  (let* ((entry (gethash path copilot-cs-eglot--states))
         (status (plist-get entry :status))
         (buffer (copilot-cs-eglot--state-buffer path))
         (managed (and buffer
                       (with-current-buffer buffer (eglot-managed-p))))
         (server (or (and buffer
                          (with-current-buffer buffer (eglot-current-server)))
                     (plist-get entry :server)))
         (running (and server (jsonrpc-running-p server)))
         (stderr-buffer (and server (jsonrpc-stderr-buffer server)))
         (stderr
          (and (buffer-live-p stderr-buffer)
               (with-current-buffer stderr-buffer
                 (let* ((text (string-trim (buffer-string)))
                        (start (max 0 (- (length text) 2000))))
                   (substring text start))))))
    (cond
     ((null entry)
      (format "eglot state=unknown path=%s" path))
     ((eq status 'error)
      (format "eglot state=error mode=%s error=%s"
              (plist-get entry :mode) (plist-get entry :error)))
     ((and (eq status 'ready) (or (not managed) (not running)))
      (format "eglot state=disconnected mode=%s path=%s reason=%s%s"
              (plist-get entry :mode) path
              (if managed "server-not-running" "buffer-unmanaged")
              (if (string-empty-p (or stderr ""))
                  ""
                (format " stderr=%s" stderr))))
     (t
      (format "eglot state=%s mode=%s server=%s path=%s"
              status (plist-get entry :mode)
              (if running "running" "not-running") path)))))

(defun copilot-cs-eglot--at-position (path line column function)
  "Call FUNCTION in PATH at one-based LINE and zero-based COLUMN."
  (unless (and (integerp line) (> line 0))
    (error "LINE must be a positive integer"))
  (unless (and (integerp column) (>= column 0))
    (error "COLUMN must be a non-negative integer"))
  (copilot-cs-eglot--server path)
  (let ((buffer (copilot-cs-eglot--state-buffer path)))
    (with-current-buffer buffer
      (save-restriction
        (widen)
        (goto-char (point-min))
        (when (/= (forward-line (1- line)) 0)
          (error "LINE %d is outside `%s'" line path))
        (move-to-column column)
        (funcall function)))))

(defun copilot-cs-eglot--request-at (path line column method &optional extra)
  "Send an LSP METHOD for PATH at LINE and COLUMN, appending EXTRA params."
  (copilot-cs-eglot--at-position
   path line column
   (lambda ()
     (prin1-to-string
      (eglot--request
       (eglot-current-server)
       method
       (append (eglot--TextDocumentPositionParams) extra)
       :timeout copilot-cs-eglot-request-timeout)))))

(defun copilot-cs-eglot-hover (path line column)
  "Return hover information for PATH at LINE and COLUMN."
  (copilot-cs-eglot--request-at path line column :textDocument/hover))

(defun copilot-cs-eglot-definition (path line column)
  "Return definitions for PATH at LINE and COLUMN."
  (copilot-cs-eglot--request-at path line column :textDocument/definition))

(defun copilot-cs-eglot-references (path line column)
  "Return references for PATH at LINE and COLUMN."
  (copilot-cs-eglot--request-at
   path line column :textDocument/references
   '(:context (:includeDeclaration t))))

(defun copilot-cs-eglot-document-symbols (path)
  "Return document symbols for PATH."
  (copilot-cs-eglot--server path)
  (let ((buffer (copilot-cs-eglot--state-buffer path)))
    (with-current-buffer buffer
      (prin1-to-string
       (eglot--request
        (eglot-current-server)
        :textDocument/documentSymbol
        `(:textDocument ,(eglot--TextDocumentIdentifier))
        :timeout copilot-cs-eglot-request-timeout)))))

(defun copilot-cs-eglot-diagnostics (path)
  "Return Eglot-backed Flymake diagnostics for PATH."
  (copilot-cs-eglot--server path)
  (let ((buffer (copilot-cs-eglot--state-buffer path)))
    (with-current-buffer buffer
      (mapcar
       (lambda (diagnostic)
         (save-excursion
           (goto-char (flymake-diagnostic-beg diagnostic))
           (list :line (line-number-at-pos)
                 :column (current-column)
                 :severity (flymake-diagnostic-type diagnostic)
                 :message (flymake-diagnostic-text diagnostic))))
       (flymake-diagnostics (point-min) (point-max))))))

(defun copilot-cs-eglot-stop (path)
  "Stop the Eglot server managing PATH."
  (let ((buffer (copilot-cs-eglot--state-buffer path)))
    (when buffer
      (with-current-buffer buffer
        (when-let* ((server (eglot-current-server)))
          (eglot-shutdown server))))
    (copilot-cs-eglot--record path 'stopped)
    (format "eglot state=stopped path=%s" path)))

(copilot-cs-eglot-configure)

(provide 'copilot-cs-eglot)
;;; copilot-cs-eglot.el ends here
