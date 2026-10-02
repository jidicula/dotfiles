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
unreadable or unsupported policy. It requires an open draft with the same head
and base branch, permits verified clean base advances and performs no PR mutation.
Only after CI passes does it
read the signing key, record the authenticated operator as `attestor`, prepare
publication choices, and emit the request. Compare its head and base branch with
the completed quality-gate evidence. Head changes or retargeting return to those
gates; a clean base advance does not discard the existing review.

Pending, failing, missing or absent required checks produce a nonzero result
and no request. Do not start a remote plan, ask for an endorsement, or forward
the signing agent while either pre-endorsement gate is blocked. A cached request
is not a substitute for checking the current revision.

Required workflows are discovered from the exact commit's fully paginated
check suites, not the repository-wide run search. The gate still verifies the
enforced workflow source, exact PR and branch, and latest run and attempt.
Missing, partial, changing or malformed discovery results block the gate;
an older successful run cannot substitute for a newer failing one.

Preserve previously endorsed commits byte-for-byte. New version 2 plans verify
existing SSH signatures against the selected endorsement key and retain commits
whose introduced parents are also retained. Other signers do not count as the
operator's endorsement; malformed or invalid matching-key signatures block planning.
A changed parent requires reconstruction and a fresh signature on its descendants.
The immutable `preserved_commits` list records what will not be re-signed; the
remaining entries in `commits` are the exact signing scope. If none remain, do not
request another endorsement: resume the prior receipt instead.

Version 1 plans and receipts retain their original full-range signing semantics.
Never edit an old plan, convert its version or reuse its approval for a new scope.
Older helpers reject version 2 rather than silently rewriting preserved commits.

Preserve committer identity on new commits by default. If an override is needed,
pass `--committer-name "<name>" --committer-email "<email>"` to `prepare` as a
proposal, without a separate permission prompt. Both values are required and
become part of the immutable plan. Show the existing and proposed identities
in the endorsement request; its affirmative approval authorises the metadata
change together with signing and the selected publication method. Preparing
or planning the proposal changes no commits and grants no approval.
Preparation checks the selected key against the authenticated operator's
registered signing keys; the supplied email must belong to that account.
The override applies only to newly signed commits; preserved commits keep their
existing identities and signatures. Authors and all timestamps remain unchanged.
Never change attribution or
Git/Secretive settings automatically to obtain GitHub's verification badge.

`gh api user/emails` is an optional identity diagnostic, not a required
preparation step. A scope-related 403/404 does not establish whether an address
is verified. If that lookup is unavailable, include confirmation of the proposed
verified account email in the same endorsement request: state that approval
also confirms the address is verified for the authenticated account. Do not
ask an earlier identity-confirmation question. If the operator cannot confirm
it, leave the draft unendorsed. Do not refresh authentication scopes, switch
accounts or use a public profile email as verification evidence. Finalisation
still requires GitHub to verify the actual published signatures.

Before planning, check `git rev-parse --is-shallow-repository` in the selected
Codespace. A shallow checkout cannot establish the full reviewed ancestry.
If it returns `true`, verify that `origin` matches the prepared repository and
check free disk space, then restore history through the login-aware runner.
Replace `BASE_OID` and `HEAD_OID` below with the exact prepared request fields:

```elisp
(copilot-cs-login-sh "git fetch --progress --unshallow --no-tags --no-write-fetch-head --no-recurse-submodules --refmap= origin BASE_OID HEAD_OID" 10)
```

This fetch can be large; poll its existing job without a total-duration cap.
Verify that the checkout is no longer shallow and that the source head, refs,
index, `FETCH_HEAD` and working tree are unchanged before retrying the original
request. Do not delete Git's shallow-boundary file, fetch every branch, rebase,
or relax the complete-history guard to make planning pass.

Pass the successful request as an escaped string, not an Elisp expression or
shell command, to the already selected Codespace:

```elisp
(copilot-cs-endorse "plan" "<JSON returned by prepare>")
(copilot-cs-poll "<planning-job-id>")
(copilot-cs-endorsement-result "<planning-job-id>")
```

The runner compresses and base64-encodes the helper before sending it through
the login/PTY wrappers, keeping the SSH command below Linux's single-argument
limit. Python's standard-library `zlib` is required locally and in the Codespace;
a compression or decoding failure stops the action rather than running a partial helper.

Use the complete result, not a status report's truncated tail. Do not manually
transcribe its JSON; transfer the persisted record as described below. Planning requires
the local source branch to match the draft head and tracked changes to be
clean. It snapshots the remote head/base and records every introduced commit
in topological order. `publication_options` lists what may be offered; it is
not a publication selection or an approval.

