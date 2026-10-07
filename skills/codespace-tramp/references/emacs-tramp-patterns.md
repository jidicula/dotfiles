# Codespace execution patterns (cookbook)

How to run commands and edit files inside a GitHub Codespace from the local
Emacs daemon behind the `emacs-codespace` MCP server. Referenced by the
`codespace-tramp` skill. Substitute:

- `<CS_ID>` — the immutable Codespace `name` (id) from
  `gh codespace list --json name`.
- `<dir>` — the repo's working directory under `/workspaces/` inside the
  Codespace (discover it; do not assume).

Submit the Elisp examples through **`setup/copilot-emacs-mcp-call`**. This is
the canonical path, not a dependency on a registered JavaScript tool method.
It speaks MCP to the same dedicated daemon and retains its guards, target, and
jobs; it is not raw `emacsclient` evaluation or interactive Emacs. GitHub
control-plane operations still use the operator's local authenticated `gh`.

```bash
/absolute/path/to/skills/codespace-tramp/setup/copilot-emacs-mcp-call \
  '(copilot-cs-status)'
```

An absent method cannot have dispatched an expression, but a timed-out tool
call might have done so. In the latter case inspect status and recover the
known job before deciding to repeat any command. The direct client never resubmits
an evaluation automatically.

Canonical clients take a per-daemon invocation lock **before** starting the
bridge. This serialises local MCP evaluations and helper reloads, not detached
remote jobs. A waiting client gets a fresh response budget when admitted.
`COPILOT_MCP_QUEUE_TIMEOUT` bounds queueing (120 seconds by default);
`COPILOT_MCP_CALL_TIMEOUT` bounds each response (30 seconds by default).
Reusing a healthy daemon does not scan or probe other sessions' daemons.
A queue timeout explicitly says no evaluation was sent; a response
timeout still requires inspecting the original job. Never delete a busy lock
or resubmit a timed-out mutation. Other sessions' daemons have separate locks.
Every invocation uses unique JSON-RPC request ids, so a late or foreign response
cannot be accepted as that invocation's job receipt. Always poll the complete
job id returned by the matching invocation.

The bridge's liveness probe is not a licence to reset a live daemon. A busy
or unresponsive daemon retains its state and the call fails visibly; retry a
read-only status call later. A missing server socket with a still-live recorded
PID also preserves state. Automatic rebuilding is reserved for daemon exits.
Obtain operator approval before stopping a persistently unresponsive live
daemon, and never replay an unconfirmed job after rebuilding.

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
4. A new direct-client invocation or `/mcp` can respawn the bridge. A busy
   daemon is preserved rather than automatically killed; discarding its
   state requires an explicit operator-approved recovery.

`copilot-cs-sh` avoids all of this. It launches work through a *local* child
process, so a slow or stalled connection can never block Emacs; runs the work
in its own session (`setsid`) in the Codespace, so it survives disconnects and
cannot be caught by a signal aimed at the connection; and streams output back
into a local buffer, so polling is instant.

## Setting the target

```elisp
(copilot-cs-use "<CS_ID>" "/workspaces/<dir>")
```

Returns immediately without opening SSH. Call it again to switch Codespaces or
directories. The first real command authenticates with the approval-free
transport key, not Stormbreaker;
an automatic background warm-up used to race that command with a second
connection and could cause one of the concurrent requests to be refused.

Start or restart the selected task Codespace without another prompt; warm up with
`setup/copilot-ghcs ssh "<CS_ID>" true`. The same startup recovery now covers
the first real command and copies, without requiring an extra warm-up or
Secretive approval before every task.

`copilot-gh-retry` accepts only complete known errors from `gh`'s pre-dispatch
phase: SSH RPC `DeadlineExceeded`/`Unavailable`, the GitHub API connection
diagnostic, or a complete Codespace-details refresh error ending in a TLS
handshake timeout or unexpected EOF. The GET-only discovery mode also accepts
those two raw GitHub API GET errors, not arbitrary SSH or tunnel errors.
It requires no stdout and rejects additional diagnostics such as
`shell closed` or `tunnel closed`. Those errors come after ssh/scp may have
executed work and are never replayed. Retries are bounded to three attempts
with five-/ten-second backoff; authentication refusals and interactive shells
are not retried. Missing acknowledgement alone is not a retry criterion.

Pinned-key validation, public-key link preparation, and the SSH handshake
share an advisory signing lock. Valid links are reused, and changed links are
replaced atomically only after acquiring the lock; a waiting connection cannot change another
connection's selected identity. OpenSSH runs with `LogLevel=VERBOSE`; its
complete local `Authenticated to ... using "publickey".` diagnostic releases
the lock, as do remote stdout or process exit. This works with `scp`, which
disables `LocalCommand`, and lets authenticated copies transfer concurrently
without blocking new handshakes.

One 120-second startup deadline covers queueing, pinned-key preparation,
setup, and retry backoff. Automatic agent enumeration and first-key selection
are disabled. A missing or overlapping key stops before SSH/SCP dispatch.
Runner launches and attachments pass their exact acknowledgement marker to
the transport and remain bounded until that whole line arrives. Authentication
alone, another job's marker, and arbitrary output cannot complete a runner's
startup. Expiry stops the local process group and reports an unconfirmed remote
outcome; never replay a mutation or apply a partial transfer on that basis.
Recover the original log or inspect the destination first.

