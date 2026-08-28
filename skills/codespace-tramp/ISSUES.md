# Codespace TRAMP issues

This file records unexpected behaviour encountered while using the
`codespace-tramp` skill so it can be reproduced and addressed later. Initial
reports are observation logs, not diagnoses.

## Recording rules

1. Add an entry as soon as a problem is observed, even if a workaround exists
   or the problem is fixed during the same session.
2. Assign the next sequential id in the form `CT-NNNN`, starting with
   `CT-0001`. Never reuse an id.
3. Append the entry under **Open issues** and remove the *No issues recorded*
   placeholder when adding the first one. Preserve all earlier entries.
4. Complete every metadata field. Use `Unknown` or `N/A` when information is
   unavailable rather than deleting a row.
5. Describe only observable symptoms. Include expected versus actual behaviour
   and enough basic reproduction steps for another person or agent to encounter
   the same symptom. Do not include suspected causes, diagnoses, proposed fixes,
   blame, or investigative reasoning.
6. Redact credentials and sensitive data from commands and output excerpts.
7. Leave **Resolution** as `Pending follow-up` when filing. A later follow-up
   may update the status and resolution without rewriting the original symptom
   report.
8. Immediately notify the human operator after adding an entry. Include its id,
   title, this file's path, and a one-sentence symptom summary; do not defer the
   notification to the final task summary.

## Open issues

_No issues recorded._

## Entry template

Copy this block under **Open issues**, replacing every placeholder. Keep the
headings and metadata labels unchanged so entries remain easy for both humans
and agents to scan and parse.

```markdown
### CT-NNNN — <short, symptom-oriented title>

| Field | Value |
|---|---|
| Status | Open |
| Date | YYYY-MM-DD |
| Copilot session | <session name and/or id, or Unknown> |
| Repository | <OWNER/REPO, or N/A> |
| Codespace | <immutable Codespace name, or N/A> |
| Branch | <branch, or Unknown> |

#### Symptoms

- **Expected:** <what should have happened>
- **Observed:** <what happened instead>
- **Error/output:** <short redacted excerpt, or None>
- **Impact:** <what could not proceed or behaved incorrectly>

#### Reproduction

1. **Starting state:** <minimum required setup or state>
2. **Action:** <specific action or command that triggers the symptom>
3. **Observed result:** <result that demonstrates the problem>

#### Resolution

Pending follow-up.
```
