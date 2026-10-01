# Exact-revision endorsement

The quality gates and publication policy are defined in
[`codespace-tramp`](../SKILL.md#quality-and-endorsement-gates). Complete required CI and the optional CCR
loop before using `prepare`; `check-ci` is the standalone read-only CI gate.
The helper implements CI, revision and signing guards. CCR interpretation and
remediation are agent-driven policy in the skill, not a text classifier inside
the signing helper.

Use this skill's existing runtime. Ordinary SSH, copies and
Eglot use `~/.ssh/secretive-codespaces-agent-sep-2026{,.pub}`. Endorsement uses
the distinct `~/.ssh/secretive-stormbreaker-github-sep-2026.pub`. Changing the
transport key must never repoint `gitconfig-work`. Both roles use
Secretive-managed public keys; no private key or GitHub credential is copied.
The canonical client and remote runner are described in the
[execution cookbook](emacs-tramp-patterns.md).

## Prepare and plan

After the quality gates, use local preparation with the operator's
authenticated `gh`:

```sh
/absolute/path/to/skills/codespace-tramp/setup/copilot-cs-endorse \
  prepare --repo "$NWO" --pr "$PR_NUMBER"
```

`prepare` repeats the same required-check policy as the final readiness gate,
including pagination, missing configured checks, and fail-closed handling of
unreadable or unsupported policy. It requires an open draft with unchanged
live head/base refs and performs no PR mutation. Only after CI passes does it
read the signing key, record the authenticated operator as `attestor`, prepare
publication choices, and emit the request. Compare that head/base with the
completed quality-gate evidence; changed state must return to those gates.

Pending, failing, missing or absent required checks produce a nonzero result
and no request. Do not start a remote plan, ask for an endorsement or committer
override, or forward the signing agent while either pre-endorsement gate is
blocked. A cached request is not a substitute for checking the current revision.

Existing committer identity is preserved unless the operator explicitly
authorises `--committer-name "<name>" --committer-email "<email>"`. Both values
are required and become part of the exact plan shown at the approval prompt.
Preparation checks the selected key against the authenticated operator's
registered signing keys; the supplied email must belong to that account.
Authors and all timestamps remain unchanged. Never change attribution or
Git/Secretive settings automatically to obtain GitHub's verification badge.

`gh api user/emails` is an optional identity diagnostic, not a required
preparation step. A scope-related 403/404 does not establish whether an address
is verified. If that lookup is unavailable, ask the operator to confirm a
verified account email before proposing a committer override, or leave the
draft unendorsed. Do not refresh authentication scopes, switch accounts or use
a public profile email as verification evidence. Finalisation still requires
GitHub to verify the actual published signatures.

Pass the successful request as an escaped string, not an Elisp expression or
shell command, to the already selected Codespace:

```elisp
(copilot-cs-endorse "plan" "<JSON returned by prepare>")
(copilot-cs-poll "<planning-job-id>")
(copilot-cs-endorsement-result "<planning-job-id>")
```

Use the complete result, not a status report's truncated tail. Planning requires
the local source branch to match the draft head and tracked changes to be
clean. It snapshots the remote head/base and records every introduced commit
in topological order. `publication_options` lists what may be offered; it is
not a publication selection or an approval.

If the prepared base commit is missing, planning fetches that exact OID and
its history from the verified origin. It does not update local or
remote-tracking refs, tags, `FETCH_HEAD`, the index or working tree. Source
and base refs are checked again after the fetch. Failures or concurrent changes
stop planning without recording a plan; they never refresh reviewed OIDs silently.

The history fetch streams progress into its planning job and, like a push,
has no total-duration cap. A healthy large transfer must not be killed by the
120-second metadata deadline. Poll the same job; use `copilot-cs-stop` only for
an explicit cancellation rather than starting another plan. Connection setup
retains its separate deadline.

The reviewed base is the live `refs/heads/<base-branch>` commit, not the PR
API's potentially historical `base.sha`. Preparation and finalisation read
the branch ref; remote planning, signing and publication reject any subsequent
base/source movement. Retargeting either PR also invalidates the endorsement.

## Assign and request human attestation

Save the complete successful planning result in this session's files area:

```sh
/absolute/path/to/skills/codespace-tramp/setup/copilot-cs-endorse \
  await-attestation --plan "<session-files>/endorsement-plan.json"
```

This rechecks required CI and the exact head/base, adds only the planned
operator's assignment, preserves other assignees and confirms the result.
It is safe to repeat for an unchanged plan. A failed or stale plan never
reaches the mutation. Do not present the attestation prompt before success
or switch the local authenticated account.

Print the full draft URL on its own line immediately before the interactive
prompt. Start its message with the same URL and a blank line, followed by:
**"Please review this implementation. Do you believe it is correct and stand
by every commit in this exact revision, or is further work needed?"**
Include CCR's outcome, full review URL and any unresolved findings, especially
when human review is recommended. Explain why CCR was skipped if unavailable.
Show head/base OIDs, commit count/range, key fingerprint, plan ID, any explicit
committer override and material validation limitations below the question.

Offer further work, leaving the draft unendorsed and the permitted publication
choices from the skill. Preselect no endorsement. Explain that signing changes
OIDs and required CI must run on the signed head; successful publication and
passing signed-head CI will mark the result ready for review.

Never infer approval from CCR, creating the draft, a previous endorsement or
a generic request to finish. Declining, cancelling, requesting further work
or a signing failure does not remove the assignment. Further work invalidates
the plan and returns to the quality gates.

## Sign and verify

Only an affirmative, revision-specific publication choice authorises signing:

```elisp
;; APPROVAL is PLAN-ID:replacement or PLAN-ID:replace, exactly as chosen.
(copilot-cs-endorse "sign" "<PLAN-ID>" "<APPROVAL>" 10)
(copilot-cs-poll "<signing-job-id>")
(copilot-cs-endorsement-result "<signing-job-id>")
```

Only `sign` forwards the agent. Obtain consent for that exposure and the
possible Touch ID request per commit; `IdentitiesOnly` does not filter keys
available to the remote forwarded agent. Retain Stormbreaker's protection
and do not use **Leave Unlocked** when fresh approval is wanted. Login profiles
may replace `SSH_AUTH_SOCK`; the helper restores the forwarded socket only
for the signer.

The helper reconstructs raw commits rather than using `rebase --exec`. It
preserves trees, messages, authors, timestamps, empty commits and merge parent
order. Committer name/email are preserved unless the approved plan explicitly
supplies them. It replaces introduced commits' signatures with the designated
endorsement; base history is untouched. `ssh-keygen -Y verify` checks the exact
key. Reconstruction does not run commit hooks: validate beforehand, and keep
publication's push hooks enabled. No signed branch exists until every commit
verifies.

The `<source>-signed` branch is created inside the Codespace without changing
the source ref or working tree. Existing branches are never overwritten.
Create-only records live beneath
`$(git rev-parse --git-common-dir)/copilot-endorsements/`, keyed by plan ID.
A nonblocking per-plan lock prevents concurrent signing/publication. Signatures
and small read/metadata commands have a 120-second timeout; pushes retain the
runner's normal lifetime for long hooks. Failures do not authorise blind retries.

Keep the signing connection alive: a detached process cannot retain its
forwarded socket after SSH disconnects. Recover the original log, then inspect
a complete receipt without forwarding or signing again:

```elisp
(copilot-cs-endorse "verify" "<PLAN-ID>")
```

Reuse a complete verified receipt. Partial attempts may require new signatures
only after asking the operator; polling or reattachment never replays signing.

## Unassign, publish and finalise

Only after the exact signing or recovery-verification job succeeds, save its
complete receipt and clear the attestation wait before publication:

```sh
/absolute/path/to/skills/codespace-tramp/setup/copilot-cs-endorse \
  complete-attestation --receipt "<session-files>/signed-receipt.json" \
  --approve-plan "<APPROVAL>"
```

The local helper checks the receipt's plan, complete mapping, approval,
operator and revision, then removes only that operator's assignment. A plan,
prompt answer or partial receipt is insufficient. It neither signs nor pushes
nor waits for signed-head CI. If the update fails or is dropped, inspect the
PR and repeat only this idempotent update, not signing. Other assignees remain.

Publish in the Codespace with the same explicit choice:

```elisp
(copilot-cs-endorse "push" "<PLAN-ID>" "<APPROVAL>" 10)
;; Alternatively, when the repository's push hook requires a terminal:
(copilot-cs-endorse "push" "<PLAN-ID>" "<APPROVAL>" 10 t)
```

Choose one call, not both. The PTY reaches the Git hook; inspect its output
and stop if checks were skipped. `replace` uses the exact source-head lease.
`replacement` uses an empty-expectation destination lease, a create-only guard
that cannot overwrite even a concurrent fast-forwardable branch. Neither mode
uses unconditional force. Changed refs or collisions require an operator
decision; do not refresh the lease expectation automatically.

If the operator explicitly chooses the other permitted method after a failed
push of the same unchanged plan, reuse signatures with `push` and the new
approval token, not `sign`. Do not switch after either method has published.

Retrieve the successful push's full receipt with `copilot-cs-endorsement-result`,
or discover the absolute common directory and copy the exact persisted receipt:

```elisp
(copilot-cs-sh "git rev-parse --path-format=absolute --git-common-dir")
```

```sh
/absolute/path/to/skills/codespace-tramp/setup/copilot-ghcs cp "$CS_ID" \
  "remote:<absolute-common-dir>/copilot-endorsements/<PLAN-ID>.<publication>.published.json" \
  "<session-files>/endorsement-receipt.json"
/absolute/path/to/skills/codespace-tramp/setup/copilot-cs-endorse finish \
  --receipt "<session-files>/endorsement-receipt.json" \
  --approve-plan "<APPROVAL>"
```

Require a successful copy before using the file. Finalisation independently
checks published content, mapped parents and each signature against the
approved key; GitHub must also verify the signatures. A **Verified** badge
or a `-signed` name alone is insufficient.

In-place publication retains the PR. Replacement publication creates/reuses
only the draft marked for that exact plan, preserves its title/body, labels,
other assignees, milestone and requested reviewers/teams, then links and closes
the original after checking both drafts. Review discussions/check runs do not
migrate. Reuse the same receipt after interrupted metadata work; never create
an unmarked duplicate or manually close the original to hide a failure.

`finish` also recovers outstanding operator unassignment before copying
metadata or checking readiness. Legacy receipts without an attestor preserve
their assignees; never guess whom to remove. New requests need a fresh `prepare`.

## Signed-head CI and readiness

Signing changes OIDs: unsigned-head CI cannot satisfy the final gate.
For either publication method, `finish` reads all check pages and required flags,
classic protection and effective ruleset requirements. Missing configured checks,
no reported required CI, pending/failing/cancelled results and unreadable policy
block readiness. Completed check runs may be `SUCCESS`, `NEUTRAL` or `SKIPPED`;
required commit statuses must succeed. Optional failures do not block readiness.

Required workflows must have a successful latest applicable run/attempt for
that exact PR, head, branch and base. GraphQL provenance must identify the
configured source repository/file at an immutable revision, matching explicit
SHA pins. Mutable refs use the successful run's recorded revision; later source
ref movement does not invalidate it. Same-named ordinary workflows, other PRs,
superseded successes, partial pagination and unreadable provenance cannot pass.
These rules also apply before endorsement; there is no manual-success override.

Only after verification, passing CI and attestor unassignment does `finish`
mark the signed PR ready and confirm its head/base, assignment and ready state.
Its success JSON contains `ready_for_review: true`. GitHub metadata/readiness
mutations have no expected-head lease; surrounding checks detect races but
cannot make them atomic. Coordinate with other branch writers and stop on
changed state.

Pending readiness is an explicit nonzero result, not a failed publication.
The signed draft and receipt remain usable; a verified replacement's original
may already be closed. Inspect the resulting PR, not its superseded original:

```sh
gh pr checks "$SIGNED_PR_URL" -R "$NWO" --required
```

Poll read-only with a bounded wait, then rerun the same local `finish` after
checks pass. Never re-sign, re-push, demote an unchanged already-ready PR, create
a duplicate or bypass readiness with `gh pr ready`. The local source ref
intentionally remains unsigned after an in-place publication; do not reset it.

Same-repository github.com drafts and complete history are currently supported.
Fork PRs, grafts/replacement objects and signed merge-tag headers require
explicit manual handling, not a lossy rewrite. Ordinary merges and empty commits
are supported. If policy rejects unsigned drafts, ask the operator; do not sign
early to evade it.

## Workflow-selection diagnostics

If a successful required workflow is reported missing, preserve the error's
`workflow selection` JSON in session files before retrying. It records the
expected revision, returned-run count, matching-path IDs, rejection predicates
and observed sources from the failing call, not a later API read. Compare those
IDs and exact PR/source associations with local `gh`; do not override the gate
or treat a subsequent success as a root-cause repair.
