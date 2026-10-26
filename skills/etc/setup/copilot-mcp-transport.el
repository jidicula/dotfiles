;;; copilot-mcp-transport.el --- Bounded MCP socket writes -*- lexical-binding: t; -*-

;;; Commentary:

;; Pace MCP writes below the Unix socket buffer size to avoid Emacs's 20ms
;; buffer-full waits.  Keep each response intact across re-entrant sends.

;;; Code:

(defun copilot-mcp--send-string (send process string)
  "Send STRING through SEND in bounded chunks for an MCP PROCESS."
  (if (not (and (processp process) (stringp string)))
      (funcall send process string)
    (let ((queue (process-get process 'copilot-mcp--send-queue)))
      (cond
       (queue
        (nconc queue (list string))
        nil)
       ((and (eq (process-type process) 'network)
             (eq (process-filter process)
                 'mcp-server-transport-unix--client-filter)
             (> (length string) 1024))
        (let ((filter (process-filter process)))
          (process-put process 'copilot-mcp--send-queue (list string))
          (unwind-protect
              (progn
                (set-process-filter process t)
                (while (setq queue (process-get process 'copilot-mcp--send-queue))
                  (let* ((text (car queue))
                         (length (length text))
                         (start 0))
                    (while (< start length)
                      (let ((end (min (+ start 1024) length)))
                        (funcall send process (substring text start end))
                        (setq start end))
                      (when (< start length)
                        (accept-process-output process 0.0001 nil -1))))
                  (process-put process 'copilot-mcp--send-queue (cdr queue))))
            (process-put process 'copilot-mcp--send-queue nil)
            (set-process-filter process filter))))
       (t (funcall send process string))))))

(advice-add 'process-send-string :around #'copilot-mcp--send-string)

(provide 'copilot-mcp-transport)
;;; copilot-mcp-transport.el ends here