Acknowledged jobs, authenticated language-server connections, and authenticated
copies have no total runtime deadline. SSH sends an encrypted keepalive after
15 seconds without incoming traffic and disconnects after three unanswered
probes. This detects an unresponsive server, not a stalled application whose
server still answers keepalives. The gate is already free in that case.
Never remove a busy lock to bypass it; the runner remains responsive while its
local transport waits.

**Call `copilot-cs-use` before any runner commands, and again after any daemon
restart.** Until a target has been chosen the runner refuses to run at all:

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
yet. Inspect connection diagnostics and poll the same job instead of launching
a duplicate. Ordinary transport should not request Touch ID; investigate an
unexpected prompt rather than approving use of the endorsement key for login.
After ten seconds, transport diagnostics identify whether it is still in
API/SSH setup or awaiting the exact runner acknowledgement. Authentication and
acknowledgement timings are retained for slow connections without changing
retry eligibility or the 120-second startup deadline.

The command is normally a shell string run by a **non-login, non-interactive
`sh`** from the directory given to `copilot-cs-use`. Prefer `sh` syntax over
manual `bash -lc`: the runner automatically gives common Git operations the
login environment they require.

For other commands that need the Codespace's login environment, use
`copilot-cs-login-sh` as described below.

The operator's local `source ~/.shared_shell_configs` convention is not a
remote prerequisite. Minimal/default Codespace images may intentionally lack
that file; send the remote command through the runner rather than sourcing a
nonexistent local dotfile or installing the operator's entire shell setup.
Local headless zsh invocations still load shared configuration and custom
aliases/functions, but skip prompt/completion frameworks and their shared
caches. Terminal sessions retain those frameworks and their plugin aliases.
`script/setup` also links the early `zshenv` guard as `~/.zshenv`: macOS loads
its terminal-session restoration before `.zshrc`, so suppressing that work
there would be too late. Headless shells leave the parent terminal's saved
session untouched; real terminals keep session restoration.

For a non-interactive command whose hook insists on a terminal, use the
explicit remote-PTY helper instead:

```elisp
(copilot-cs-tty-sh "git push --set-upstream origin <branch>")
```

It runs util-linux `script -q -e` **inside** the detached Codespace job, supplies
login state, and propagates the command's exit code. Input is EOF: this is not
an interactive session. The local SSH stream stays a pipe, so closing it still
cannot signal the remote job's process group. Do not add a local PTY to the
runner or use skip-hook flags as a substitute. A missing `script` executable is
an explicit dependency failure, not permission to silently drop the terminal.

The runner routes SSH and out-of-band copies through `setup/copilot-ghcs`. The
helper presents the pinned approval-free public-key stand-in in the base-plus-`.pub`
shape required by `gh`, pins Secretive's agent socket, and enables
`IdentitiesOnly=yes` and `ForwardAgent=no`. All private signing remains in
Secretive. Failed authentication stops instead of silently succeeding with
`~/.ssh/codespaces.auto` or another disk key.

The transport uses an empty SSH config and public-key-only batch mode, so
unrelated configured identities and password prompts cannot provide fallback
authentication. A warning that labels the public stand-in an "unprotected
private key" does not mean it contains one. Inspect the agent failure; do not
export private material or change permissions in Secretive's managed storage.

Do not replace this with raw `gh codespace ssh` or `gh codespace cp`. If
Secretive authentication fails three times, stop and prompt the operator before
trying again; they may be away from the computer and unable to approve the
request. The signing gate counts explicit pre-authentication
`sign_and_send_pubkey` refusals, agent-communication failures, and terminal SSH
`Permission denied (...)` diagnostics across all connections sharing the gate.
Each failed, unauthenticated connection counts once, including when diagnostics
are fragmented or exceed the retry buffer. New and already queued SSH, copy,
attachment, and Eglot connections exit with status 75 without dispatching
SSH/SCP. Existing authenticated streams are unaffected.

Only after the operator explicitly approves recovery:

```bash
setup/copilot-ghcs resume-auth --operator-approved
```

This resets the shared failure count while holding the existing lock. It does
not restart Secretive, change credentials, or replay any operation. Inspect
the original job or destination before explicitly retrying an unconfirmed
operation. Never delete the gate file or reset it automatically. Successful
authentication resets consecutive failures. Network errors, HTTP 403s, warnings
alone, and errors after SSH authentication do not count as authentication failures
or reset the counter.

The gate removes overlapping connection setup from this transport; it does
not change Secretive's key settings. Any persistent refusal still needs
operator attention. Do not infer that a warm-up's temporary success
resolved a recurring agent failure; do not switch keys or disable the agent.
Before an operator-approved retry, confirm the Mac is awake and unlocked.
Secretive's [Secure Enclave key creation](https://github.com/maxgoedjen/secretive/blob/v4.0.0/Sources/Packages/Sources/SecureEnclaveSecretKit/SecureEnclaveStore.swift)
uses `kSecAttrAccessibleWhenUnlockedThisDeviceOnly` even without a
user-presence requirement. A prompt-free key is not guaranteed to work while
macOS is locked. Preserve that protection, and do not attribute a past refusal
to the lock state unless it was observed.

Secretive debug output can report `SSH_AGENT_FAILURE` for an unsupported
`SSH_AGENTC_EXTENSION` even when the subsequent signature succeeds. Correlate
the request type and `SSH_AGENT_SIGN_RESPONSE` with OpenSSH's authentication
result rather than counting every generic agent-failure response as a refusal.

### Reading complete output from concurrent jobs

Each report carries its own `job=...` id. Use it instead of looking up whatever
job another invocation happened to launch or finish last:

```elisp
(let ((report (copilot-cs-sh "cat path/to/file")))
  (if (string-match-p "\\`job=[^[:space:]]+ state=done rc=0 " report)
      (copilot-cs-output (copilot-cs-job-id report))
    report))
```

The runner also restores the returning job as the default after a re-entrant
wait, fixing the immediate `copilot-cs-sh`/`copilot-cs-output` pattern. Across
separate MCP calls, always retain and pass an explicit id. Do not change the
selected Codespace concurrently with launches; each launched job retains its
own target for subsequent polling and cancellation.

Do not issue parallel tool calls to the same daemon. Instead, submit one
labelled batch of zero-wait launches:

```elisp
(list
 (cons "source" (copilot-cs-sh "cat path/to/source" 0))
 (cons "validation" (copilot-cs-login-sh "<validation command>" 0)))
```

Only batch jobs whose runtime resources are independent. Test harnesses often
start services on fixed ports or share databases; separate test files do not
make their processes safe to run concurrently. Combine their selectors in one
test-runner invocation, run them serially, or use the repository's supported
isolation mechanism.

Then poll the two exact ids together in a subsequent call, again labelling
their results. The remote jobs execute concurrently without making one
client queue behind several default ten-second polling waits. Keep
`copilot-cs-use` and its launch in the same evaluation when changing targets.
If a receipt is inconsistent with its requested command, inspect the stored
command/target before using its output; do not relaunch the command.

Nested waits share the earliest active deadline: a new MCP request serviced
inside another request's event loop cannot add its own full wait to the outer
call. Use an explicit zero-second wait when launching a batch, retain each
returned id, then poll the acknowledged jobs. Zero-second waits drain only
their own stream and do not start another event-loop wait.

### Bounded diagnostics and cancellation

For a diagnostic that must not run indefinitely, set a remote command deadline
separately from the short MCP wait:

```elisp
(copilot-cs-timed-sh
 "git --no-pager log --no-decorate --no-show-signature --format='%h %s' -5"
 20 10)
