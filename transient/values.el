((magit-diff:magit-diff-mode "--no-ext-diff" "--stat" "--show-signature")
 (magit-log:magit-log-mode "-n256" "--graph" "--decorate" "--show-signature")
 (rg-menu "--hidden" "--glob='!.git'"))
