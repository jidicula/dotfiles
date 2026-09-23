# Codespace TRAMP

`codespace-tramp` is a GitHub Copilot CLI skill that keeps work-repository changes, dependency installation, language-server indexing, builds, and tests inside GitHub Codespaces. It drives each Codespace through a dedicated local Emacs daemon, the Model Context Protocol (MCP), Eglot, and the `/ghcs:` TRAMP method.

The result is a stateful remote development session: Emacs preserves the selected Codespace, open buffers, running processes, and job metadata across Copilot turns, while commands continue running inside the Codespace even if SSH, MCP, or the Copilot session disconnects.

The complete agent workflow is defined in [`SKILL.md`](SKILL.md). The command and file-operation details are in [`references/emacs-tramp-patterns.md`](references/emacs-tramp-patterns.md).

## The problem

Copilot CLI normally operates in its local working directory. That is undesirable for work repositories when:

- dependencies are large, platform-specific, or easier to maintain in a repository-provided development container;
- large source trees or deep Git histories consume enough disk space that local worktrees are impractical;
- source changes must not be made on the operator's machine;
- builds and tests need the same environment used by other Codespace users;
- long-running work must survive a dropped SSH connection or CLI restart; or
- several Copilot sessions need independent remote state.

Using individual `gh codespace ssh` calls does not solve the whole problem. Each call is effectively stateless, and a command that outlives the connection can lose its output and process state.

Using ordinary synchronous TRAMP command primitives is also unsafe here. Emacs is single-threaded, so a slow remote command or an unanswered SSH prompt blocks the entire daemon. If that delay exceeds Copilot CLI's tool-call budget, the MCP bridge may be terminated and every later call can fail with `Transport closed`.

A single shared Emacs daemon compounds the problem: one blocked session stalls every Copilot session using that daemon, as well as the operator's interactive Emacs.

## How it solves the problem

The skill separates provisioning, control, file access, and command execution:

```text
Copilot CLI
  |
  | MCP messages encoded as JSON-RPC over stdio
  v
copilot-emacs-mcp
  |
  | local Unix socket
  v
per-session Emacs daemon
  |                         |                         \
  | /ghcs: TRAMP            | copilot-cs-* jobs       \ Eglot LSP JSON-RPC
  | file operations         | through SSH              \ through SSH
  v                         v                           v
GitHub Codespace repository and development environment

Local authenticated gh
  |
  +-- Codespace provisioning and state
  +-- pull request and workflow operations
```

The main design choices are:

1. **One disposable Emacs daemon per Copilot session.** A blocked or crashed session cannot affect another session or the operator's interactive Emacs.
2. **MCP as the local control channel.** The canonical `copilot-emacs-mcp-call` client invokes the daemon's narrow `eval-elisp` tool directly, without depending on Copilot CLI's MCP tool registry. MCP uses JSON-RPC to initialize the connection, correlate responses by request id, and return errors.
3. **TRAMP for guarded file access, not arbitrary remote commands.** The daemon understands `/ghcs:` paths and allows edits only beneath `/workspaces/` in a Codespace.
4. **A non-blocking command runner.** `copilot-cs-sh` starts SSH in a local child process, launches the remote work under `nohup` and `setsid`, and streams output into an Emacs buffer. MCP polling reads that local buffer instead of waiting synchronously on SSH.
5. **Remote job persistence.** Scripts, logs, process ids, and exit status live under `~/.copilot-cs-jobs` in the Codespace. A job can be polled or reattached after a transport or session failure.
6. **Separate transport and endorsement identities.** Ordinary SSH pins an approval-free Secretive key with `IdentitiesOnly=yes` and `ForwardAgent=no`. A separate Touch ID-protected key signs reviewed commits only through the endorsement workflow; neither role falls back to an on-disk private key.
7. **Local GitHub control-plane operations.** The operator's authenticated local `gh` creates and inspects Codespaces and pull requests. Repository commands run remotely; local credentials are never copied into the Codespace.
8. **Codespace-native language intelligence.** Eglot keeps source buffers local to Emacs while running gopls, Sorbet, or Ruby LSP inside the Codespace over a dedicated Secretive-backed stdio process. Definitions, references, hover information, document symbols, and diagnostics therefore use the repository's remote checkout and dependencies.
9. **Revision-specific human endorsement.** Agent commits remain unsigned until the operator reviews the draft and chooses its publication method. Every introduced commit is then reconstructed and individually signed, without flattening merges, dropping empty commits, or changing authorship.