```

Here the remote command gets 20 seconds while the call waits at most 10.
GNU `timeout` reports expiry and sends SIGTERM, escalating after five seconds;
its nonzero status remains visible. Normal `copilot-cs-sh` jobs remain unbounded
so a long build is not mistaken for a stuck diagnostic.

Both `copilot-cs-stop` and its compatibility name `copilot-cs-interrupt` cancel
the original job's descendants. They do not abort Git's sequencer or reset its
index. Poll the cancellation and original jobs, then inspect Git state.

## Semantic code intelligence with Eglot

`copilot-cs-eglot.el` provides Codespace-aware Eglot contacts for Ruby and Go.
The source remains a `/ghcs:` TRAMP buffer, but the language server is launched
as a local `copilot-ghcs` process whose stdin and stdout carry LSP JSON-RPC to
the server inside the Codespace. This avoids Eglot's normal remote
`make-process :file-handler t` path, which can synchronously block Emacs while
TRAMP establishes the connection.

Startup schedules file preparation with a 12-second deadline, then starts the
LSP handshake without a synchronous wait. The handshake has its own bounded
readiness period; a slow server does not extend a runner call's deadline.
Preparation failures and early process exits produce `state=error`, including
available stderr. Failed helper-created servers are stopped without enabling
automatic reconnection. Duplicate pending starts reuse the same attempt, and
`copilot-cs-eglot-stop` invalidates its queued callbacks.

```elisp
(copilot-cs-eglot-start
 "/ghcs:<CS_ID>:/workspaces/<dir>/path/to/file.go"
 'go-mode)
(copilot-cs-eglot-status
 "/ghcs:<CS_ID>:/workspaces/<dir>/path/to/file.go")
```

Wait for `state=ready` and `server=running`, then query that file or another
Ruby or Go file served by the same Codespace project and language server:

```elisp
(copilot-cs-eglot-document-symbols "<remote-path>")
(copilot-cs-eglot-hover "<remote-path>" 36 6)
(copilot-cs-eglot-definition "<remote-path>" 37 15)
(copilot-cs-eglot-references "<remote-path>" 36 6)
(copilot-cs-eglot-diagnostics "<remote-path>")
```

Line numbers are one-based; columns are zero-based. Semantic calls have a
12-second request deadline so one server request cannot consume the complete
MCP budget.

For an unrecorded file, a query reuses a ready server only after checking the
Codespace, project and language. File preparation has its own 12-second bound
and rejects missing files and paths that resolve outside that project. The
requested file gets its own managed buffer and state; requests never use the
representative file's contents or location. Status calls do not open files,
and a query cannot start a missing server or restart a stopped one.
`copilot-cs-eglot-stop` is server-scoped: stopping any managed file stops the
shared language server for every buffer using it in that Emacs session.

The configured servers are:

| Modes | Local project | Codespace project |
|---|---|---|
| `go-mode`, `go-ts-mode` | `gopls` | `gopls` inside the Codespace |
| `ruby-mode`, `ruby-ts-mode` with `sorbet/config` | `bin/srb typecheck --lsp --cache-dir tmp/sorbet` when that binstub is executable, otherwise `bundle exec srb` with the same arguments | The same selection inside a login shell, adding `--disable-watchman` when Watchman is absent |
| `ruby-mode`, `ruby-ts-mode` without `sorbet/config` | `ruby-lsp` | `ruby-lsp` inside a login shell |

The same contacts are loaded by the operator's `init.el`, so visiting a
Codespace file interactively and running Eglot uses the remote server transport
without any Copilot-specific setup.

Automatic reconnection is disabled only for `/ghcs:` servers, including
already connected servers when this configuration is reloaded. The guard also
cancels Eglot's delayed autoreconnect timer: otherwise a later timer can undo
the guard and a background SSH reconnect can revive an inactive task's Codespace.
On `state=disconnected`, check availability and start the selected task's
Codespace if needed without another prompt before calling
`copilot-cs-eglot-start` again. Local and other SSH servers keep their existing
reconnection policy.

### Recovering from an unavailable optional native tool

The MCP registration sets `"deferTools": "never"` and
`"disableToolCache": true`, but an already-running Copilot process can still
temporarily reject `emacs-codespace-eval-elisp` while listing it as available.
Do not repeatedly restart or pipe a bare JSON-RPC request into the bridge: a
long evaluation can still be running when the pipe closes stdin, which makes
the bridge exit before the response arrives.

Use the canonical direct client instead:

```bash
printf '%s' '(copilot-cs-status)' | \
  /absolute/path/to/skills/codespace-tramp/setup/copilot-emacs-mcp-call
```

It performs the initialization handshake, keeps the bridge open until the
matching response arrives, prints the evaluated result, and then disconnects
cleanly. It uses the same `COPILOT_AGENT_SESSION_ID`, so repeated direct calls
reattach to the same daemon and retain its runner state.

### Commands that need the Codespace login environment

`git push`, HTTPS `git fetch`, commit hooks, and repository commands that
fetch authenticated remote data require Codespace login state. Codespaces
injects `GITHUB_SERVER_URL`, `GITHUB_API_URL`, and `CODESPACE_NAME` into
**login shells only**, and several things depend on them:

- `/.codespaces/bin/gitcredential_github.sh` exits without emitting credentials
  unless **both** `GITHUB_TOKEN` and `GITHUB_SERVER_URL` are set, so git falls
  through to prompting and fails with
  `could not read Username for 'https://github.com'`.
- `gpg.program` points at `/.codespaces/bin/gh-gpgsign`, a shim that holds no
  key and POSTs the payload to `$GITHUB_API_URL/vscs_internal/commit/sign`. With
  `GITHUB_API_URL` unset it builds a scheme-less relative URL and dies with
  `unsupported protocol scheme ""`. Manual Codespace sessions retain that
  configuration. Agent runner jobs override implicit signing per process, so
  they must not call this signer for unreviewed work.
- `gh` falls back to the restricted `GITHUB_TOKEN` and `gh auth status` reports
  it invalid.
- Repository validation tools that fetch coverage maps or other protected
  artifacts may report `No token found` when the URLs or related login
  environment are absent.

`GITHUB_TOKEN` **is** present in the non-login shell, which can make missing-URL
problems look like token problems. Confirm before
theorising:

```elisp
(copilot-cs-sh "echo login=$(bash -lc env | wc -l) nonlogin=$(env | wc -l)")
```

`copilot-cs-sh` recognizes direct and chained credentialed or commit-producing
Git operations: `fetch`, `push`, `commit`, `pull`, `cherry-pick`, `revert`,
`merge`, `rebase`, and `am`. It routes them automatically:

Automatic routing is not approval to commit. First follow the skill's
[pre-commit gate](../SKILL.md#gate-0---required-pre-commit-rubber-duck-review):
make the change, run tests/lint in the Codespace, obtain independent rubber-duck
review, then commit and push. The commit example below assumes that gate passed
for the unchanged index.

A worker that cannot launch nested agents hands its validated, frozen candidate
to the parent for a sibling `rubber-duck` review. Wait for the complete result
bound to those parents and that tree; do not commit merely because the worker
finished its implementation.

```elisp
(copilot-cs-sh "git fetch origin main")
(copilot-cs-sh "git commit -m 'Update configuration'")
```

Confirm that the commit's tree and parents match the reviewed candidate before
the separate push:

```elisp
(copilot-cs-sh "git push")
```

The login shell starts with no positional arguments. This matters because
profiles can source version managers such as NVS, which interpret inherited
arguments as commands and may download a runtime. The runner carries the
working directory in a temporary environment variable, removes it after
restoring the directory, and runs the safely quoted command only after login
setup. It does not disable profiles or change the selected runtime version.
The shared Eglot launcher applies the same no-startup-arguments rule, quoting
its already-known remote project directory into the post-login command.

Login-routed commands retain Git's normal configuration; the runner does not
replace it with a `GIT_CONFIG_GLOBAL` override. This dotfiles repository keeps
the HTTPS-to-SSH rewrite in `gitconfig-local`, which `script/setup` hardlinks to
`~/.gitconfig-local` only outside Codespaces. The shared `gitconfig` includes
that optional file; Git ignores the missing include in Codespaces. The existing
`/workspaces/` conditional include still supplies Codespace credentials and
manual-session signing, while shared preferences remain available. Remote
agent jobs append `commit.gpgsign=false` and `tag.gpgsign=false` using
`GIT_CONFIG_COUNT` entries, preserving existing entries and reapplying the
policy after login profiles. Child processes inherit the policy. Nothing is
written to global or repository Git config; deliberate local test-mode jobs
retain their existing defaults.

Update the shared `gitconfig` in existing Codespaces before invoking the direct
client again (or reloading the optional native registration with `/mcp`).
Otherwise an older
Codespace's unconditional rewrite would become active again when the runner's
global-config override is removed. Reloading the runner and Eglot helpers
preserves the selected target, job registry, and tracked Eglot state when the
bridge reuses an existing daemon.

Use the explicit helper for other authenticated repository commands:

```elisp
(copilot-cs-login-sh "<validation-command>")
```

Do not print, copy, or manually export a token. The login shell supplies the
environment through the Codespace's normal configuration.

Ordinary commits remain unsigned. Do not amend them just to obtain a Codespace
API signature, add `-S`, or alter signing configuration before review.
Explicit Git flags can override the environment defaults, so this policy is
not a security sandbox. Inspect newly produced commits before publishing the
initial draft. Read-only commands that need no login state stay on plain `sh`.

### Tag signing and preserved authors

Automatic tag signing is a local-machine preference in `gitconfig-local`.
It must not leak into Codespace test services that create ordinary annotated
tags: those processes may have neither the local signing key nor an API
signer capable of signing their synthetic authors. Updating the shared
`gitconfig` removes that implicit tag-signing request. The agent runner also
suppresses implicit commit and tag signing per process. This is intentional
for unreviewed agent work, not permission to downgrade a repository rule or an
explicitly requested signed release tag.

For `403 | Author is invalid` during a cherry-pick, do not treat login routing
as permission to sign for the preserved author. Inspect `git status` and
`git diff --cached` first; changes may already be staged. Never rerun the
cherry-pick, abort it, or reset the index blindly.

The current runner avoids the API signer and retains the cherry-picked author.
If this legacy failure occurs, reload the runner, inspect explicit signing
overrides, and use the continuation appropriate to the existing Git state.
Never silently reauthor or replay the operation. `copilot-cs-ssh-git` is retired
and rejects all calls: signing a preserved author's commit now belongs to the
same human endorsement gate as every other agent commit.

### Exact-revision endorsement

Follow this skill's [quality and endorsement gates](../SKILL.md#quality-and-endorsement-gates),
starting with validation and rubber-duck review before the first change commit.
After publishing the unsigned draft, continue with the
[CCR commands](copilot-code-review.md) and
[endorsement reference](endorsement.md).
Do not begin signing directly from this execution cookbook. The existing
`copilot-cs-endorse` runner/helper paths remain unchanged.

### Validation readiness and missing artifacts

For a new task, verify the requested base before editing: a new Codespace can
still contain a stale prebuild. Fetch the named base inside the Codespace and
create the new task branch from it when appropriate. Do not reset a dirty
checkout, rewrite a reused branch, or replace an explicitly requested older
revision with the latest default branch.

After refreshing a base branch, run the repository's documented readiness
check in the Codespace before tests or publication. A container can be
`Available` with dependencies from an older revision. If readiness fails, run
the documented bootstrap command there, then repeat readiness and the actual
checks. Do not bypass hooks, reuse a previous revision's green result, or treat
"checks skipped" plus a successful push as validation. For a hook that requires
a terminal, use `copilot-cs-tty-sh` after readiness is established; explicit
validation is still necessary when the hook skips work or treats a timeout as
success.

Respect the repository's Ruby bootstrap boundary. A generated binstub may
already load a standalone bundle; adding a second `require "bundler/setup"`
can reject gems that the application wrapper has already activated. Use the
documented entrypoint rather than inventing a second bootstrap sequence.
Likewise, prefer the repository's executable `bin/srb` to a generic
`bundle exec srb` launcher. Do not upgrade gems, rewrite lockfiles, or skip
bootstrap checks to make an invalid probe pass.

After a stopped Codespace restarts, check its backing services separately.
`Available` and a successful SSH command do not imply that its database has
finished starting. Use repository-documented read-only health checks with both
a per-probe timeout and a bounded overall wait. Report a readiness timeout
explicitly; do not silently continue, restart shared services, or change their
configuration. Once ready, rerun the actual failed validation rather than
assuming that an open service port proves the tests passed.

If concurrent test jobs report occupied ports, inspect which job owns those
listeners and wait for it to finish. Do not kill its services, delete socket
files or rerun a competing harness. Once the previous job is terminal and its
fixtures have stopped, rerun only the selection that never reached its tests.

Use the host, port and authentication path the application actually uses.
For example, `mysqladmin ping` can return zero after printing access denied,
and a healthy default socket says nothing about another configured TCP port.
Require the repository's documented healthy response or a read-only query
through its application configuration. Do not print credentials or replace
authentication checks with a TCP-connect-only probe.

After a base/schema refresh, a healthy database and successful bootstrap can
still leave generated ORM schema metadata stale. For a missing-column or
undeclared-enum error, compare the live columns through the model's configured
test connection with its cached column metadata in a fresh process. Check the
relevant connection role as well; a column in another database is not evidence
that this model can use it. If the live column exists but the generated cache
omits it, use the repository's supported schema-cache generation task in the
correct test environment. Recheck the metadata in a new process and rerun the
original failing selector. Do not drop databases, delete arbitrary cache files,
change schemas or add an explicit model attribute just to hide stale metadata.

Docker `healthy` may describe a supervisor or liveness endpoint even while the
database behind its API is unavailable. A persistent HTTP 503 needs inspection
of the actual database process and startup log, not repeated test launches or
a longer sleep. Check process identity and live socket ownership in the correct
process/network namespaces before treating a PID or socket lock as stale: a PID
can have been reused by an unrelated process or thread. Do not delete/recreate
a container with unmounted data, remove database files, or stop another
session's services to clear it.
Any approved repair must wait until the affected test services are idle and
preserve the existing container data. Test-only feature-management endpoints
also need their own repository-native fixture; an unrelated development
sidecar is not evidence that the test endpoint is ready. A fixture-enabling
flag does not prove that the harness started it; confirm its listener,
readiness and service log. Passing tests with persistent dependency errors
do not establish that the environment problem is resolved.

Check tool availability in the same plain or login runner used for the check.
Some devcontainers' bootstrap scripts omit packages present in their CI image.
If a check reports a missing executable, install that repository-declared
dependency inside the Codespace and rerun the actual check; do not change the
operator's local environment or disable the check.

Also compare actual versions with the repository's manifests, toolchain files
and CI setup. A newer compiler can produce export data an older pinned
analyser cannot read. For Go, select the declared supported toolchain through
job-scoped `GOTOOLCHAIN`, not `go env -w` or a global upgrade. Check
`go env GOROOT GOTOOLDIR GOVERSION` as well as `go version`: an inherited
`GOROOT` can point the selected compiler at another installation's standard
library. Scope it to the same declared installation as `PATH` and
`GOTOOLCHAIN`, or unset that override so Go derives its own matching root.
A successful version command alone does not establish compiler compatibility.
For Rust, install
the declared toolchain/components once and wait for completion before parallel
Cargo/formatter jobs; a stable compiler does not satisfy a separately pinned
nightly formatter. Apply the same check to utility versions such as `jq`.
Install only after a missing/incompatible dependency is established, and
rerun the original failing check.

Keep read-only probes from installing tools implicitly. Mise loads all
configured tools even when `mise exec` names just one runtime; use its
process-scoped [`MISE_AUTO_INSTALL=false`](https://mise.jdx.dev/configuration/settings.html#auto_install)
setting for readiness commands rather than changing global configuration.
First confirm the declared installation with `mise where TOOL@VERSION`, and
check the actual executable's version. Auto-install disabled is not fail-closed
runtime selection: mise can warn about a missing version and execute a
different binary on `PATH`. A successful command with that fallback does not
prove the required runtime is present. Install only an established missing
dependency as a separate, explicit step, then rerun the original check.

The devcontainer editor's `remoteEnv` is separate from the SSH/login
environment. Compare required **non-secret** selectors with the repository's
documented wrapper: a missing `COMPOSE_FILE`, for example, can choose a
different image while both Docker and Bundler are installed. Pass the reviewed
selector explicitly to the affected job. Do not bulk-evaluate devcontainer
JSON, dump environment variables, or copy editor credentials.

A private-package 401/403 is an access failure, not a missing dependency to
replace. Check the documented package/repository grants with existing
authentication, then stop for the operator if access is unavailable. Never
create a PAT, copy local credentials into the Codespace, substitute a package,
or bypass the failed validation.

For GitHub Packages registries supporting granular permissions,
[package-level Codespaces access](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility#ensuring-github-codespaces-access-to-your-package)
is separate from additional repository grants. A successful
`codespaces/permissions_check` does not prove the package is accessible. Ask
the package owner to confirm an approved seamless-access path for the
Codespace's repository; do not assume a local OAuth scope change fixes remote
package authentication.

A cross-repository Git 403 after successful SSH and login setup is likewise a
repository-access problem. Additional permissions in `devcontainer.json`
[apply only to newly created Codespaces](https://docs.github.com/en/codespaces/managing-your-codespaces/managing-repository-access-for-your-codespaces#setting-additional-repository-permissions),
not to an existing Codespace or its rebuild. Use an already-authorised
Codespace for the source repository, or obtain approval for a new Codespace
with the required grants. Neither path authorises copying credentials or
creating a PAT.

Every remote job defaults an unset or empty `LANG` to `C.UTF-8` before starting
its command, avoiding an implicit POSIX/US-ASCII environment. Explicit `LANG`,
`LC_ALL`, and `LC_CTYPE` values are preserved, including settings established by
login profiles. If an explicit setting is unsuitable for Unicode tests, inspect
those three variables and `locale -a`, and select an installed UTF-8 locale for
that command rather than printing secrets from the complete environment.

A selector that cannot download coverage (HTTP 410 means the artifact is
unavailable) has not run its downstream tests, type checks, or linters.
Check for a current, unexpired coverage source through the repository's normal
workflow; repeated downloads of the same expired id cannot repair it. In the
meantime use the repository's documented direct entrypoints for the relevant
tests and checks, with `copilot-cs-login-sh` if they need login state. Keep the
selector failure and any baseline errors visible; do not narrow the suite to
hide them. A dry-run file listing is not a test run.

These checks and bootstrap commands are repository-specific. Keep their exact
commands in the operator's repository notes rather than guessing or adding
monolith-specific commands to this generic skill.

### Git LFS prerequisites

The shared Git config enables required LFS filters. Even `git status` and
`git diff` can invoke them, so a missing `git-lfs` binary is a real dependency
failure, not a reason to disable the filters or skip the affected files.

The minimal Codespace path in this dotfiles repository's `script/setup` installs
missing Git LFS with `apt-get` before its early return. Existing installations
are left alone. Package failures stop setup; images without `apt-get` get an
explicit installation error rather than a successful but incomplete setup.

For an older Codespace reporting `git-lfs: not found`, compare availability:

```elisp
(copilot-cs-sh "git lfs version")
(copilot-cs-login-sh "git lfs version")
```

If only the login environment finds it, use `copilot-cs-login-sh` for the
LFS-dependent Git command. If neither finds it, install it **inside the
Codespace**, using the image's package manager. On Debian or Ubuntu:

```elisp
(copilot-cs-sh "sudo apt-get update && sudo apt-get install -y --no-install-recommends git-lfs && git lfs version")
```

Then rerun the original Git command with the required filters intact. Do not
install work dependencies on the operator's machine or bypass LFS to obtain a
successful status result.

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

If `gh run view --log-failed` cannot download a run archive (for example,
`stream error: stream ID 1; CANCEL; received from peer`), fetch only the
identified job's log through the Actions job endpoint. Keep log control
characters away from the terminal by encoding the response as JSON:

```bash
set -o pipefail
gh api "repos/$NWO/actions/jobs/<job-id>/logs" --allow-escape-sequences |
  python3 -c 'import json, sys; print(json.dumps(sys.stdin.read().replace("\r\n", "\n"), ensure_ascii=True))' \
  > "<session-files>/job-log.json"
```

The escape-sequence flag permits reading the log as data; the JSON encoder
escapes terminal controls and normalises line endings before storage. Do not
print the raw decoded log to the terminal. Check the pipeline's exit status
and the requested job's log content before treating retrieval as successful.
This read-only fallback neither reruns CI nor changes authentication or HTTP
protocol settings. A recovered download does not itself fix the CI failure.

If `gh run rerun` reports a 404 for `/actions/workflows/<id>`, first inspect
the selected run. An enforced workflow can instead have a `workflow_url`
under `/actions/required_workflows/`, which that CLI lookup does not handle:

```bash
gh api "repos/$NWO/actions/runs/<run-id>" \
  --jq '{id, head_sha, status, conclusion, run_attempt, workflow_url}'
```

Confirm the repository, revision and intended run, and check that an earlier
request has not already queued a rerun. When the task authorises rerunning that
completed workflow, address the run directly instead of looking up its
workflow definition:

```bash
gh api --method POST "repos/$NWO/actions/runs/<run-id>/rerun"
gh api "repos/$NWO/actions/runs/<run-id>" \
  --jq '{id, head_sha, status, conclusion, run_attempt}'
```

Do not automatically retry the POST after an ambiguous response; inspect the
same run first. Queued or running attempts still block endorsement, and reruns
can retain the original workflow source revision. A later change to an unpinned
source branch or tag is not, by itself, a reason to rerun successful CI.

For flaky **read-only discovery**, use the bounded GET wrapper:

```bash
/absolute/path/to/skills/codespace-tramp/setup/copilot-gh-retry \
  get "repos/$NWO/codespaces/machines" --jq '.machines[].name'
```

It accepts an endpoint and optional `--jq` only, always uses GET, and retains
`gh`'s stored authentication. Connection errors can be retried before response
output; HTTP errors (including 401, 403, and 410) and partial responses cannot.
Never interpret a failed Codespace lookup as "none exists", and never retry a
create request or advance to another SKU unless a successful lookup has ruled
out an already-created Codespace.

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

For **remote** paths containing spaces or shell metacharacters, use
`copilot-cs-put` with literal content or TRAMP's inline transfer instead. Local
paths may contain spaces when passed as a quoted argument.

### Transferring session patches

An agent-authored file in the local session's `files/` directory is still a
local file. The Emacs MCP evaluator deliberately cannot read it, even inside
`with-temp-buffer` or as an argument to `copilot-cs-put`. Do not weaken the
Codespace-only file guard or retry that local read through Emacs.

Transfer an existing patch with the explicit local copy tool instead:

```bash
/absolute/path/to/skills/codespace-tramp/setup/copilot-ghcs cp "$CS_ID" \
  "$HOME/.copilot/session-state/$COPILOT_AGENT_SESSION_ID/files/change.patch" \
  "remote:/tmp/copilot-$COPILOT_AGENT_SESSION_ID-change.patch"
```

Use this session's artifact and a session-specific remote path. Wait for the
copy to exit successfully before applying it. Select the target checkout, then
use the same concrete session id and remote path in the runner:

```elisp
(copilot-cs-sh "git apply --check /tmp/copilot-<session-id>-change.patch && git apply /tmp/copilot-<session-id>-change.patch && git diff --stat")
```

For small text already available in the conversation, passing a literal string
to `copilot-cs-put` is also supported. Its second argument is the content, not a
filename or an Emacs expression that reads a local artifact.

### Running `gh copilot` without a TTY

`gh copilot` normally prompts before downloading Copilot CLI when the binary is
absent. The runner deliberately has no TTY, so that prompt is unavailable and
`gh` exits with `Copilot CLI not installed`. Set `CI=1` on the first and
subsequent non-interactive invocations; `gh` then performs its supported
prompt-free download:

```elisp
(copilot-cs-login-sh "CI=1 gh copilot -- <copilot-arguments>")
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

Repeat while the state is `connecting`, `running`, or `detached`. Stop on
`done` or `failed`; a failed connection needs the recovery decision below, not
an endless polling loop. Read the complete output with:

```elisp
(copilot-cs-output "job-124114-003")
```

Reports include only the tail of the output; `copilot-cs-output` always returns
all of it.

`copilot-cs-poll` is passive: it never opens a connection. If an acknowledged
job's stream has died, it reports `detached` and preserves the available output.
Check the original Codespace's availability with the local API and obtain
operator approval before starting it if stopped. Then explicitly call
`copilot-cs-attach` with that job's id. A known job uses its original Codespace
and directory even if the selected target changed; an unknown job from an
earlier daemon requires selecting its original target first. Attachment replays
the log, never the command, and does not duplicate live or completed streams.

The daemon remains available while its owning Copilot CLI process is running,
even when a human approval prompt outlasts an hour. The one-hour orphan grace
starts after that process exits; each reattachment refreshes the owner without
resetting the target or job table. Process start times distinguish the owner
from a recycled PID. Manual invocations with no Copilot CLI ancestor use the
bridge-based grace period instead. A live runner connection, including one
still waiting for Secretive, prevents shutdown until it finishes. Retaining
this local state does not reconnect to or keep a Codespace awake.

If a command exits nonzero without writing anything, the runner records
`command exited with status N without producing stdout or stderr`. This is a
real silent failure rather than lost output; split the command or add
command-specific diagnostics to identify the failing step.

`state=failed` means the connection ended without an acknowledgement. The
original output and connection exit status remain available; repeated polling
does not open another connection or replace the error with a missing-log
message. `copilot-cs-status` uses the same states, so it does not mislabel these
jobs as running or detached.

**The remote outcome is unconfirmed, not necessarily "nothing is running".**
The connection could have failed after starting the command but before its
acknowledgement arrived. Read the original error first. If the outcome is
ambiguous, select the original Codespace and use `copilot-cs-attach` with the
same job id to inspect its log and check the command's expected effects before
deciding whether to run it again. A missing log alone is not proof that a
command never ran. Authentication retries still obey the three-failure limit.

### Analysing retained job output safely

Keep log parsing, grouping, and filtering outside the dedicated Emacs daemon.
Even a read-only loop can block every later MCP call, and the client's response
timeout does not interrupt that evaluation. Retrieve an explicit job's output
in a separate call; do not batch analysis with new job launches.

The MCP provider prints Elisp string literals, which are not a general JSON
encoding of arbitrary log text. Export UTF-8 as unwrapped base64 so the returned
literal is safely JSON-decodable, then decode and store the log as escaped JSON
outside Emacs:

```bash
set -o pipefail
/absolute/path/to/skills/codespace-tramp/setup/copilot-emacs-mcp-call \
  '(base64-encode-string
     (encode-coding-string (copilot-cs-output "job-124114-003") (quote utf-8))
     t)' |
  python3 -c 'import base64, json, sys; text = base64.b64decode(json.loads(sys.stdin.read()), validate=True).decode("utf-8"); print(json.dumps(text, ensure_ascii=True))' \
  > "<session-files>/job-output.json"
```

Use this session's existing `COPILOT_AGENT_SESSION_ID`, a known job id, and an
artifact path in this session's `files/` directory. Check the pipeline's exit
status before using the artifact; a failed capture is not an empty successful
log. The decoder requires only the Python standard library already used by
the client, not repository dependencies on the host. Keep control characters
escaped rather than printing the raw decoded log to the terminal.

Analyse the artifact in a separate, bounded process, loading its text with
`json.loads`. If that analysis stalls, cancel only the analysis process.
Do not use a live daemon to debug an Elisp regex loop: helpers such as
`split-string` can replace match data before a later `match-end` advances the
cursor. Use a small fixture in an isolated, timeout-bounded batch Emacs instead.
Stopping a live session daemon still requires operator approval; a timeout
never authorises replaying unconfirmed commands.

### Other job operations

```elisp
(copilot-cs-status)                    ; every job this daemon knows about
(copilot-cs-stop)                      ; ask the most recent job to stop
(copilot-cs-attach "job-124114-003")   ; re-attach to a job from an earlier session
```

Jobs run in their own session in the Codespace, so they outlive the SSH
connection, the Emacs daemon, and the Copilot session that started them. If a
session dies mid-build, `copilot-cs-attach` with the old job id picks the output
back up, including the exit code. Use the same explicit recovery when a stream
disconnects within the current session. Status and output reads do not restart
a stopped Codespace.

`copilot-cs-stop` freezes the supervisor and all descendants parent-first before
sending `SIGTERM` to the descendants. Suspending parents first keeps shell
`wait` calls from treating a stopped child as a completed command. Processes
that ignore `SIGTERM` receive `SIGKILL` after five seconds. The supervisor is
resumed rather than terminated, so it records the command's actual exit code,
usually `rc=143` or `rc=137`.

Cancellation itself is a runner job: poll its returned id to confirm success,
then poll the original job for its exit code. Surviving descendants produce a
nonzero cancellation result. The helper checks the persisted completion marker,
supervisor identity, and descendant start times before signalling; completed
jobs and stale or unrelated PIDs are not treated as live cancellation targets.
Failed cancellation resumes processes it suspended.

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

For an existing local artifact, use the
[session-patch transfer workflow](#transferring-session-patches), not a local
`insert-file-contents` call inside MCP.

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
the operator's own machine (including this session's authored artifacts), and
paths **inside** the Codespace but outside the
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

If a ghcs connection has gone stale, the dedicated daemon automatically cleans
it and retries one top-level `find-file-noselect`, `insert-file-contents`,
`write-region`, or buffer save. Nested primitives do not multiply the retry. A
second `remote-file-error` is returned unchanged so the workflow never loops
indefinitely.

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
| Fetch, push, or make an unsigned commit | `copilot-cs-sh "git …"` | Login environment and unsigned defaults are automatic. |
| Review and endorse a draft | [Quality and endorsement gates](../SKILL.md#quality-and-endorsement-gates) | CCR/CI and explicit human approval precede signing with `copilot-cs-endorse`. |
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
