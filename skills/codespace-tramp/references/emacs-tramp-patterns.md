# Codespace execution patterns (cookbook)

How to run commands and edit files inside a GitHub Codespace from the local
Emacs daemon behind the `emacs-codespace` MCP server. Referenced by the
`codespace-tramp` skill. Substitute:

- `<CS_ID>` — the immutable Codespace `name` (id) from
  `gh codespace list --json name`.
- `<dir>` — the repo's working directory under `/workspaces/` inside the
  Codespace (discover it; do not assume).

Repository commands and file operations run through the
**`emacs-codespace-eval-elisp`** tool. GitHub control-plane operations use the
operator's local authenticated `gh`, as described below.

## Golden rules

1. **Run every command whose execution environment is the Codespace with
   `copilot-cs-sh`.** Never with
   `process-file`, `start-file-process`, or `gh codespace ssh -c <id> -- <cmd>`.
   The reason is in "Why not TRAMP" below, and it is not a style preference —
   getting this wrong can cost the whole session.
2. **Point the runner at the Codespace once, with `copilot-cs-use`**, before
   doing anything else. Every later call inherits that target.
3. **Never assume a command is fast.** On a large repository even `git status`,
   `git fetch`, or a repo-wide `grep` can take minutes. `copilot-cs-sh` already
   handles this; just poll when it tells you to.
4. **Stay inside the security blocklist** (below); use the allowed primitives.
5. **Leave the tree clean** — revert throwaway changes when done.

## Why not TRAMP for commands

TRAMP's `process-file` blocks single-threaded Emacs until the remote command
returns, and `start-file-process` blocks too whenever the SSH connection has to
be established first — so "launch it asynchronously" is not by itself a
defence. The launch call is exactly where this bites, because it looks
instantaneous right up until the connection needs re-establishing.

A blocked daemon triggers a cascade wildly out of proportion to the command
that caused it:

1. The call outruns Copilot CLI's per-tool-call budget.
2. Copilot CLI may kill the stdio bridge.
3. Later tool calls fail with `Transport closed` until the bridge is respawned.
4. `/mcp` respawns it; its startup probe replaces the wedged daemon. Use
   `/restart` only as a fallback, because that also discards conversation
   context.

`copilot-cs-sh` avoids all of this. It launches work through a *local* child
process, so a slow or stalled connection can never block Emacs; runs the work
in its own session (`setsid`) in the Codespace, so it survives disconnects and
cannot be caught by a signal aimed at the connection; and streams output back
into a local buffer, so polling is instant.

## Setting the target

```elisp
(copilot-cs-use "<CS_ID>" "/workspaces/<dir>")
```

Returns immediately and starts warming the SSH connection in the background, so
the first real command does not pay for the handshake. Call it again to switch
Codespaces or directories.

**Call this before anything else, and again after any daemon restart.** Until a
target has been chosen the runner refuses to run at all:

```
copilot-cs: no target selected -- call (copilot-cs-use CS-ID DIR) first
```

That guard exists because the alternative is worse than an error. A nil
`copilot-cs-id` means "run on the operator's own machine", and a replacement
daemon starts with every variable back at its default — so commands issued
after a daemon restart used to retarget silently from the Codespace to the
local machine, with `git status` and friends reporting on the operator's
dotfiles as if they were the repo under work. Passing nil explicitly still
selects local execution for testing, and `copilot-cs-use` labels it
`cs=<local -- THIS MACHINE>` so it cannot be mistaken for a Codespace.

## Running commands

```elisp
(copilot-cs-sh "git status --porcelain")
```

`copilot-cs-sh` waits up to 10 seconds and then reports. Short commands come
back with their output directly:

```
job=job-124113-002 state=done rc=0 elapsed=1.2s
----
 M app/models/user.rb
```

Longer ones come back with a job id to follow:

```
job=job-124114-003 state=running elapsed=10.2s -- still going; poll with (copilot-cs-poll "job-124114-003")
----
Running 412 tests...
```

Before the remote launcher prints its acknowledgement, the report uses
`state=connecting`, not `state=running`. No remote job is known to have started
yet. This usually means the Codespace is connecting or Secretive is waiting for
approval; approve the request and poll the same job instead of launching a
duplicate.

The command is a shell string run by a **non-login, non-interactive `sh`** from
the directory given to `copilot-cs-use`. Prefer `sh` syntax over `bash -lc`:
some Codespaces' login-shell setup fails silently, returning rc=1 with no output
at all, and a login shell costs an extra process on every call.