If the prepared base commit is missing, planning fetches that exact OID and
its history from the verified origin. It does not update local or
remote-tracking refs, tags, `FETCH_HEAD`, the index or working tree. Source and
base refs are checked again after the fetch. A changed source stops planning;
a base advance is checked for ancestry and merge conflicts. Fetch or mergeability
failures stop planning without changing the requested OIDs.

The history fetch streams progress into its planning job and, like a push,
has no total-duration cap. A healthy large transfer must not be killed by the
120-second metadata deadline. Poll the same job; use `copilot-cs-stop` only for
an explicit cancellation rather than starting another plan. Connection setup
retains its separate deadline.

The recorded base is the live `refs/heads/<base-branch>` commit at preparation,
not the PR API's potentially historical `base.sha`. It permanently defines the
reviewed commit range; never change that field, the plan ID or the approval token
just because the base advances.

A fast-forward of the same base branch is allowed when it remains mergeable.
The Codespace fetches missing base objects by exact OID, checks ancestry, and uses
`git merge-tree --write-tree` without changing the index, checkout or refs. GitHub
control-plane checks verify fast-forward ancestry and the current PR's explicit
mergeability for the expected head and base branch. Publication checks the signed
head; finalisation uses the open replacement when the original is already closed.

Conflicts block progress until addressed. Unknown mergeability, a failed probe
or a continuously moving base pauses the same plan; recheck it without requesting
fresh approval or replaying signatures/publication. A changed source head,
retargeted base, rewind or rewritten base history still requires a new plan and
human decision. Do not merge or rebase the source merely to track a clean advance.
Current required CI, including signed-head CI before readiness, remains mandatory.

## Assign and request human attestation

Transfer the successful plan's persisted record into this session's files area;
never reconstruct its request, key or commit list from displayed text. Set
`SESSION_FILES` to that existing directory and use the selected immutable
`CS_ID`. First discover the absolute Git common directory in its worktree:

```elisp
(copilot-cs-sh "git rev-parse --path-format=absolute --git-common-dir")
```

Set `COMMON_DIR` to the printed path and `PLAN_ID` to `plan_id` from the complete
planning result, not the runner job ID. The persisted record omits the result
envelope's `plan_id`, so add only that field programmatically after the copy
succeeds:

```sh
/absolute/path/to/skills/codespace-tramp/setup/copilot-ghcs cp "$CS_ID" \
  "remote:$COMMON_DIR/copilot-endorsements/$PLAN_ID.plan.json" \
  "$SESSION_FILES/endorsement-plan-record.json" &&
python3 -c '
import json, sys
print(json.dumps(dict(plan_id=sys.argv[1], **json.load(sys.stdin))))
' "$PLAN_ID" < "$SESSION_FILES/endorsement-plan-record.json" \
  > "$SESSION_FILES/endorsement-plan.json"
```

Only after both commands succeed, submit that generated file:

```sh
/absolute/path/to/skills/codespace-tramp/setup/copilot-cs-endorse \
  await-attestation --plan "$SESSION_FILES/endorsement-plan.json"
```

This rechecks required CI, the exact head and base compatibility, adds only the planned
operator's assignment, preserves other assignees and confirms the result.
It is safe to repeat for an unchanged plan. A failed or stale plan never
reaches the mutation. Do not present the attestation prompt before success
or switch the local authenticated account.

If digest verification fails, preserve the error and recover the exact original
record and plan identifier. Do not shorten fields, compute a replacement plan
ID, bypass verification or replay planning merely to repair a local copy.

Print the full draft URL on its own line immediately before the interactive
prompt. Start its message with the same URL and a blank line, followed by:
**"Please review this implementation. Do you believe it is correct and stand
by every commit in this exact revision, or is further work needed?"**
Include CCR's outcome, full review URL and any unresolved findings, especially
when human review is recommended. Explain why CCR was skipped if unavailable.
Show head/base OIDs, commit count/range, key fingerprint, plan ID, any proposed
committer change and material validation limitations below the question.
Distinguish the complete reviewed range from the commits requiring signatures,
and explicitly list the previously endorsed commits that retain their IDs.
For a committer proposal, show the existing identities and proposed name/email,
confirm that authors and timestamps stay unchanged, and state that endorsement
also approves this change. Include any verified-email confirmation here.

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
supplies them. It replaces only the planned new commits' signatures with the
designated endorsement; previously endorsed commits and base history are untouched.
The receipt includes identity mappings for preserved commits. `ssh-keygen -Y verify` checks the exact
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
uses unconditional force. Changed source/destination refs or collisions require
an operator decision; do not refresh the lease expectation automatically. A
verified clean base advance leaves that exact-head lease and approval unchanged.

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
that exact PR, head, source branch and base branch. GraphQL provenance must identify the
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
