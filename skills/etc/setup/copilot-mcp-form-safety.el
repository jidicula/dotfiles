;;; copilot-mcp-form-safety.el --- Dotted-form compatibility for MCP -*- lexical-binding: t; -*-

;;; Commentary:

;; The MCP checker uses `dolist' even for quoted dotted pairs.  Give the checker
;; a proper-list view without changing the form that is actually evaluated.

;;; Code:

(defun copilot-mcp--check-dotted-form (original form)
  "Check FORM through ORIGINAL, including every improper-list tail."
  (if (or (atom form) (proper-list-p form))
      (funcall original form)
    (let ((tail form)
          reversed)
      (dotimes (_ (safe-length form))
        (push (car tail) reversed)
        (setq tail (cdr tail)))
      (when (consp tail)
        (error "Security: circular MCP forms are unsupported"))
      (funcall original (nreverse (cons tail reversed))))))

(advice-add 'mcp-server-security--check-form-safety
            :around #'copilot-mcp--check-dotted-form)

(provide 'copilot-mcp-form-safety)
;;; copilot-mcp-form-safety.el ends here