The one exception is commands that need the Codespace's login-shell
environment, below.

The runner routes SSH and out-of-band copies through `setup/copilot-ghcs`. The
helper presents the active Secretive public-key stand-in in the base-plus-`.pub`
shape required by `gh`, pins Secretive's agent socket, and enables
`IdentitiesOnly=yes`. All private signing remains in Secretive. If signing is
not approved, the connection fails instead of silently succeeding with
`~/.ssh/codespaces.auto` or another disk key.

Do not replace this with raw `gh codespace ssh` or `gh codespace cp`. If
Secretive authentication fails three times, stop and prompt the operator before
trying again; they may be away from the computer and unable to approve the
request.

### Commands that need the Codespace login environment

`git push`, HTTPS `git fetch`, signed `git commit`, and repository commands that
fetch authenticated remote data are where the non-login shell is the wrong
default. Codespaces injects `GITHUB_SERVER_URL`, `GITHUB_API_URL`, and
`CODESPACE_NAME` into **login shells only**, and several things depend on them:

- `/.codespaces/bin/gitcredential_github.sh` exits without emitting credentials
  unless **both** `GITHUB_TOKEN` and `GITHUB_SERVER_URL` are set, so git falls
  through to prompting and fails with
  `could not read Username for 'https://github.com'`.
- `gpg.program` points at `/.codespaces/bin/gh-gpgsign`, a shim that holds no
  key and POSTs the payload to `$GITHUB_API_URL/vscs_internal/commit/sign`. With
  `GITHUB_API_URL` unset it builds a scheme-less relative URL and dies with
  `unsupported protocol scheme ""`. Codespaces sets `commit.gpgsign=true`, so
  this fails *every* commit.
- `gh` falls back to the restricted `GITHUB_TOKEN` and `gh auth status` reports
  it invalid.
- Repository validation tools that fetch coverage maps or other protected
  artifacts may report `No token found` when the URLs or related login
  environment are absent.

`GITHUB_TOKEN` **is** present in the non-login shell, which makes all three look
like token problems when they are really missing-URL problems. Confirm before
theorising:

```elisp
(copilot-cs-sh "echo login=$(bash -lc env | wc -l) nonlogin=$(env | wc -l)")
```

Wrap only these commands in a login shell, and `cd` explicitly — `bash -l` does
not inherit the runner's directory:

```elisp
(copilot-cs-sh "bash -lc 'cd /workspaces/<dir> && git push -u origin <branch>'")
```

The same pattern applies to an authenticated validation command:

```elisp
(copilot-cs-sh "bash -lc 'cd /workspaces/<dir> && <validation-command>'")
```

Do not print, copy, or manually export a token. The login shell supplies the
environment through the Codespace's normal configuration.

To sign a commit that was already made unsigned, amend it through a login
shell:

```elisp
(copilot-cs-sh "bash -lc 'cd /workspaces/<dir> && git commit --amend --no-edit'")
```

Everything else stays on plain `sh`.

### GitHub control-plane operations use local `gh`

Creating or updating a pull request, dispatching a workflow, reading workflow
runs or job logs, and similar GitHub API operations do not need the Codespace
execution environment. Run them with the operator's local authenticated `gh`,
not through `copilot-cs-sh`, and always identify the remote repository
explicitly:

```bash
gh pr list -R "$NWO" --head "$BRANCH" --state open
gh pr create -R "$NWO" --head "$BRANCH" --draft \
  --title "<title>" --body "<body>"
gh workflow run <workflow> -R "$NWO" --ref "$BRANCH"
gh run view <run-id> -R "$NWO" --job <job-id> --log
```

The Codespace's `GITHUB_TOKEN` is an integration token whose permissions can
reject these operations with errors such as `Bad credentials` or
`Resource not accessible by integration`. Do not copy the operator's local
credentials into the Codespace; keep these control-plane actions local.

### Explicit host-to-Codespace copies

Use `setup/copilot-ghcs cp` for an explicit transfer between the operator's
machine and a Codespace. Never invoke raw `gh codespace cp`: the helper pins
Secretive, enables the remote expansion needed for absolute paths, and rejects
remote path characters that would make expansion unsafe.

