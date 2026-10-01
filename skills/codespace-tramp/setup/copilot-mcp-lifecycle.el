;;; copilot-mcp-lifecycle.el --- Keep session daemons with their CLI owner -*- lexical-binding: t; -*-

;;; Code:

(defvar copilot-cs--jobs)

(defvar copilot-mcp-parent-pid nil
  "PID of the owning Copilot CLI, or the bridge for a manual invocation.")

(defvar copilot-mcp--parent-start nil
  "Start time of the tracked owner, to distinguish reused process ids.")

(defconst copilot-mcp-parent-poll-interval 20
  "Seconds between owner liveness checks.")

(defvar copilot-mcp-orphan-grace
  (let ((raw (getenv "COPILOT_MCP_ORPHAN_GRACE")))
    (if (and raw (> (string-to-number raw) 0))
        (string-to-number raw)
      3600))
  "Seconds to keep running after the owning process disappears.")

(defvar copilot-mcp--orphaned-since nil
  "When the owning process was first seen to be gone, or nil if it is alive.")

(defun copilot-mcp-attach-parent (bridge-pid)
  "Track BRIDGE-PID's nearest Copilot CLI ancestor, or the bridge itself.
Refresh the owner's identity and grace window without resetting runner state."
  (let* ((pid bridge-pid)
         (attributes (and (integerp pid) (> pid 1) (process-attributes pid)))
         (bridge-start (alist-get 'start attributes))
         seen owner)
    (unless bridge-start
      (error "Cannot identify MCP bridge process %s" bridge-pid))
    (while (and attributes (not owner) (not (memq pid seen)))
      (push pid seen)
      (if (equal (file-name-nondirectory (or (alist-get 'comm attributes) ""))
                 "copilot")
          (setq owner (cons pid (alist-get 'start attributes)))
        (setq pid (alist-get 'ppid attributes)
              attributes (and (integerp pid) (> pid 1)
                              (process-attributes pid)))))
    (when (and owner (null (cdr owner)))
      (error "Cannot identify Copilot CLI process %s" (car owner)))
    (setq copilot-mcp-parent-pid (if owner (car owner) bridge-pid)
          copilot-mcp--parent-start (if owner (cdr owner) bridge-start)
          copilot-mcp--orphaned-since nil)
    (format "tracking %s process %s"
            (if owner "Copilot CLI" "bridge (no Copilot CLI ancestor)")
            copilot-mcp-parent-pid)))

(defun copilot-mcp-runner-active-p ()
  "Return non-nil while any known runner connection is still live."
  (and (boundp 'copilot-cs--jobs)
       (hash-table-p copilot-cs--jobs)
       (catch 'active
         (maphash
          (lambda (_ job)
            (when (process-live-p (plist-get job :process))
              (throw 'active t)))
          copilot-cs--jobs)
         nil)))

(defun copilot-mcp-watch-parent ()
  "Shut down after the owner exits and its full grace window expires."
  (let ((attributes (and copilot-mcp-parent-pid
                         (process-attributes copilot-mcp-parent-pid))))
    (if (or (null copilot-mcp-parent-pid)
            (and attributes copilot-mcp--parent-start
                 (alist-get 'start attributes)
                 (time-equal-p (alist-get 'start attributes) copilot-mcp--parent-start)
                 (not (member (alist-get 'state attributes) '("Z" "X")))))
        (setq copilot-mcp--orphaned-since nil)
      (unless copilot-mcp--orphaned-since
        (setq copilot-mcp--orphaned-since (float-time))
        (message "Copilot MCP owner %s exited; starting orphan grace"
                 copilot-mcp-parent-pid))
      (when (>= (- (float-time) copilot-mcp--orphaned-since)
                copilot-mcp-orphan-grace)
        (unless (copilot-mcp-runner-active-p)
          (message "Copilot MCP orphan grace expired; stopping idle daemon")
          (kill-emacs 0))))))

(provide 'copilot-mcp-lifecycle)
;;; copilot-mcp-lifecycle.el ends here
