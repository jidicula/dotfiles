# Copilot Code Review commands

Use this reference with this skill's [optional CCR gate](../SKILL.md#gate-2---optional-copilot-code-review).
Run GitHub operations locally using the operator's existing `gh` authentication.
`OWNER`, `REPO`, `NWO`, `PR_NUMBER` and `PR_URL` must all identify the supplied
PR; `SESSION_FILES` is this session's artifact directory. Do not run these
commands against an unrelated PR to test a mutation.

## Availability on the target PR

GitHub CLI detects CCR eligibility through a PR's suggested reviewer actors;
there is no equivalent repository-level reviewer-eligibility API. In particular,
`repository.suggestedActors` concerns assignable/author actors, not reviewers.

Read all pages and retain the full response, including pagination metadata:

```sh
gh api graphql --paginate --slurp \
  -F owner="$OWNER" -F name="$REPO" -F number="$PR_NUMBER" \
  -f query='
    query CopilotReviewAvailability(
      $owner: String!, $name: String!, $number: Int!, $endCursor: String
    ) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          id url state isDraft headRefOid headRefName baseRefName
          suggestedReviewerActors(first: 100, after: $endCursor) {
            nodes { reviewer { __typename ... on Bot { id login } } }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }' > "$SESSION_FILES/ccr-availability.json"
```

Require a successful command, no GraphQL `errors`, non-null repository/PR,
consistent expected head/branches across pages, and a final `hasNextPage: false`.
Find a `Bot` with login `copilot-pull-request-reviewer` and retain its node ID.
REST calls use `copilot-pull-request-reviewer[bot]`; the display name "Copilot"
is not an identity check.

Read pending reviewers before deciding the gate is unavailable or sending a
request:

```sh
gh api "repos/$NWO/pulls/$PR_NUMBER/requested_reviewers?per_page=100" \
  --paginate --slurp > "$SESSION_FILES/ccr-requested-reviewers.json"
```

Validate every page's `users` and `teams` lists; match the reviewer's node ID
or its exact Bot login/type. An existing request or an identified in-progress
CCR run is not a reason to submit another one. If it contradicts missing
eligibility, inspect the pending operation rather than skipping the gate.
Never treat a failed, truncated or unreadable lookup as an empty list.

## Request and re-request

After Gate 1 passes, and only when no request/run is already pending:

```sh
gh pr edit "$PR_URL" -R "$NWO" --add-reviewer '@copilot'
```

The same command re-requests CCR after fixes. Keep the PR in draft. Record its
head, the last completed review ID and the request outcome in session state.
Confirm a pending request or a newer completed Copilot review through readback;
an immediate completion need not remain in the requested-reviewer list.

A timeout or uncertain mutation outcome does not authorise automatic replay.
Inspect pending requests, newer reviews and the PR timeline first. Do not
remove/re-add Copilot to force a duplicate or change automatic-review settings.

## Read the assessment and discussions

Fetch all review and comment pages, not only a CLI summary or the first review:

```sh
gh api "repos/$NWO/pulls/$PR_NUMBER/reviews?per_page=100" \
  --paginate --slurp > "$SESSION_FILES/ccr-reviews.json"
gh api "repos/$NWO/pulls/$PR_NUMBER/comments?per_page=100" \
  --paginate --slurp > "$SESSION_FILES/ccr-comments.json"
gh api graphql --paginate --slurp \
  -F owner="$OWNER" -F name="$REPO" -F number="$PR_NUMBER" \
  -f query='
    query CopilotReviewThreads(
      $owner: String!, $name: String!, $number: Int!, $endCursor: String
    ) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          id headRefOid
          reviewThreads(first: 100, after: $endCursor) {
            nodes {
              id isResolved isOutdated path line
              comments(first: 1) {
                nodes { databaseId url author { __typename login } }
              }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }' > "$SESSION_FILES/ccr-threads.json"
```

These independent reads may run in parallel. Validate their shapes and
pagination, then recheck the PR head before acting. The thread query fetches
only each thread's origin for attribution; the fully paginated REST comments
provide its complete discussion, including human replies via `in_reply_to_id`.
Do not mistake `comments(first: 1)` for the whole conversation.

Match review author Bot identity, `commit_id`, review ID and completion time.
Exclude pending/dismissed reviews and stale commits. After a re-request, require
a newer review, even when the head did not change. A later pending request
prevents reusing an earlier completed assessment as the new result.

Read the overview's explicit approval assessment, such as "Approval recommended"
or "Human review recommended", together with its explanation and findings.
Copilot can publish that assessment in a `COMMENTED` review; formal `APPROVED`
reviews depend on a separate setting. Do not enable that setting or require it
for this skill. Neither a generic comment state nor zero comments establishes
an approval recommendation. Missing or contradictory assessments need operator
attention, not a guessed success.

## Address and resolve findings

Make and validate fixes in the approved Codespace, then commit and push them
unsigned. Inspect each Copilot-origin thread and its human replies. Resolve it
only after the underlying finding is addressed; outdated line positions or
an approving overview do not prove that.

A concise reply can link the verified fix where useful:

```sh
gh api --method POST \
  "repos/$NWO/pulls/$PR_NUMBER/comments/$COMMENT_ID/replies" \
  -f body="Addressed in $FIX_COMMIT."
```

Use the origin comment's actual ID. Follow the repository's writing conventions,
use full GitHub issue/PR URLs and scan outgoing text for bare numeric references.
Do not bulk-resolve human comments, disputes or findings needing a design choice.
Copilot does not read or reply to these conversation replies; the code change
and a new review request are what allow it to reassess the implementation.

For the exact inspected Copilot thread:

```sh
gh api graphql -f id="$THREAD_ID" -f query='
  mutation ResolveCopilotReviewThread($id: ID!) {
    resolveReviewThread(input: {threadId: $id}) {
      thread { id isResolved }
    }
  }'
```

Require no errors, the same thread ID and `isResolved: true`, then confirm its
state on the intended PR. On an uncertain response, inspect that thread before
retrying. After fixes change the head, rerun required CI and request/reuse the
new head's CCR run as directed by the skill; old CI/review evidence is stale.

## Authoritative behaviour

- [Using Copilot code review](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/request-a-code-review/use-code-review) documents REST review requests, re-reviews, approval assessments and the fact that Copilot does not read conversation replies.
- [About Copilot code review](https://docs.github.com/en/copilot/concepts/agents/code-review) documents availability, policy and usage limits. Do not change those settings to make an optional gate available.
- [GitHub CLI reviewer discovery](https://github.com/cli/cli/blob/fc4b137cdef0a6bd28fd461b7cf9c84a5812a8cd/api/queries_pr_review.go) uses the PR's `suggestedReviewerActors` connection to detect Copilot eligibility.