```bash
# Write an exact absolute remote path.
setup/copilot-ghcs cp "$CS_ID" \
  /local/path/baseline.txt remote:/tmp/baseline.txt

# A plain relative remote path is below the remote user's home directory.
setup/copilot-ghcs cp "$CS_ID" \
  /local/path/baseline.txt remote:baseline.txt
```

For paths containing spaces or shell metacharacters, use `copilot-cs-put` or
TRAMP's inline transfer instead.

### Running `gh copilot` without a TTY

`gh copilot` normally prompts before downloading Copilot CLI when the binary is
absent. The runner deliberately has no TTY, so that prompt is unavailable and
`gh` exits with `Copilot CLI not installed`. Set `CI=1` on the first and
subsequent non-interactive invocations; `gh` then performs its supported
prompt-free download:

```elisp
(copilot-cs-sh "bash -lc 'cd /workspaces/<dir> && CI=1 gh copilot -- <copilot-arguments>'")
```

This installs the CLI in `gh`'s data directory inside the Codespace. It does
not require copying the operator's local installation or credentials.

### Following a long job

```elisp
(copilot-cs-poll)                      ; most recent job, waits up to 10s
(copilot-cs-poll "job-124114-003")     ; a specific job
(copilot-cs-poll nil 15)               ; longest supported wait
(copilot-cs-poll "job-124114-003" 15)  ; named job with a custom wait
```

Wait values above 15 seconds are clamped. Repeated short polls leave enough
time for MCP request and response overhead while the detached job continues
unaffected in the Codespace.

Repeat until the state is `done`. Then read everything:

```elisp
(copilot-cs-output)
```

Reports include only the tail of the output; `copilot-cs-output` always returns
all of it.

`copilot-cs-poll` reconnects on its own if the connection carrying a job's
output has died, replaying the log from the start. A dropped SSH session
therefore costs nothing but the time to poll again — no output is lost, and
there is no need to notice the drop or do anything about it.

A job that reports `state=failed` never started, so nothing is running; the
output below the status line says why.

### Other job operations

```elisp
(copilot-cs-status)                    ; every job this daemon knows about
(copilot-cs-stop)                      ; ask the most recent job to stop
(copilot-cs-attach "job-124114-003")   ; re-attach to a job from an earlier session
```

Jobs run in their own session in the Codespace, so they outlive the SSH
connection, the Emacs daemon, and the Copilot session that started them. If a
session dies mid-build, `copilot-cs-attach` with the old job id picks the output
back up, including the exit code. Within a session `copilot-cs-poll` already
does this for you; reach for `copilot-cs-attach` when the job id came from an
*earlier* session.

`copilot-cs-stop` signals the job but leaves the wrapper watching it alive, so a
stopped job reports a real exit code (`rc=143`) rather than looking like it ran
away. It refuses to signal a job that has already finished, since the recorded
pid may by then belong to something else.

## Searching the repository

Use **ripgrep (`rg`)** rather than `grep -r`. It is dramatically faster on a
large repository, and it skips `.git/` and `.gitignore`d files by default, so
the results are usually the ones you actually wanted.

`rg` is frequently **not on `PATH`** in a Codespace, but it is almost always
present anyway — vendored inside the VS Code server. Resolve it once, at the
start of the session:

```elisp
(copilot-cs-sh "command -v rg 2>/dev/null || ls -1t /vscode/bin/*/*/node_modules/@vscode/ripgrep*/bin/rg /vscode/bin/*/*/node_modules/@vscode/ripgrep*/bin/*/rg ~/.vscode-server/bin/*/node_modules/@vscode/ripgrep/bin/rg 2>/dev/null | head -1")
```

Each `copilot-cs-sh` call is a **fresh** `sh`, so a shell variable will not
survive to the next call. Note the path it prints and use it literally:

```elisp
(copilot-cs-sh "/vscode/bin/linux-x64/<hash>/node_modules/@vscode/ripgrep-universal/bin/linux-x64/rg -n 'pattern' -g '*.rb'")
```

If that turns up nothing, fall back to **`git grep`** before `grep -r` — it
searches tracked files only and is far quicker than walking the whole tree.

Two ripgrep behaviours are worth remembering, because both cause silent misses
rather than errors:

- It **skips `.gitignore`d and hidden files**. Pass `-u` to include ignored
  files, `-uu` to include hidden ones too.
- It **does not follow symlinks** without `-L`. Vendored and generated
  directories are sometimes symlinked.

