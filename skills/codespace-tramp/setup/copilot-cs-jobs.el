;;; copilot-cs-jobs.el --- Non-blocking Codespace command runner -*- lexical-binding: t; -*-

;;; Commentary:

;; Runs shell commands inside a GitHub Codespace without ever blocking the
;; Emacs daemon that Copilot CLI talks to over MCP.
;;
;; Why this exists
;; ---------------
;; Driving a Codespace with TRAMP's synchronous primitives (`process-file',
;; and even `start-file-process' when the connection has to be re-established)
;; blocks single-threaded Emacs for the duration of the call.  When such a call
;; outlives Copilot CLI's per-tool-call budget the client kills the stdio
;; bridge, the daemon's parent watchdog then reaps the daemon, and every later
;; tool call fails with `Transport closed'.  One slow command costs the whole
;; session.
;;
;; How this avoids it
;; ------------------
;; Nothing here ever waits on a synchronous remote operation:
;;
;;   * Commands are launched with LOCAL `start-process', which returns
;;     immediately.  The SSH connection is established inside that child
;;     process, so a slow or stalled connection cannot block Emacs.
;;   * The remote work runs detached under `nohup', writing to a log file in
;;     the Codespace.  It therefore survives the SSH connection dropping, the
;;     daemon being reaped, or the Copilot session ending -- and can be
;;     re-attached to later with `copilot-cs-attach'.
;;   * Output is streamed back by `tail -F' into a local buffer, so polling is
;;     a pure buffer read: instant, and impossible to stall.
;;   * Waiting is done with `accept-process-output', which runs the Emacs event
;;     loop.  Timers keep firing and the MCP socket keeps being serviced, so a
;;     wait is never stop-the-world.
;;
;; The MCP server's security blocklist inspects only the form submitted by the
;; client, not the innards of functions that form calls.  These helpers are
;; loaded into the daemon at boot, so they may use `start-process' and friends
;; while callers stay on the safe side of the blocklist by calling
;; `copilot-cs-sh' and friends.
;;
;; Usage from `emacs-codespace-eval-elisp':
;;
;;   (copilot-cs-use "<CS_ID>" "/workspaces/<dir>")
;;   (copilot-cs-sh "git status --porcelain")     ; -> output, or a job id
;;   (copilot-cs-poll)                            ; -> progress of the last job
;;   (copilot-cs-output)                          ; -> full output of that job

;;; Code:

(require 'seq)
(require 'subr-x)

;;; Configuration

(defconst copilot-cs-setup-dir
  (file-name-directory (or load-file-name buffer-file-name default-directory))
  "Directory holding this runner and its companion programs.")

(defvar copilot-cs-id nil
  "Immutable Codespace name (id) that commands are sent to.
Set with `copilot-cs-use'.  When nil, commands run on the local machine,
which is useful for testing this library without a Codespace.")

(defvar copilot-cs-ghcs-program
  (expand-file-name "copilot-ghcs" copilot-cs-setup-dir)
  "Program that runs Codespace SSH through Secretive.")

(defvar copilot-cs-configured nil
  "Non-nil once `copilot-cs-use' has chosen a target for this session.
Local execution is a deliberate choice, not a default: until a target has
been picked, `copilot-cs--argv' refuses to run anything at all.

Without this, a nil `copilot-cs-id' silently means \"run on the operator's
own machine\" -- the one outcome this whole workflow exists to prevent.  It
is an easy state to reach by accident, because a replacement daemon starts
with every variable back at its default, so commands issued after a daemon
restart would quietly retarget from the Codespace to the local machine.")

(defvar copilot-cs-dir nil
  "Working directory inside the Codespace that commands start in.")

(defvar copilot-cs-default-wait 10
  "Seconds runner calls wait by default before reporting back.
Kept well below the MCP tool's execution limit.")

(defconst copilot-cs-max-wait 15
  "Maximum seconds any single runner call may wait before reporting.
This leaves time for MCP request and response overhead instead of consuming
the complete tool-call budget inside the Emacs event loop.")

(defvar copilot-cs-tail-bytes 4000
  "Maximum bytes of job output included in a status report.
`copilot-cs-output' always returns everything.")

(defvar copilot-cs-remote-dir "$HOME/.copilot-cs-jobs"
  "Directory in the Codespace holding job scripts, logs, and pid files.
Written literally into the remote command line, so it may reference remote
shell variables such as $HOME.")

;;; Internals

(defun copilot-cs--done-marker (id)
  "Return the line job ID's log ends with, followed by its exit code.
The job id is baked in so that a command which merely prints something
marker-shaped -- grepping this repository, say -- cannot be mistaken for a
finished job."
  (format "__COPILOT_CS_DONE_%s__:" id))

(defun copilot-cs--ack-marker (id)
  "Return the line the launcher prints once job ID is detached and running."
  (format "__COPILOT_CS_ACK_%s__" id))

(defvar copilot-cs--jobs (make-hash-table :test #'equal)
  "Job id -> plist of (:id :cmd :cs :dir :buffer :process :started).")

(defvar copilot-cs--last-id nil
  "Id of the most recently launched job, used when callers omit one.")

(defvar copilot-cs--counter 0)

(random t)

(defun copilot-cs--new-id ()
  "Return a fresh job id.
The random tail keeps ids distinct across daemons, which matters because
concurrent sessions share one job directory inside the Codespace."
  (setq copilot-cs--counter (1+ copilot-cs--counter))
  (format "job-%s-%03d-%04x" (format-time-string "%H%M%S")
          copilot-cs--counter (random 65536)))

(defun copilot-cs--check-id (id)
  "Return ID if it is safe to interpolate into a remote command, else error."
  (if (and (stringp id) (string-match-p "\\`[A-Za-z0-9._-]+\\'" id))
      id
    (error "Invalid job id `%s'" id)))

(defun copilot-cs--argv (command)
  "Return an argv list that runs COMMAND, a shell string, on the target.
With `copilot-cs-id' set that is a Codespace over `gh codespace ssh';
otherwise it runs locally."
  (unless copilot-cs-configured
    (error "copilot-cs: no target selected -- call (copilot-cs-use CS-ID DIR) \
first; pass nil as CS-ID only if you really mean to run on this machine"))
  (if copilot-cs-id
      ;; The wrapper pins Secretive and refuses disk-key fallback. A signing
      ;; failure therefore fails this connection instead of printing an agent
      ;; error and then succeeding with an unrelated identity.
      (list copilot-cs-ghcs-program "ssh" copilot-cs-id command)
    (list "sh" "-c" command)))

(defun copilot-cs--launcher (id command)
  "Return the remote shell string that starts COMMAND as detached job ID.

The command is shipped base64-encoded, so no amount of quoting, newlines,
or shell metacharacters in COMMAND can corrupt the wrapper around it.

The remote side writes the script to a file, runs it detached under `nohup'
with its output redirected to a log, appends an exit-code marker when it
finishes, and finally execs `tail -F' on that log so output streams back
over this same connection."
  (copilot-cs--check-id id)
  (let* ((workdir (or copilot-cs-dir "."))
         ;; The shell's own `cd' error already names the directory, so this
         ;; message interpolates nothing and has no quoting surface at all.
         (script (format "cd %s || { printf '%%s\\n' 'copilot-cs: cannot enter \
the target directory -- set it with (copilot-cs-use ...)' >&2; exit 127; }\n%s\n"
                         (shell-quote-argument workdir) command))
         (b64 (base64-encode-string (encode-coding-string script 'utf-8) t))
         (dir copilot-cs-remote-dir))
    (concat
     "mkdir -p " dir " || exit 1; "
     "printf %s '" b64 "' | base64 -d > " dir "/" id ".sh || exit 1; "
     ;; `nohup' only ignores SIGHUP -- it leaves the job in our process group,
     ;; where a single group signal would still reach it. `setsid' moves the
     ;; job into its own session so nothing aimed at this connection can
     ;; touch it. Codespaces are Linux and always have it; elsewhere we fall
     ;; back to plain nohup.
     "S=; command -v setsid >/dev/null 2>&1 && S=setsid; "
     ;; $HOME and $R stay single-quoted here so the *inner* shell expands them.
     "$S nohup sh -c 'sh " dir "/" id ".sh; R=$?; "
     "if [ \"$R\" -ne 0 ] && [ ! -s " dir "/" id ".log ]; then "
     "printf \"copilot-cs: command exited with status %s without producing "
     "stdout or stderr\\n\" \"$R\"; "
     "fi; "
     "printf \"\\n" (copilot-cs--done-marker id) "%s\\n\" \"$R\"' "
     "> " dir "/" id ".log 2>&1 & "
     "echo $! > " dir "/" id ".pid; "
     "echo " (copilot-cs--ack-marker id) "; "
     "sleep 1; "
     "exec tail -c +1 -F " dir "/" id ".log")))

(defun copilot-cs--start (id command &optional label)
  "Run COMMAND, a remote shell string, streaming output into job ID's buffer.
LABEL describes the job in status reports.  Returns the job plist."
  (let* ((buffer (generate-new-buffer (format " *copilot-cs-%s*" id)))
         (argv (copilot-cs--argv command))
         ;; Pipes, not a pty. A pty would make this process a session leader,
         ;; so killing the stream would signal its whole process group and
         ;; take the detached job down with it -- exactly what detaching is
         ;; meant to prevent. Pipes also keep output byte-exact.
         (process-connection-type nil)
         (process (apply #'start-process (format "copilot-cs-%s" id) buffer argv))
         (job (list :id id :cmd (or label command) :cs copilot-cs-id
                    :dir copilot-cs-dir :buffer buffer :process process
                    :started (float-time))))
    (set-process-query-on-exit-flag process nil)
    ;; The default sentinel writes "Process ... killed" into the buffer, which
    ;; would show up as job output.  The buffer holds job output only.
    (set-process-sentinel process #'ignore)
    (puthash id job copilot-cs--jobs)
    (setq copilot-cs--last-id id)
    job))

(defun copilot-cs--job (&optional id)
  "Return the job plist for ID, defaulting to the most recent job."
  (let ((key (or id copilot-cs--last-id)))
    (unless key (error "No jobs have been launched yet"))
    (or (gethash key copilot-cs--jobs)
        (error "Unknown job `%s'; use (copilot-cs-status) to list jobs, \
or (copilot-cs-attach \"%s\") to re-attach to a job from an earlier session"
               key key))))

(defun copilot-cs--text (job)
  "Return everything streamed back for JOB so far."
  (let ((buffer (plist-get job :buffer)))
    (if (buffer-live-p buffer)
        (with-current-buffer buffer (buffer-string))
      "")))

(defun copilot-cs--rc (job)
  "Return JOB's exit code, or nil if it has not finished.
The marker must occupy a whole line, and carries JOB's id, so ordinary
output cannot be mistaken for it."
  (let ((text (copilot-cs--text job)))
    (when (string-match (concat "^"
                                (regexp-quote
                                 (copilot-cs--done-marker (plist-get job :id)))
                                "\\([0-9]+\\)$")
                        text)
      (string-to-number (match-string 1 text)))))

(defun copilot-cs--acked-p (job)
  "Return non-nil once the launcher confirmed JOB is detached and running."
  (string-match-p (concat "^"
                          (regexp-quote
                           (copilot-cs--ack-marker (plist-get job :id)))
                          "$")
                  (copilot-cs--text job)))

(defun copilot-cs--clean (job text)
  "Strip the transport's own bookkeeping lines for JOB from TEXT."
  (let ((done (copilot-cs--done-marker (plist-get job :id)))
        (ack (copilot-cs--ack-marker (plist-get job :id))))
    (string-join
     (seq-remove (lambda (line)
                   (or (string-prefix-p ack line)
                       (string-prefix-p done line)
                       (string-match-p "\\`tail: " line)))
                 (split-string text "\n"))
     "\n")))

(defun copilot-cs--tail (text)
  "Return at most `copilot-cs-tail-bytes' of the end of TEXT."
  (let ((trimmed (string-trim-right text)))
    (if (> (length trimmed) copilot-cs-tail-bytes)
        (concat "[...truncated, use (copilot-cs-output) for everything...]\n"
                (substring trimmed (- (length trimmed) copilot-cs-tail-bytes)))
      trimmed)))

(defun copilot-cs--output-text (job)
  "Return cleaned output for JOB, including a silent-failure diagnostic."
  (let* ((rc (copilot-cs--rc job))
         (output (copilot-cs--clean job (copilot-cs--text job))))
    (if (and rc (/= rc 0) (string-empty-p (string-trim output)))
        (format "copilot-cs: command exited with status %d without producing \
stdout or stderr" rc)
      output)))

(defun copilot-cs--attach-command (id)
  "Return the remote shell string that replays and follows job ID's log."
  (format "test -f %s/%s.log || { printf '%%s\\n' \
'copilot-cs: no log for this job in this Codespace'; exit 1; }; \
echo %s; exec tail -c +1 -F %s/%s.log"
          copilot-cs-remote-dir id
          ;; Re-attaching skips the launcher, so stand in for its
          ;; acknowledgement once the log is known to be there.
          (copilot-cs--ack-marker id)
          copilot-cs-remote-dir id))

(defun copilot-cs--resume (job)
  "Start streaming JOB's log again after its local connection went away.
The log is replayed from the beginning, so nothing said while we were not
listening is lost.  Returns the refreshed job plist."
  (let* ((id (plist-get job :id))
         (stale (plist-get job :buffer))
         ;; Follow the job back to wherever it was launched, not wherever the
         ;; daemon happens to be pointing now.
         (copilot-cs-id (plist-get job :cs))
         (copilot-cs-dir (plist-get job :dir))
         (fresh (copilot-cs--start id (copilot-cs--attach-command id)
                                   (plist-get job :cmd))))
    (when (buffer-live-p stale) (kill-buffer stale))
    (plist-put fresh :started (plist-get job :started))))

(defun copilot-cs--live (job)
  "Return JOB, reconnecting first if its output stream has gone away.
A dropped SSH connection, a restarted bridge, or a stopped stream leaves
the job running with nobody listening; this picks it back up."
  (if (or (copilot-cs--rc job)
          (process-live-p (plist-get job :process)))
      job
    (copilot-cs--resume job)))

(defun copilot-cs--bounded-wait (seconds)
  "Return SECONDS constrained to the safe wait interval."
  (unless (numberp seconds)
    (error "copilot-cs: wait must be a number, got %S" seconds))
  (min copilot-cs-max-wait (max 0 seconds)))

(defun copilot-cs--settle (job seconds)
  "Let JOB run briefly, returning early once it finishes.
SECONDS is capped at `copilot-cs-max-wait'. Waits by running the Emacs event
loop, so the daemon stays responsive."
  (let ((deadline (+ (float-time) (copilot-cs--bounded-wait seconds)))
        (process (plist-get job :process)))
    (while (and (null (copilot-cs--rc job))
                (process-live-p process)
                (< (float-time) deadline))
      (accept-process-output nil 0.2))
    ;; The exit marker can arrive in the same chunk that ends the stream.
    (accept-process-output nil 0.1)
    job))

(defun copilot-cs--report (job)
  "Return a human-readable status line plus recent output for JOB."
  (let* ((id (plist-get job :id))
         (process (plist-get job :process))
         (rc (copilot-cs--rc job))
         (elapsed (- (float-time) (plist-get job :started)))
         (output (copilot-cs--tail (copilot-cs--output-text job))))
    ;; Nothing more will arrive once the job is done; let the streamer go.
    (when (and rc (process-live-p process))
      (delete-process process))
    (concat
     (cond
      (rc (format "job=%s state=done rc=%d elapsed=%.1fs" id rc elapsed))
      ((and (process-live-p process) (not (copilot-cs--acked-p job)))
       (format "job=%s state=connecting elapsed=%.1fs -- waiting for the \
Codespace connection or Secretive approval; no remote job has been \
acknowledged yet; approve Secretive, then poll with \
(copilot-cs-poll \"%s\")" id elapsed id))
      ((process-live-p process)
       (format "job=%s state=running elapsed=%.1fs -- still going; \
poll with (copilot-cs-poll \"%s\")" id elapsed id))
      ;; Died before confirming the job was running: the Codespace was never
      ;; reached, or the job it names is not there. Either way nothing is
      ;; running out of sight, and the output below says which it was.
      ((not (copilot-cs--acked-p job))
       (format "job=%s state=failed elapsed=%.1fs -- could not start or \
reach the job; nothing is running" id elapsed))
      (t (format "job=%s state=detached elapsed=%.1fs -- the local stream \
ended but the job keeps running in the Codespace; re-attach with \
(copilot-cs-attach \"%s\")" id elapsed id)))
     "\n----\n" output)))

;;; Public API

(defun copilot-cs-use (cs-id &optional dir)
  "Send subsequent commands to Codespace CS-ID, starting in DIR.
Also starts warming the SSH connection in the background so the first real
command is not the one paying for the handshake.  A nil CS-ID targets the
local machine, which is useful for testing."
  (setq copilot-cs-id (and cs-id (not (string-empty-p cs-id)) cs-id)
        copilot-cs-dir (or dir ".")
        copilot-cs-configured t)
  (copilot-cs-warm)
  (format "target: cs=%s dir=%s%s"
          (or copilot-cs-id "<local -- THIS MACHINE>") copilot-cs-dir
          (if copilot-cs-id " (warming connection in background)" "")))

(defun copilot-cs-warm ()
  "Start establishing the Codespace connection, without waiting for it.
Safe to call repeatedly.  Booting a `Shutdown' Codespace also happens here."
  (when copilot-cs-id
    (let ((process (apply #'start-process "copilot-cs-warm" nil
                          (copilot-cs--argv "true"))))
      (set-process-query-on-exit-flag process nil)))
  "warming")

(defun copilot-cs-sh (command &optional wait)
  "Run COMMAND in the Codespace and report what happened.

COMMAND is a shell string, run by `sh' from `copilot-cs-dir'.  It is
launched detached and its output streamed back, so this call cannot block
the daemon no matter how long the command takes.

Waits up to WAIT seconds (default `copilot-cs-default-wait', capped at
`copilot-cs-max-wait') for it to finish. Short commands therefore return their
output directly; longer ones return a job id to hand to `copilot-cs-poll'."
  (let* ((id (copilot-cs--new-id))
         (job (copilot-cs--start id (copilot-cs--launcher id command) command)))
    (copilot-cs--report
     (copilot-cs--settle job (or wait copilot-cs-default-wait)))))

(defun copilot-cs-poll (&optional id wait)
  "Report on job ID, defaulting to the most recent job.
Waits up to WAIT seconds (default `copilot-cs-default-wait', capped at
`copilot-cs-max-wait') for it to finish before reporting. If the connection
carrying the job's output has died, this silently reconnects, so a dropped SSH
session costs nothing but the time to poll again."
  (copilot-cs--report
   (copilot-cs--settle (copilot-cs--live (copilot-cs--job id))
                       (or wait copilot-cs-default-wait))))

(defun copilot-cs-output (&optional id)
  "Return the complete output of job ID, defaulting to the most recent job."
  (let ((job (copilot-cs--job id)))
    (string-trim (copilot-cs--output-text job))))

(defun copilot-cs-attach (id &optional wait)
  "Re-attach to job ID and stream its Codespace log from the beginning.

Jobs run detached, so they outlive the SSH connection, the Emacs daemon,
and the Copilot session that started them.  Use this to pick a job back up
after a transport failure or in a later session."
  (copilot-cs--check-id id)
  (let ((job (copilot-cs--start id (copilot-cs--attach-command id)
                                "(re-attached)")))
    (copilot-cs--report
     (copilot-cs--settle job (or wait copilot-cs-default-wait)))))

(defun copilot-cs-stop (&optional id)
  "Ask job ID to stop, defaulting to the most recent job.
Signals the job itself but spares the wrapper watching it, so the wrapper
still records an exit code and the job reports as finished rather than
looking like it ran away.  The output stream is left connected, so that
exit code arrives here rather than having to be chased down later."
  (let* ((job (copilot-cs--live (copilot-cs--job id)))
         (key (copilot-cs--check-id (plist-get job :id)))
         (rc (copilot-cs--rc job))
         ;; Pid files outlive the jobs that wrote them, so signalling a job
         ;; that already finished could hit whatever inherited its pid.
         (copilot-cs-id (plist-get job :cs))
         (copilot-cs-dir (plist-get job :dir)))
    (if rc
        (format "job=%s already finished with rc=%d; nothing to stop" key rc)
      (copilot-cs-sh
       (format "P=$(cat %s/%s.pid 2>/dev/null); \
if [ -n \"$P\" ] && kill -0 \"$P\" 2>/dev/null; \
then pkill -TERM -P \"$P\" 2>/dev/null; echo \"signalled job under $P\"; \
else echo \"job is not running\"; fi"
               copilot-cs-remote-dir key)
       10))))

(defun copilot-cs-status ()
  "Summarize every job this daemon knows about."
  (if (zerop (hash-table-count copilot-cs--jobs))
      "no jobs"
    (let (lines)
      (maphash
       (lambda (id job)
         (push (format "%s%-18s %-9s %5.0fs  %s"
                       (if (equal id copilot-cs--last-id) "*" " ")
                       id
                       (cond ((copilot-cs--rc job)
                              (format "rc=%d" (copilot-cs--rc job)))
                             ((process-live-p (plist-get job :process)) "running")
                             (t "detached"))
                       (- (float-time) (plist-get job :started))
                       (plist-get job :cmd))
               lines))
       copilot-cs--jobs)
      (string-join (sort lines #'string<) "\n"))))

(defun copilot-cs-put (path content)
  "Write CONTENT to PATH in the Codespace, relative to `copilot-cs-dir'.
CONTENT is shipped base64-encoded, so it needs no escaping and may contain
quotes, newlines, or anything else."
  (let ((b64 (base64-encode-string (encode-coding-string content 'utf-8) t))
        (quoted (shell-quote-argument path)))
    (copilot-cs-sh
     ;; The path is only ever passed as a quoted argument -- never spliced
     ;; into a message the shell would then re-parse.
     (format "mkdir -p \"$(dirname %s)\" && printf %%s '%s' | base64 -d > %s \
&& echo \"wrote $(wc -c < %s) bytes to\" %s"
             quoted b64 quoted quoted quoted))))

(provide 'copilot-cs-jobs)
;;; copilot-cs-jobs.el ends here