## Workflow

When invoked, the skill:

1. Confirms that the current directory is within its configured work-repository scope. This installation is restricted to `~/work/github/`.
2. Collects a repository, an optional issue or pull request, and any additional instructions.
3. Derives recognizable session and Codespace names from the repository and reference.
4. Offers to reuse a matching Codespace or create a separately named one.
5. For a new Codespace, ranks every available machine by CPU count, memory, and storage. It tries the largest machine first and falls back through each next-largest SKU if creation fails.
6. Selects the development-container configuration, obtains its required repository-permission approval, and waits for the Codespace to become available. It never silently opts out of permissions needed by dependency bootstrap.
7. Points the dedicated Emacs runner at the immutable Codespace id and the discovered repository directory under `/workspaces/`.
8. Uses Codespace-hosted Eglot servers for semantic navigation and diagnostics when working in Ruby or Go.
9. Makes changes and runs builds, tests, linters, and other repository commands through detached `copilot-cs-*` jobs.
10. Commits and pushes retained changes unsigned from the Codespace, then uses local `gh` to create or update a draft pull request unless the user explicitly requested otherwise.
11. Prompts for endorsement of the exact revision and a publication choice: an exact-head lease update when allowed, or a new `-signed` draft. Rejection or cancellation leaves the draft unendorsed. Replacement drafts are verified and linked before the original is closed.
12. Leaves the resulting PR in draft and the Codespace running so its configured idle timeout can stop it.

## Requirements

The local machine needs:

- GitHub Copilot CLI with skills and MCP support;
- an authenticated [GitHub CLI](https://cli.github.com/);
- Emacs and `emacsclient`;
- [`socat`](http://www.dest-unreach.org/socat/);
- Python 3 for the connection wrapper and direct MCP client;
- [Secretive](https://github.com/maxgoedjen/secretive) with distinct approval-free transport and Touch ID-protected endorsement keys;
- [`patrickt/codespaces.el`](https://github.com/patrickt/codespaces.el) for the `/ghcs:` TRAMP method; and
- an Emacs MCP server package providing `mcp-server` and `mcp-server-security`.

The supplied daemon init resolves Emacs packages from a `straight.el` `straight/build/` directory. Adapt the load-path setup in [`setup/copilot-mcp-init.el`](setup/copilot-mcp-init.el) when using another package manager.

The target GitHub repository must permit Codespaces and expose at least one machine type and development-container configuration. Semantic Ruby support requires Sorbet or Ruby LSP in the Codespace; semantic Go support requires gopls.

Endorsement additionally requires Git, Python 3 with its full standard library, and OpenSSH's `ssh-keygen -Y sign`/`verify` locally and in the Codespace. A minimal Python package can report a version while lacking modules such as `json`; repair the runtime inside the Codespace if imports fail. Its initial implementation supports same-repository github.com draft PRs with complete history. Fork PRs, signed merge tags, and Git grafts/replacement objects stop with explicit errors. If repository rules prohibit unsigned draft branches, obtain a policy decision rather than signing agent work prematurely.

### Secretive key roles

| Role | Public-key alias | Use |
|---|---|---|
| Transport | `~/.ssh/secretive-codespaces-agent-sep-2026{,.pub}` | Unattended Codespace SSH, copies, and Eglot; no Touch ID requirement |
| Endorsement | `~/.ssh/secretive-stormbreaker-github-sep-2026.pub` | Every reviewed commit; remains the signing key in `gitconfig-work` |

Both transport paths are symlinks to the same Secretive-managed **public** key, not a private/public pair. Create these aliases when installing on another machine; never repoint Stormbreaker to the transport key. `COPILOT_SECRETIVE_STANDIN` and `COPILOT_SECRETIVE_PUBLIC_KEY` select transport paths; `COPILOT_SECRETIVE_SIGNING_PUBLIC_KEY` independently selects the endorsement public key. The transport does not enumerate the agent or choose its first key, and fails if the roles overlap.

Register the endorsement public key with GitHub as a **signing** key. The transport key does not need general GitHub SSH authentication registration for `gh codespace ssh`, and must not serve as the endorsement key. Keep user-presence protection enabled on Stormbreaker; **Leave Unlocked** may reuse approval rather than require fresh Touch ID for each signature. The signing transport explicitly pins both authentication and the forwarded agent socket to Secretive, regardless of the daemon's inherited `SSH_AUTH_SOCK`. Forwarding exposes the agent, not just one key, for the lifetime of the approved signing connection.

## Installation

1. Put this directory beneath a Copilot skill search root. This repository exports its skill root with:

   ```sh
   export COPILOT_SKILLS_DIRS="$HOME/dotfiles/skills"
   ```

2. The direct client starts or reuses the per-session daemon without requiring a Copilot MCP registration. If you also want the native MCP tool available, optionally register the bridge in `~/.copilot/mcp-config.json`:

   ```json
   {
     "mcpServers": {
       "emacs-codespace": {
         "type": "stdio",
         "command": "/absolute/path/to/skills/codespace-tramp/setup/copilot-emacs-mcp",
         "args": [],
         "tools": ["eval-elisp"],
         "deferTools": "never",
         "disableToolCache": true
       }
     }
   }
   ```

   Eager discovery and disabled tool caching reduce stale snapshots, but the canonical direct client does not depend on this optional registration.

3. Ensure these helper programs are executable:

   ```sh
   chmod +x \
     setup/copilot-emacs-mcp \
     setup/copilot-emacs-mcp-call \
     setup/copilot-ghcs \
     setup/copilot-gh-retry \
     setup/copilot-cs-endorse \
     setup/copilot-issues-lock
   ```

4. Invoke `setup/copilot-emacs-mcp-call '(copilot-cs-status)'` to connect. If using the optional registration, reload MCP with `/mcp`; its tool is named `emacs-codespace-eval-elisp`.

5. If this skill is being shared, change the scope near the top of [`SKILL.md`](SKILL.md). The checked-in configuration intentionally refuses to activate outside `~/work/github/`.

## Interactive Emacs

This repository's [`init.el`](../../init.el) adds the skill's `setup/` directory to `load-path` and requires `copilot-cs-eglot` from the normal Eglot configuration. The same Ruby and Go contacts are therefore usable by a person visiting `/ghcs:` files: local files use ordinary local language servers, while Codespace files start the server inside that Codespace through Secretive-backed SSH.

Both `go-mode`/`go-ts-mode` and `ruby-mode`/`ruby-ts-mode` call `eglot-ensure`. Ruby selects Sorbet when the project contains `sorbet/config`, otherwise Ruby LSP; Go selects gopls.

## Usage

Ask Copilot CLI to perform repository work in a Codespace, for example:

```text
Use a Codespace to fix OWNER/REPOSITORY issue URL.
```

```text
Run the test suite for OWNER/REPOSITORY in a Codespace without changing files.
```

```text
Make the requested change in OWNER/REPOSITORY in a Codespace and use branch
my-feature-branch.
```

If an input is missing, the skill asks for the repository, reference, and additional instructions separately. It also prints a paste-ready `/rename` command so concurrent sessions remain identifiable.

The agent, rather than the user, drives the normal `copilot-cs-*` calls. For diagnosis or manual recovery, the core Elisp interface is:

```elisp
(copilot-cs-use "<codespace-id>" "/workspaces/<repository>")
(copilot-cs-sh "command to run")
(copilot-cs-poll "job-id")
(copilot-cs-output "job-id")
(copilot-cs-attach "job-id")
```

For semantic code intelligence:

```elisp
(copilot-cs-eglot-start "<remote-path>" 'ruby-mode)
(copilot-cs-eglot-status "<remote-path>")
(copilot-cs-eglot-document-symbols "<remote-path>")
(copilot-cs-eglot-hover "<remote-path>" 12 4)
(copilot-cs-eglot-definition "<remote-path>" 12 4)
(copilot-cs-eglot-references "<remote-path>" 12 4)
(copilot-cs-eglot-diagnostics "<remote-path>")
```

See the [execution cookbook](references/emacs-tramp-patterns.md) for file editing, polling, login-shell commands, copies, and recovery procedures.

## Safety boundaries

The implementation deliberately fails closed:

- The skill refuses to apply outside its work-repository path.
- The runner refuses every command until `copilot-cs-use` explicitly selects a target. A restarted daemon cannot silently fall back to the local machine.
- MCP file edits are restricted to `/workspaces/` inside a `/ghcs:` remote.
- Interactive Emacs prompts are inhibited because no person can answer a minibuffer prompt in the background daemon.
- Secretive authentication failures do not fall back to another SSH identity.
- Agent forwarding is restricted to `copilot-cs-endorse "sign"` after the interactive revision-and-publication prompt. The old unrestricted `copilot-cs-ssh-git` API rejects calls. Planning, verification, pushes, copies, and reattachments do not forward the agent.
- Agent jobs append process-scoped unsigned Git defaults before commands and after login profiles, preserving other Git configuration and child-process inheritance. Explicit Git flags can override defaults, so the skill also prohibits premature signing and checks initial agent commits. Manual Codespace sessions and local Git defaults are unchanged.
- Every endorsement prompt starts with the full draft PR URL on its own line, repeated in ordinary chat immediately before the form so the review link is easy to find in scrollback. Revision details follow the review question, not the link.
- Every approval binds the head, base, complete introduced commit range, key fingerprint, and publication options. Signing/pushing requires both the exact plan id and the operator's publication choice; none is selected automatically. Existing `-signed` branches are not overwritten.
- Publication eligibility checks effective rulesets and classic branch protection separately. A ruleset can mark a branch `protected` without prohibiting rewrites; `protection.enabled: false` explicitly rules out classic protection. Rewrite-blocking rules and unreadable or ambiguous policy still suppress the in-place option.
- In-place publication uses `--force-with-lease=<source-ref>:<reviewed-head>` only for that endorsement's explicit choice. New-branch publication uses an empty-expectation lease as a create-only compare-and-swap: it cannot replace an existing branch. Neither path uses unconditional `--force`, changes the original local branch, or deletes it.
- Finalisation independently verifies the expected SSH key and the published commit content. Replacement draft metadata and signatures are checked before linking and closing the original. GitHub's generic verification badge alone is insufficient, and non-atomic PR/branch updates still require coordination with concurrent writers.
- Git retains shared global defaults and conditionally includes Codespace credentials and commit signing. Host-only URL rewrites and automatic tag signing live in `gitconfig-local`, which `script/setup` hardlinks as `~/.gitconfig-local` only outside Codespaces. Synthetic test tags no longer inherit a requirement for the operator's unavailable signing key.
- Minimal Codespace setup installs missing Git LFS before returning, so the shared configuration's required filters work even for `git status`. It does not disable filters to hide a missing dependency.
- Local session artifacts use the explicit Secretive-backed copy transport. They do not gain access through Emacs' Codespace-only file guard; see the [session-patch workflow](references/emacs-tramp-patterns.md#transferring-session-patches).
- Codespace Eglot servers are launched only through the Secretive-backed transport and receive repository paths with the TRAMP prefix removed.
- GitHub API and pull-request operations use local `gh`; local credentials are not transferred into the Codespace.
- Repository readiness must be rechecked after a base-branch refresh. Skipped hooks, expired coverage artifacts, and dry-run test listings are not successful validation. A Codespace signer rejecting a preserved author is not permission to silently change authorship.
- Remote jobs default an unset or empty `LANG` to `C.UTF-8`; explicit `LANG`, `LC_ALL`, and `LC_CTYPE` settings retain their normal precedence. Local test-mode jobs do not change the operator's locale.
- Login setup receives no command arguments, preventing sourced version managers such as NVS from treating a Git operation or Eglot server launch as a runtime request. Both launch paths preserve the working directory, quoting, and exit status without bypassing login profiles.
- Reusing an existing Codespace and starting a billable `Shutdown` Codespace retain their explicit confirmation gates. Machine sizing is the exception: the skill automatically tries available SKUs from largest to smallest.

## Recovery model

The workflow is designed so a transport failure does not automatically discard the work:

- `copilot-emacs-mcp` reuses a healthy daemon for the same Copilot session and replaces a wedged daemon when necessary. Each direct-client invocation reloads the runner and Eglot helpers without resetting the selected target, existing jobs, or tracked Eglot state. Reload the optional native MCP registration after updating the skill if using that route instead.
- A daemon remains available for a grace period after its bridge disappears, and a live runner connection prevents the orphan watchdog from stopping it.
- Remote jobs remain detached in the Codespace independently of the daemon.
- `copilot-cs-attach` reconnects the local runner to an existing remote job.
- Polling automatically reconnects only acknowledged jobs, using their original Codespace. A connection that ends before acknowledgement keeps its original diagnostics and an explicitly unconfirmed remote outcome; it is not silently replaced with a missing-log error or retried as a new command.
- `copilot-ghcs` retries complete, known API/SSH startup failures at most twice, with five- and ten-second backoff, including on the first task and before a copy. It requires proof that `gh` failed before dispatch and no stdout; missing acknowledgement alone is never enough. Remote command/transfer failures, authentication refusals, and interactive shells are never replayed.
- A shared advisory gate covers pinned-key validation, public-key link preparation, and SSH handshakes until OpenSSH confirms public-key authentication, remote output arrives, or the connection exits. Links are replaced atomically only when necessary; queued connections cannot replace an active identity or contact the agent while another handshake holds the gate. Authenticated copies no longer monopolise the gate until transfer completion.
- Three consecutive pre-authentication signing refusals pause new and already queued connections across sessions and Codespaces sharing that gate. They fail before SSH/SCP dispatch with status 75; already authenticated work continues. After explicit operator approval, `setup/copilot-ghcs resume-auth --operator-approved` clears the refusal count without replaying any command or changing credentials.
- Queueing, pinned-key preparation, connection setup, and safe retries share one 120-second startup budget. Runner connections must receive their exact acknowledgement within it. Timeouts preserve an unconfirmed remote outcome and never replay the command. Acknowledged jobs and authenticated copies have no arbitrary runtime limit. SSH keepalives detect an unresponsive server after approximately 45 seconds, but cannot diagnose a stalled application on an otherwise responsive server.
- Endorsement records live in the repository's Git common directory under `copilot-endorsements/`. Completed receipts can be verified and publication resumed without re-signing. Partial signing failures create no signed branch and are never replayed automatically. A forwarded socket disappears with its connection even though the detached signing process may still exist; inspect its original log before an operator-approved retry.
- Returning job reports restore their own default output id after re-entrant waits, so immediate parallel reads cannot substitute another invocation's output. Explicit ids remain required across separate MCP calls.
- Re-entrant waits share the earliest active deadline, so queued MCP requests cannot extend an outer call past its wait budget. A zero-second wait drains only its own stream without servicing another request or timer.
- `copilot-cs-tty-sh` supplies a remote PTY for non-interactive terminal-only hooks, preserves their exit status, and keeps the local SSH stream a pipe.
- `copilot-cs-timed-sh` adds an explicit remote deadline for bounded diagnostics without changing the default lifetime of builds. `copilot-cs-interrupt` is a compatibility name for scoped cancellation, not a Git abort operation.
- `copilot-cs-job-id` extracts the complete id from a returned report so callers need not retype or truncate its suffix. Genuine daemon replacement still uses the existing remote-log reattachment workflow.
- `copilot-cs-stop` cancels the full descendant tree, escalates processes that ignore `SIGTERM`, and preserves the supervisor's exit-code reporting. Its cancellation job reports failure if descendants remain alive.
- `copilot-emacs-mcp-call` is the canonical entrypoint, performing the MCP initialization and `tools/call` exchange without depending on Copilot's in-memory native-tool registry.

The direct client is protocol-aware: unlike piping one JSON-RPC line into the bridge, it keeps stdin open until the matching response arrives. Its bounded byte-stream reader handles coalesced notifications and incomplete frames. Protocol errors, `isError` results, and the Emacs provider's bare `Error:` diagnostics produce nonzero exit statuses; quoted successful string results remain data. It never automatically resubmits an evaluation; a timed-out mutation must be checked through the job registry first.

## Components

| Path | Purpose |
|---|---|
| [`SKILL.md`](SKILL.md) | Agent contract, provisioning workflow, publication rules, and troubleshooting |
| [`references/emacs-tramp-patterns.md`](references/emacs-tramp-patterns.md) | Command, polling, editing, transfer, and recovery cookbook |
| [`setup/copilot-emacs-mcp`](setup/copilot-emacs-mcp) | Per-session daemon launcher and resilient stdio-to-Unix-socket bridge |
| [`setup/copilot-mcp-init.el`](setup/copilot-mcp-init.el) | Minimal `emacs -Q` configuration, MCP server, TRAMP guards, and daemon watchdog |
| [`setup/copilot-cs-jobs.el`](setup/copilot-cs-jobs.el) | Non-blocking detached Codespace command runner and job registry |
| [`setup/copilot-cs-endorse`](setup/copilot-cs-endorse) | Exact-revision signing, per-endorsement publication choice, and verified draft replacement |
| [`setup/copilot-cs-stop`](setup/copilot-cs-stop) | Scoped process-tree cancellation, escalation, and stale-PID protection |
| [`setup/copilot-cs-eglot.el`](setup/copilot-cs-eglot.el) | Shared Eglot configuration, remote language-server transport, and semantic query helpers |
| [`setup/copilot-ghcs`](setup/copilot-ghcs) | Secretive-only wrapper for Codespace SSH and copies |
| [`setup/copilot-gh-retry`](setup/copilot-gh-retry) | Pre-dispatch retries, shared signing gate, and strictly GET-only API discovery |
| [`setup/copilot-emacs-mcp-call`](setup/copilot-emacs-mcp-call) | Canonical, protocol-aware direct MCP client |
| [`setup/copilot-issues-lock`](setup/copilot-issues-lock) | Transactional lock for concurrent issue-log updates |
| [`ISSUES.md`](ISSUES.md) | Durable symptom reports and resolutions for workflow failures |

## Maintainer regression tests

Run the local regression cases with the existing Python, Emacs, Git, and shell tools; no additional packages or live GitHub credentials are needed:

```sh
python3 -m unittest discover -s skills/codespace-tramp/tests -v
```

The cases cover local-only Git config and tag signing, unsigned agent jobs, key-role separation, approval gates, signature verification, preserved merge/empty-commit history, lease races, safe PR replacement/recovery, Codespace LFS installation, pre-dispatch retry boundaries, exact job ids, deadlines, MCP framing, re-entrant output isolation, remote PTYs, daemon refresh, and process-tree cancellation. Signing uses disposable test keys and a dedicated temporary SSH agent, never Secretive. Git remotes and GitHub API responses are isolated fixtures; no real PR, branch, or account is mutated.

## Reporting problems

Unexpected MCP, Emacs, TRAMP, SSH, Codespace, or runner behaviour must be recorded immediately in [`ISSUES.md`](ISSUES.md), even when a workaround is available or the problem is fixed in the same session.

Reports describe observable symptoms only. Updates are serialized through `setup/copilot-issues-lock` so concurrent Copilot sessions cannot overwrite each other's issue-log or Git-index changes.