Searches are still Codespace commands, so run them through `copilot-cs-sh` like
everything else — a repo-wide search is exactly the kind of command that can
outrun the per-call budget.

## Reading and writing files

Read with an ordinary command:

```elisp
(copilot-cs-sh "cat relative/path/to/file")
```

Write with `copilot-cs-put`, which ships content base64-encoded, so quotes,
newlines, `$`, and backticks need no escaping and arrive byte-for-byte:

```elisp
(copilot-cs-put "relative/path/to/file" "line 1\nline 2\n")
```

Missing parent directories are created. For small, targeted edits `sed` is
usually less work than rewriting the whole file:

```elisp
(copilot-cs-sh "sed -i 's/OLD/NEW/' relative/path/to/file && git diff -- relative/path/to/file")
```

Always confirm edits with `git diff` before running anything against them.

## Clean up

```elisp
(copilot-cs-sh "git checkout -- relative/path/to/file && git status --porcelain")
```

## Security blocklist

The MCP server inspects the Elisp form you submit and refuses it if it names a
blocked function. Blocked (non-exhaustive): `shell-command`,
`shell-command-to-string`, `call-process`, `start-process`,
`async-shell-command`, `directory-files`, `directory-files-recursively`,
`write-file`, `delete-file`, `copy-file`, `rename-file`, `make-directory`,
`getenv`, `setenv`, `load`, `eval`, `with-temp-file`, `kill-emacs`.

Only the submitted form is inspected, not the innards of what it calls. The
`copilot-cs-*` helpers are loaded into the daemon at boot, so they can do things
a form you write directly cannot — which is why the runner works at all.

### File access is confined to the Codespace

`setup/copilot-mcp-init.el` re-permits the file-visiting functions
(`find-file`, `find-file-noselect`, `insert-file-contents`, `write-region`,
`with-current-buffer`) so a remote file can be edited as a buffer, and then
narrows *every* path the daemon may touch to
`/ghcs:<CS_ID>:/workspaces/…`.

Anything else is reported as a sensitive file and refused — including paths on
the operator's own machine, and paths **inside** the Codespace but outside the
working tree, such as the Codespace's `~/.ssh` or `/etc/passwd`. Because the
same check covers both arguments of `copy-file` and `rename-file`, it also
blocks copying a Codespace file out to the local disk.

The check uses `file-in-directory-p`, not a textual prefix: it canonicalizes
`..` components and resolves symlinks before deciding. This matters because
`/workspaces/../etc/hosts` and a symlink below `/workspaces/` pointing to
`/etc` both look in-scope until resolved.

`with-current-buffer` is additionally restricted to this exact target shape:

```elisp
(with-current-buffer (find-file-noselect "/ghcs:<CS_ID>:/workspaces/<dir>/file")
  ...)
```

A literal buffer name, `(get-buffer ...)`, a variable, or any other
buffer-producing form is refused. The upstream MCP check only protected
literal sensitive names, so `(get-buffer "*Messages*")` otherwise bypassed it.
`basic-save-buffer` is also guarded by the visited file's canonical path,
closing the pathless `save-buffer` route.

Verified against a live Codespace: reading `/workspaces/github/README.md`
succeeds, while remote `~/.ssh/id_rsa`, remote `/etc/passwd`, local
`~/dotfiles/init.el`, local `~/.ssh/id_rsa`, traversal through
`/workspaces/..`, a `/workspaces` symlink to `/etc`, and a ghcs→local
`copy-file` are all refused.

Do **not** try to widen this with `mcp-server-security-prompt-for-permissions`.
It asks via `read-char-choice`, which in a headless daemon never returns: the
daemon stops answering `emacsclient` entirely and has to be `kill -9`'d.

## Using TRAMP directly (rarely, and never for commands)

The `/ghcs:` TRAMP method is still configured, and Emacs-native file operations
against `/ghcs:<CS_ID>:/workspaces/<dir>/` can be convenient. **Every one of
them blocks the daemon for as long as the operation takes**, so reach for them
only when an operation is certainly small and the connection is already warm —
and never for running commands, where `copilot-cs-sh` is strictly better.

> **Prompts, not slowness, are what wedge the daemon.** A daemon that asks a
> minibuffer question waits forever, because nobody can answer it: it stops
> serving MCP entirely and only `kill -9` ends it. This was diagnosed by
> stack-sampling a wedged daemon, which sat in
> `find-file-noselect` → `yes-or-no-p` → `read-from-minibuffer` while the
> Codespace itself answered `gh codespace ssh` in 11s.
>
> The trigger is ordinary workflow, not an edge case: edit a file as a buffer,
> then let any `copilot-cs-sh` command change that file on disk — `git checkout`,
> `git pull`, a generator — and the next `find-file-noselect` asks *"File X
> changed on disk. Reread from disk?"* and hangs.
>
> `setup/copilot-mcp-init.el` closes this off, so it should not recur:
> `revert-without-query` silently rereads an unmodified buffer,
> `query-about-changed-file` downgrades the modified-buffer case to a message,
> and `inhibit-interaction` turns every remaining prompt — host keys, passwords,
> supersession-on-save — into an `inhibited-interaction` error. Verified by
> reproducing the exact sequence above: it now returns in 0.4s with the buffer
> correctly reread from disk.
>
> Keep this in mind before adding config that prompts, and note that a stale
> buffer is silently refreshed rather than preserved — never treat an open
> buffer as a durable copy of what you wrote.

### Editing files as buffers

With file access scoped to the Codespace (see **Security blocklist**), a remote
file can be opened, edited, and saved as an ordinary buffer:

```elisp
(with-current-buffer (find-file-noselect "/ghcs:<CS_ID>:/workspaces/<dir>/config/boot.rb")
  (goto-char (point-max))
  (unless (bolp) (insert "\n"))
  (insert "# appended\n")
  (save-buffer)
  (list :size (buffer-size) :saved (not (buffer-modified-p))))
```

This is genuinely useful for a surgical change to an existing file, where
rewriting the whole thing with `copilot-cs-put` would be clumsy. It stays
subject to the blocking rule above, so keep it to small files on a warm
connection; `copilot-cs-put` remains the right tool for whole-file writes and
for anything large.

Always confirm the result from the Codespace side rather than trusting the
buffer's own report — `(copilot-cs-sh "git diff --stat")` is the cheap check.

| Need | Use | Notes |
|------|-----|-------|
| Run any command | `copilot-cs-sh` | Never `process-file`/`start-file-process`. |
| Read a remote file | `copilot-cs-sh "cat ..."` | Not `find-file`/`insert-file-contents`. |
| Write a remote file | `copilot-cs-put` | Base64, so no quoting or escaping issues. |
| Stop a remote job | `copilot-cs-stop` | `kill-process`/`delete-process` are blocked. |
| Push, or sign a commit | `copilot-cs-sh "bash -lc '…'"` | Needs a login shell; see above. |
| Read workflow/job logs | Local `gh run view ... -R "$NWO"` | Never through `copilot-cs-sh`. |
| Copy a local file | `setup/copilot-ghcs cp ... remote:<path>` | Never raw `gh codespace cp`. |

### How large TRAMP transfers are routed

`codespaces.el` registers `ghcs` as a login-only method, so TRAMP transfers
every file inline, base64'd through the shell connection. Measured against a
real Codespace that costs roughly **8s per MB**, which for a few megabytes is
enough on its own to overrun Copilot CLI's per-call budget.

The daemon therefore also teaches `ghcs` to copy *out of band* via
`gh codespace cp`. That route has a flat ~6.5s setup cost and is then quick, so
`tramp-copy-size-limit` is set to 1MB — the measured crossover:

| File size | Inline | Out of band |
|-----------|--------|-------------|
| 256 KB | 2.3s | 6.5s |
| 1 MB | 8.0s | 6.5s |
| 4 MB | 32.0s | 6.7s |
| 16 MB | prompts, then minutes | 4.9s |

Two caveats are handled automatically, and neither needs any thought in normal
use:

- `gh codespace cp` cannot express remote paths containing spaces or shell
  metacharacters — it either fails or silently writes to a backslashed name. A
  guard keeps such paths on the inline route, which handles them correctly.
- Inline transfers above `large-file-warning-threshold` would ask for
  confirmation, and a prompt in a headless daemon hangs it. The threshold is
  disabled.

Size is not the only gate: `tramp-method-out-of-band-p` also picks the
out-of-band route whenever no inline encoding is available for the connection,
regardless of size. So probing the routing decision without a live connection
reports out-of-band for everything — connect first, or the answer is meaningless.

None of this applies to `copilot-cs-sh` or `copilot-cs-put`, which do not use
TRAMP at all. Prefer them regardless of size.
