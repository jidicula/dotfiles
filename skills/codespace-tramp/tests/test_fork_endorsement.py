"""Fork provenance and narrowly scoped collaborator-approval ordering."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import shutil
import sys
from unittest import mock

import test_endorsement as fixtures
from test_endorsement import (
    EndorsementTestCase, GitHubEndorsementTestCase,
)


APPROVAL_CHECK = {
    "kind": "pending_collaborator_approvals", "check_name": "Collaborator Review",
    "app_id": 1001, "app_slug": "collaborator-review", "required_approvals": 3,
}


class ForkRepositoryTestCase(EndorsementTestCase):
    def setUp(self):
        super().setUp()
        self.request.update(version=5, source_repository="contributor/project")
        self.upstream = self.root / "upstream.git"
        self.git("init", "--quiet", "--bare", "--template=", str(self.upstream))
        self.git("push", "--quiet", str(self.upstream), "main")
        self.git("remote", "set-url", "origin", "https://github.com/contributor/project.git")
        self.origin_patch.stop()
        real_git = shutil.which("git")
        self.executable("git", f"""
import os, sys
arguments = sys.argv[1:]
if arguments and arguments[0] in ("fetch", "push", "ls-remote") and "--get-url" not in arguments:
    source = {str(self.remote)!r}
    upstream = {str(self.upstream)!r}
    remotes = {{
        "https://github.com/example/project.git": upstream,
        "https://github.com/contributor/project.git": source,
    }}
    for item in arguments:
        if "github.com" in item and item not in remotes:
            raise SystemExit("unexpected fixture transport destination")
    arguments = ["-c", "url." + source + ".insteadOf=https://github.com/contributor/project.git",
                 "-c", "url." + upstream + ".insteadOf=https://github.com/example/project.git",
                 *arguments]
os.execv({real_git!r}, ["git", *arguments])
""")

    def advance_base(self, **options):
        source = self.remote
        self.remote = self.upstream
        try:
            return super().advance_base(**options)
        finally:
            self.remote = source

    def follow_up(self, previous, count=3):
        added = super().follow_up(previous, count)
        self.request["version"] = 5
        return added

    def upstream_refs(self):
        return self.git("--git-dir=" + str(self.upstream), "show-ref")


class ForkPublicationTests(ForkRepositoryTestCase, fixtures.PublicationTests):
    """Run the existing real-Git publication regressions against a source fork."""

    def test_publication_never_changes_upstream_or_resets_the_local_source(self):
        upstream = self.upstream_refs()
        receipt = self.sign("replace")
        self.repository.push(self.plan_id, self.approve("replace"))
        self.assertEqual(self.upstream_refs(), upstream)
        self.assertEqual(self.remote_head("feature"), receipt["head"])
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.head)

    def test_fork_origin_fetch_and_push_must_match_even_when_upstream_is_writable(self):
        self.repository.validate_origin(self.request)
        for option in ("url", "pushurl"):
            with self.subTest(option=option):
                self.git("config", "remote.origin." + option, "https://github.com/example/project.git")
                with self.assertRaisesRegex(ValueError, "origin fetch/push URL"):
                    self.freeze()
                self.git("config", "remote.origin." + option,
                         "https://github.com/contributor/project.git")
        self.assertFalse(list(self.repository.state.glob("*.plan.json")))

    def test_upstream_url_rewrites_cannot_silently_select_another_repository(self):
        self.git("config", "url.https://github.com/other/project.git.insteadOf",
                 "https://github.com/example/project.git")
        with self.assertRaisesRegex(ValueError, "upstream fetch URL"):
            self.freeze()
        self.assertFalse(list(self.repository.state.glob("*.plan.json")))

    def test_replacement_source_movement_during_push_is_not_reported_as_complete(self):
        self.sign()
        git = self.repository.git

        def move_source(*arguments, **options):
            result = git(*arguments, **options)
            if arguments[0] == "push":
                self.git("--git-dir=" + str(self.remote), "update-ref",
                         "refs/heads/feature", self.first)
            return result

        with mock.patch.object(self.repository, "git", side_effect=move_source):
            with self.assertRaisesRegex(ValueError, "source branch changed"):
                self.repository.push(self.plan_id, self.approve())
        self.assertFalse(list(self.repository.state.glob("*.published.json")))
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.head)


class ForkPlanningTests(ForkRepositoryTestCase):
    def test_missing_base_fetches_from_upstream_without_touching_refs_or_worktree(self):
        self.request["base"] = self.advance_base()
        refs = self.git("show-ref")
        status = self.git("status", "--porcelain")
        fetch_head = self.root / ".git" / "FETCH_HEAD"
        fetch_head.write_text("retained fetch receipt\n")
        with mock.patch.object(self.repository, "git", wraps=self.repository.git) as commands:
            self.freeze()
        fetches = [call.args for call in commands.call_args_list if call.args[0] == "fetch"]
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0][-2:], ("https://github.com/example/project.git", self.request["base"]))
        self.assertEqual(self.git("show-ref"), refs)
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.assertEqual(fetch_head.read_text(), "retained fetch receipt\n")
        self.assertEqual(self.remote_head("main"), self.base)
        self.assertEqual(self.plan["commits"], [self.first, self.head])

    def test_divergent_fork_base_is_irrelevant_to_the_upstream_reviewed_range(self):
        self.git("--git-dir=" + str(self.remote), "update-ref", "refs/heads/main", self.head)
        self.request["base"] = self.advance_base()
        self.freeze()
        self.assertEqual(self.plan["commits"], [self.first, self.head])
        self.assertEqual(self.remote_head("main"), self.head)

    def test_missing_upstream_base_cannot_fall_back_to_the_fork_base(self):
        self.git("--git-dir=" + str(self.upstream), "update-ref", "-d", "refs/heads/main")
        with self.assertRaisesRegex(ValueError, "upstream base branch"):
            self.freeze()

    def test_recorded_upstream_base_must_also_be_mergeable(self):
        self.request["base"] = self.advance_base(conflict=True)
        with self.assertRaisesRegex(ValueError, "merge conflicts"):
            self.freeze()
        self.assertFalse(list(self.repository.state.glob("*.plan.json")))

    def test_fork_base_cannot_mask_upstream_conflicts_or_rewinds(self):
        self.advance_base(conflict=True)
        with self.assertRaisesRegex(ValueError, "merge conflicts"):
            self.freeze()
        self.git("--git-dir=" + str(self.upstream), "update-ref", "refs/heads/main", self.base)
        self.request["base"] = self.advance_base()
        self.git("--git-dir=" + str(self.upstream), "update-ref", "refs/heads/main", self.base)
        with self.assertRaisesRegex(ValueError, "without a fast-forward"):
            self.freeze()

    def test_upstream_advance_and_source_movement_during_fetch_are_checked(self):
        self.request["base"] = self.advance_base()
        git = self.repository.git

        def move_source(*arguments, **options):
            result = git(*arguments, **options)
            if arguments[0] == "fetch":
                self.git("--git-dir=" + str(self.remote), "update-ref",
                         "refs/heads/feature", self.first)
            return result

        with mock.patch.object(self.repository, "git", side_effect=move_source):
            with self.assertRaisesRegex(ValueError, "source branch changed"):
                self.freeze()
        self.assertFalse(list(self.repository.state.glob("*.plan.json")))

    def test_same_named_source_and_upstream_base_keep_distinct_leases(self):
        self.git("update-ref", "refs/heads/main", self.head)
        self.git("--git-dir=" + str(self.remote), "update-ref", "refs/heads/main", self.head)
        self.request.update(source_branch="main", target_branch="main-signed")
        upstream = self.upstream_refs()
        receipt = self.sign("replace")
        self.repository.push(self.plan_id, self.approve("replace"))
        self.assertEqual(self.upstream_refs(), upstream)
        self.assertEqual(self.remote_head("main"), receipt["head"])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.head)
        self.assertEqual(self.repository.sign(self.plan_id, self.approve("replace")), receipt)

    def test_v5_preserves_existing_signatures_metadata_and_exact_approval_scope(self):
        previous = self.sign()
        originals = {oid: self.repository.git("cat-file", "commit", oid)
                     for oid in previous["mapping"].values()}
        added = self.follow_up(previous, count=1)
        self.request["committer"] = {"name": "Fixture Endorser", "email": "endorser@example.invalid"}
        receipt = self.sign("replace")
        self.assertEqual(receipt["plan"]["preserved_commits"], list(previous["mapping"].values()))
        for oid, raw in originals.items():
            self.assertEqual(receipt["mapping"][oid], oid)
            self.assertEqual(self.repository.git("cat-file", "commit", oid), raw)
        before, after = self.commit_data(added[0]), self.commit_data(receipt["mapping"][added[0]])
        self.assertEqual(before["author"], after["author"])
        self.assertEqual(before["committer"]["date"], after["committer"]["date"])
        self.assertEqual(after["committer"]["name"], "Fixture Endorser")
        with self.assertRaisesRegex(ValueError, "AND publication"):
            self.repository.sign(self.plan_id, previous["plan_id"] + ":replace")

    def test_fork_verified_base_merge_preserves_platform_and_operator_signatures(self):
        previous = self.sign()
        self.follow_up(previous, count=0)
        base = self.advance_base()
        self.git("fetch", "--quiet", "https://github.com/example/project.git", "main")
        key = self.root / "platform-test-only"
        self.command("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key))
        key.chmod(0o600)
        self.command("ssh-add", str(key))
        self.git("-c", "user.name=GitHub", "-c", "user.email=noreply@github.com",
                 "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}",
                 "merge", "--quiet", "--no-ff", "-S", "-m", "Merge upstream main", base)
        merge = self.git("rev-parse", "HEAD").strip()
        retained = self.repository.git("cat-file", "commit", merge)
        self.git("commit", "--quiet", "--allow-empty", "-m", "new work")
        self.request.update(base=base, head=self.git("rev-parse", "HEAD").strip(),
                            preserve_base_merges=[merge])
        self.git("push", "--quiet", "origin", "followup")
        receipt = self.sign()
        self.assertEqual(receipt["plan"]["preserved_base_merges"], [merge])
        self.assertEqual(receipt["mapping"][merge], merge)
        self.assertEqual(self.repository.git("cat-file", "commit", merge), retained)
        self.assertEqual(self.repository.verify(self.plan_id), receipt)


class ForkGitHubTests(GitHubEndorsementTestCase, ForkRepositoryTestCase):
    def test_preparation_uses_upstream_policy_and_source_rewrite_permission(self):
        with mock.patch.object(self.helper, "replacement_allowed", return_value=False) as permission:
            prepared = self.helper.prepare("example/project", 12, str(self.key_file) + ".pub")
        permission.assert_called_once_with("contributor/project", "feature")
        self.assertEqual(prepared["version"], 5)
        self.assertEqual(prepared["source_repository"], "contributor/project")
        self.assertEqual(prepared["publication_options"], ["replacement"])
        self.assertIn(("GET-PAGES", "repos/example/project/rules/branches/main?per_page=100", None),
                      self.calls)
        self.assert_read_only_preparation()

    def test_unknown_source_branch_protection_does_not_offer_rewriting(self):
        for protected in (None, 0, "", "false"):
            with self.subTest(protected=protected), mock.patch.object(
                    self.helper, "github", side_effect=[[], {"protected": protected}]), redirect_stderr(
                    io.StringIO()):
                self.assertFalse(self.helper.replacement_allowed("contributor/project", "feature"))

    def test_fork_base_conflicts_and_unknown_mergeability_block_even_without_an_advance(self):
        for mergeable in ("CONFLICTING", "UNKNOWN", None):
            with self.subTest(mergeable=mergeable):
                self.mergeable = mergeable
                with self.assertRaisesRegex(ValueError, "mergeability|merge conflicts"):
                    self.prepare()
        self.assert_read_only_preparation()

    def test_unknown_source_policy_never_inherits_upstream_rewrite_permission(self):
        self.failure = ("GET", "repos/contributor/project/rules/branches/feature")
        with redirect_stderr(io.StringIO()):
            prepared = self.helper.prepare("example/project", 12, str(self.key_file) + ".pub")
        self.assertEqual(prepared["publication_options"], ["replacement"])
        self.assert_read_only_preparation()

    def test_full_replacement_assignment_draft_verification_and_resumption(self):
        self.helper.await_attestation(self.plan)
        self.assertIn("fixture", self.helper.assignee_logins(self.original))
        self.helper.complete_attestation(self.receipt, self.approve())
        self.assertNotIn("fixture", self.helper.assignee_logins(self.original))
        self.repository.push(self.plan_id, self.approve())
        result = self.helper.finish(self.receipt, self.approve(), draft_only=True)
        self.assertFalse(result["ready_for_review"])
        self.assertTrue(self.replacement["draft"])
        self.assertEqual(self.original["state"], "closed")
        creation = [data for method, endpoint, data in self.calls
                    if method == "POST" and endpoint == "repos/example/project/pulls"]
        self.assertEqual(creation[0]["head"], "contributor:feature-signed")
        self.assertEqual(creation[0]["head_repo"], "project")
        self.assertTrue(any("head=contributor%3Afeature-signed" in endpoint
                            for _, endpoint, _ in self.calls))
        self.assertFalse(any("/repos/example/project/git/commits/" in endpoint
                             or endpoint.startswith("repos/example/project/git/commits/")
                             for _, endpoint, _ in self.calls))
        result = self.finish()
        self.assertTrue(result["ready_for_review"])
        self.finish()
        self.assertEqual(len([data for method, endpoint, data in self.calls
                              if method == "POST" and endpoint == "repos/example/project/pulls"]), 1)

    def test_in_place_publication_uses_fork_signature_readback_without_replacement(self):
        self.receipt = self.repository.push(self.plan_id, self.approve("replace"))
        self.original["head"]["sha"] = self.receipt["head"]
        self.assertTrue(self.finish()["ready_for_review"])
        self.assertIsNone(self.replacement)
        self.assertEqual(self.original["state"], "open")
        self.assertIn(("GET", "repos/contributor/project/git/ref/heads/feature", None), self.calls)
        self.assertIn(("GET", "repos/contributor/project/git/commits/" + self.receipt["head"], None),
                      self.calls)

    def test_same_named_source_base_is_supported_by_github_finalisation(self):
        self.git("update-ref", "refs/heads/main", self.head)
        self.git("--git-dir=" + str(self.remote), "update-ref", "refs/heads/main", self.head)
        self.original["head"]["ref"] = "main"
        self.request = self.prepare()
        self.receipt = self.sign("replace")
        self.commits.update({oid: self.commit_data(oid) for oid in self.receipt["mapping"].values()})
        self.repository.push(self.plan_id, self.approve("replace"))
        self.original["head"]["sha"] = self.receipt["head"]
        self.assertTrue(self.finish()["ready_for_review"])
        self.assertIn(("GET", "repos/example/project/git/ref/heads/main", None), self.calls)
        self.assertIn(("GET", "repos/contributor/project/git/ref/heads/main", None), self.calls)

    def test_fork_workflow_run_requires_source_and_upstream_pr_provenance(self):
        run = self.add_required_workflow()
        run["head_repository"] = {"full_name": "contributor/project"}
        association = run["pull_requests"][0]
        association.update(url="https://api.github.com/repos/example/project/pulls/12")
        association["head"].update(ref="feature",
                                   repo={"url": "https://api.github.com/repos/contributor/project"})
        association["base"]["repo"] = {"url": "https://api.github.com/repos/example/project"}
        self.assertEqual(self.prepare()["version"], 5)
        valid = copy.deepcopy(run)
        for change in (
            lambda item: item.update(head_repository={"full_name": "other/project"}),
            lambda item: item["pull_requests"][0].update(
                url="https://api.github.com/repos/other/project/pulls/12"),
            lambda item: item["pull_requests"][0]["head"].update(
                repo={"url": "https://api.github.com/repos/other/project"}),
            lambda item: item["pull_requests"][0]["base"].update(
                repo={"url": "https://api.github.com/repos/contributor/project"}),
        ):
            with self.subTest(change=change):
                run.clear()
                run.update(copy.deepcopy(valid))
                change(run)
                with self.assertRaisesRegex(ValueError, "missing required workflow"):
                    self.prepare()
        self.assert_read_only_preparation()

    def test_graphql_cannot_substitute_another_repository_for_the_same_head(self):
        api = self.api
        for field, value in (("repository", {"nameWithOwner": "other/project"}),
                             ("headRepository", {"nameWithOwner": "other/project"}),
                             ("headRepository", None)):
            with self.subTest(field=field, value=value):
                def changed(endpoint, method="GET", data=None):
                    result = api(endpoint, method, data)
                    if endpoint == "graphql" and "EndorsementBaseCompatibility" in data["query"]:
                        result["data"]["node"][field] = value
                    return result

                with mock.patch.object(self.helper, "github", side_effect=changed):
                    with self.assertRaisesRegex(ValueError, "repository identity"):
                        self.prepare()
        self.assert_read_only_preparation()

    def test_original_identity_changes_block_finalisation_before_any_mutation(self):
        original = copy.deepcopy(self.original)
        for change in (
            lambda pr: pr["head"].update(repo={"full_name": "other/project"}),
            lambda pr: pr["base"].update(repo={"full_name": "other/project"}),
            lambda pr: pr.update(number=99),
            lambda pr: pr.update(html_url="https://github.com/other/project/pull/12"),
        ):
            with self.subTest(change=change):
                self.original = copy.deepcopy(original)
                change(self.original)
                with self.assertRaisesRegex(ValueError, "repository|identity"):
                    self.finish()
        self.assert_read_only_calls()

    def test_replacement_with_same_oid_but_wrong_fork_or_pr_identity_is_not_adopted(self):
        self.repository.push(self.plan_id, self.approve())
        self.helper.finish(self.receipt, self.approve(), draft_only=True)
        replacement = copy.deepcopy(self.replacement)
        for change in (
            lambda pr: pr["head"].update(repo={"full_name": "other/project"}),
            lambda pr: pr.update(html_url="https://github.com/other/project/pull/13"),
            lambda pr: pr["head"].update(ref="other"),
            lambda pr: pr.update(body="unrelated draft"),
        ):
            with self.subTest(change=change):
                self.replacement = copy.deepcopy(replacement)
                change(self.replacement)
                with self.assertRaises(ValueError):
                    self.finish()
                self.assertTrue(self.replacement["draft"])

    def test_an_ordinary_verified_badge_cannot_substitute_for_the_planned_signature(self):
        self.repository.push(self.plan_id, self.approve())
        signed = self.commits[self.receipt["head"]]
        signed["verification"]["verified"] = True
        signed["verification"]["signature"] = "not the planned signature"
        with self.assertRaises((ValueError, RuntimeError)):
            self.finish()
        self.assertIsNone(self.replacement)
        self.assertEqual(self.original["state"], "open")

    def test_fork_no_required_ci_exception_stays_independent_and_revision_bound(self):
        self.required_contexts = []
        self.check_pages = [[]]
        with redirect_stderr(io.StringIO()):
            prepared = self.prepare(allow_no_required_ci=True)
        self.assertEqual(prepared["version"], 5)
        self.assertEqual(prepared["ci_exception"], "no_required_ci")
        self.assertNotIn("ci_deferral", prepared)
        with self.assertRaisesRegex(ValueError, "independent"):
            self.prepare(allow_no_required_ci=True, defer_collaborator_approvals=APPROVAL_CHECK)


class ApprovalDeferralPlanTests(EndorsementTestCase):
    def test_deferral_is_immutable_and_cannot_be_retrofitted_into_old_protocols(self):
        self.request.update(version=5, ci_deferral=dict(APPROVAL_CHECK, check_run_id=71))
        receipt = self.sign()
        self.assertEqual(receipt["plan"]["request"]["ci_deferral"]["check_run_id"], 71)
        for version in (1, 2, 3, 4):
            with self.subTest(version=version):
                request = dict(self.request, version=version)
                if version == 4:
                    request["ci_exception"] = "no_required_ci"
                with self.assertRaises(ValueError):
                    self.helper.validate_request(request)
        changed = copy.deepcopy(receipt)
        changed["plan"]["request"]["ci_deferral"]["app_id"] += 1
        with self.assertRaisesRegex(ValueError, "digest"):
            self.helper.signed_receipt_request(changed, self.approve())
        with self.assertRaisesRegex(ValueError, "AND publication"):
            self.repository.sign(self.plan_id, self.plan_id + ":other")

    def test_malformed_deferrals_and_combined_exceptions_are_rejected(self):
        valid = dict(APPROVAL_CHECK, check_run_id=71)
        for change in (
            {"app_id": True}, {"check_run_id": 0}, {"required_approvals": False},
            {"kind": "skip_check"}, {"app_slug": ""}, {"check_name": ""},
            {"extra": "permission"}, {"required_approvals": 0},
        ):
            with self.subTest(change=change):
                request = dict(self.request, version=5, ci_deferral={**valid, **change})
                with self.assertRaises(ValueError):
                    self.helper.validate_request(request)
        with self.assertRaises(ValueError):
            self.helper.validate_request(dict(
                self.request, version=5, ci_deferral=valid, ci_exception="no_required_ci"))


class ApprovalDeferralTests(GitHubEndorsementTestCase):
    def setUp(self):
        super().setUp()
        self.required_contexts.append(APPROVAL_CHECK["check_name"])
        self.approval_node = {
            "__typename": "CheckRun", "isRequired": True, "name": APPROVAL_CHECK["check_name"],
            "databaseId": 71, "status": "IN_PROGRESS", "conclusion": None,
            "checkSuite": {"app": {"databaseId": 1001, "slug": "collaborator-review"}},
        }
        self.check_pages.append([self.approval_node])
        self.approval_run = {
            "id": 71, "name": APPROVAL_CHECK["check_name"], "head_sha": self.head,
            "url": "https://api.github.com/repos/example/project/check-runs/71",
            "status": "in_progress", "conclusion": None,
            "app": {"id": 1001, "slug": "collaborator-review"},
            "output": {"title": "Awaiting approvals (0/3)"},
        }

    def api(self, endpoint, method="GET", data=None):
        if "/check-runs/" in endpoint:
            self.calls.append((method, endpoint, data))
            if self.failure == (method, endpoint):
                raise RuntimeError("fixture API failure")
            return copy.deepcopy(self.approval_run)
        return super().api(endpoint, method, data)

    def prepare_deferral(self):
        return self.prepare(defer_collaborator_approvals=copy.deepcopy(APPROVAL_CHECK))

    def signed_deferral(self, publication="replace"):
        self.follow_up(self.receipt, count=1)
        self.original["head"].update(ref=self.request["source_branch"], sha=self.request["head"])
        self.approval_run["head_sha"] = self.request["head"]
        self.request = self.prepare_deferral()
        self.freeze()
        self.helper.await_attestation(self.plan)
        self.receipt = self.repository.sign(self.plan_id, self.approve(publication))
        self.commits.update({oid: self.commit_data(oid) for oid in (
            *self.receipt["mapping"], *self.receipt["mapping"].values())})
        self.helper.complete_attestation(self.receipt, self.approve(publication))
        self.repository.push(self.plan_id, self.approve(publication))
        if publication == "replace":
            self.original["head"]["sha"] = self.receipt["head"]
        self.approval_node["databaseId"] = 72
        self.approval_run.update(id=72, head_sha=self.receipt["head"],
                                 url="https://api.github.com/repos/example/project/check-runs/72")

    def approve_collaborators(self):
        self.approval_node.update(status="COMPLETED", conclusion="SUCCESS")
        self.approval_run.update(status="completed", conclusion="success",
                                 output={"title": "Collaborator approvals confirmed"})

    def test_normal_path_remains_blocked_and_explicit_preparation_never_claims_passing_ci(self):
        with self.assertRaisesRegex(ValueError, "required CI"):
            self.prepare()
        with redirect_stderr(io.StringIO()) as diagnostic:
            checked = self.helper.check_ci(
                "example/project", 12, defer_collaborator_approvals=APPROVAL_CHECK)
        self.assertEqual(checked["required_ci"], "deferred_collaborator_approvals")
        self.assertIn("not a passing CI result", diagnostic.getvalue())
        prepared = self.prepare_deferral()
        self.assertEqual(prepared["version"], 5)
        self.assertEqual(prepared["ci_deferral"], dict(APPROVAL_CHECK, check_run_id=71))
        self.assertNotIn("ci_exception", prepared)
        self.assertNotIn("required_ci", prepared)
        self.assert_read_only_preparation()

    def test_other_checks_workflows_and_missing_policy_must_pass_before_deferral(self):
        self.check_pages[0][0]["conclusion"] = "FAILURE"
        with self.assertRaisesRegex(ValueError, "required CI"):
            self.prepare_deferral()
        self.check_pages[0][0]["conclusion"] = "SUCCESS"
        self.add_required_workflow()["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "required workflow"):
            self.prepare_deferral()
        self.rules = []
        self.workflow_runs = []
        self.required_contexts.append("not-started")
        with self.assertRaisesRegex(ValueError, "missing required CI"):
            self.prepare_deferral()
        self.assertFalse(any("/check-runs/" in endpoint for _, endpoint, _ in self.calls))
        self.assert_read_only_preparation()

    def test_failed_queued_ambiguous_missing_or_non_required_checks_cannot_be_deferred(self):
        valid = copy.deepcopy(self.approval_node)
        for changed in (
            {"status": "COMPLETED", "conclusion": "FAILURE"},
            {"status": "COMPLETED", "conclusion": "CANCELLED"},
            {"status": "QUEUED"}, {"__typename": "StatusContext", "context": valid["name"],
                                  "state": "PENDING"},
            {"isRequired": False}, {"databaseId": None}, {"status": None},
            {"conclusion": False},
        ):
            with self.subTest(changed=changed):
                self.approval_node.clear()
                self.approval_node.update({**copy.deepcopy(valid), **changed})
                with self.assertRaises(ValueError):
                    self.prepare_deferral()
        self.approval_node.clear()
        self.approval_node.update(valid)
        self.check_pages[0].append(copy.deepcopy(valid))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.prepare_deferral()
        self.check_pages = [[self.check_pages[0][0]]]
        with self.assertRaisesRegex(ValueError, "missing required CI"):
            self.prepare_deferral()

    def test_no_other_reported_ci_can_use_the_selected_ordering_exception(self):
        optional = {"__typename": "CheckRun", "name": "additional-ci", "isRequired": False,
                    "status": "IN_PROGRESS", "conclusion": None}
        self.check_pages[0].append(optional)
        for status, conclusion in (("IN_PROGRESS", None), ("COMPLETED", "FAILURE")):
            with self.subTest(status=status):
                optional.update(status=status, conclusion=conclusion)
                with self.assertRaisesRegex(ValueError, "additional-ci"):
                    self.prepare_deferral()
        optional.update(status="COMPLETED", conclusion="SUCCESS")
        self.assertEqual(self.prepare_deferral()["ci_deferral"]["check_name"], APPROVAL_CHECK["check_name"])

    def test_only_the_exact_pending_collaborator_count_semantics_are_accepted(self):
        for title in (
            None, 3, "Awaiting approvals (3/3)", "Awaiting approvals (0/4)",
            "Awaiting approvals (01/3)", "Awaiting approvals (-1/3)",
            "Awaiting approvals (0/3) and tests", "Waiting for CI", "Verified",
        ):
            with self.subTest(title=title):
                self.approval_run["output"] = {"title": title}
                with self.assertRaisesRegex(ValueError, "strictly awaiting"):
                    self.prepare_deferral()
        self.approval_run["output"] = {"title": "Awaiting approvals (2/3)"}
        self.assertEqual(self.prepare_deferral()["ci_deferral"]["required_approvals"], 3)

    def test_provenance_and_current_head_are_verified_on_both_apis(self):
        valid = copy.deepcopy(self.approval_run)
        for change in (
            {"id": 99}, {"head_sha": self.first},
            {"url": "https://api.github.com/repos/other/project/check-runs/71"},
            {"name": "unrelated"}, {"app": {"id": 2002, "slug": "collaborator-review"}},
            {"app": {"id": 1001, "slug": "other-app"}},
            {"status": "queued"}, {"conclusion": "success"},
        ):
            with self.subTest(change=change):
                self.approval_run = {**copy.deepcopy(valid), **change}
                with self.assertRaisesRegex(ValueError, "provenance"):
                    self.prepare_deferral()
        self.approval_run = valid
        self.approval_node["checkSuite"]["app"]["databaseId"] = 2002
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.prepare_deferral()

    def test_rule_source_pins_and_partial_policy_fail_closed(self):
        self.rules = [{
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{
                "context": APPROVAL_CHECK["check_name"], "integration_id": 2002,
            }]},
        }]
        with self.assertRaisesRegex(ValueError, "different app"):
            self.prepare_deferral()
        self.rules[0]["parameters"]["required_status_checks"][0]["integration_id"] = True
        with self.assertRaisesRegex(ValueError, "app policy"):
            self.prepare_deferral()
        self.rules = []
        api = self.api

        def incomplete(endpoint, method="GET", data=None):
            result = api(endpoint, method, data)
            if endpoint == "graphql" and "EndorsementRequiredChecks" in data["query"]:
                node = result["data"]["node"]
                node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["totalCount"] += 1
            return result

        with mock.patch.object(self.helper, "github", side_effect=incomplete):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                self.prepare_deferral()
        self.failure = ("GET", "repos/example/project/check-runs/71")
        with self.assertRaisesRegex(RuntimeError, "fixture API failure"):
            self.prepare_deferral()

    def test_unused_or_mismatched_deferrals_cannot_be_prepared_as_latent_permissions(self):
        self.approve_collaborators()
        with self.assertRaisesRegex(ValueError, "unused"):
            self.prepare_deferral()
        self.assertNotIn("ci_deferral", self.prepare())
        with self.assertRaises(ValueError):
            self.prepare(defer_collaborator_approvals={**APPROVAL_CHECK, "check_name": "ci"})

    def test_signed_draft_verification_does_not_waive_final_readiness(self):
        self.signed_deferral()
        with self.assertRaisesRegex(ValueError, "required CI"):
            self.finish()
        self.assertTrue(self.original["draft"])
        result = self.helper.finish(self.receipt, self.approve("replace"), draft_only=True)
        self.assertFalse(result["ready_for_review"])
        self.assertEqual(result["ci_scope"], "operator_deferred_collaborator_approvals")
        self.approve_collaborators()
        self.assertTrue(self.finish()["ready_for_review"])

    def test_replacement_deferral_resumes_only_its_marked_signed_draft(self):
        self.signed_deferral("replacement")
        result = self.helper.finish(self.receipt, self.approve(), draft_only=True)
        self.assertFalse(result["ready_for_review"])
        self.assertEqual(self.original["state"], "closed")
        self.assertTrue(self.replacement["draft"])
        self.assertEqual(self.helper.finish(self.receipt, self.approve(), draft_only=True), result)
        with self.assertRaisesRegex(ValueError, "required CI"):
            self.finish()
        self.approve_collaborators()
        self.assertTrue(self.finish()["ready_for_review"])

    def test_signed_head_ci_failures_and_readiness_skips_remain_blocking(self):
        self.signed_deferral()
        self.check_pages[0][0]["conclusion"] = "FAILURE"
        with self.assertRaisesRegex(ValueError, "required CI"):
            self.helper.finish(self.receipt, self.approve("replace"), draft_only=True)
        self.check_pages[0][0]["conclusion"] = "SUCCESS"
        for conclusion in ("NEUTRAL", "SKIPPED"):
            self.approval_node.update(status="COMPLETED", conclusion=conclusion)
            self.approval_run.update(status="completed", conclusion=conclusion.lower())
            with self.assertRaisesRegex(ValueError, "strictly awaiting"):
                self.finish()
        self.assertTrue(self.original["draft"])

    def test_a_successful_signed_check_from_another_app_cannot_release_readiness(self):
        self.signed_deferral()
        self.approve_collaborators()
        self.approval_run["app"]["id"] = 2002
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.finish()
        self.assertTrue(self.original["draft"])

    def test_completed_approval_followed_by_a_failure_returns_the_same_pr_to_draft(self):
        self.signed_deferral()
        self.approve_collaborators()
        self.finish()
        self.approval_node["conclusion"] = "FAILURE"
        self.approval_run["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "returned the unchanged signed PR to draft"):
            self.finish()
        self.assertTrue(self.original["draft"])

    def test_original_run_or_repository_movement_requires_new_endorsement(self):
        self.follow_up(self.receipt, count=1)
        self.original["head"].update(ref=self.request["source_branch"], sha=self.request["head"])
        self.approval_run["head_sha"] = self.request["head"]
        self.request = self.prepare_deferral()
        self.freeze()
        self.approval_node["databaseId"] = 99
        with self.assertRaisesRegex(ValueError, "check run changed"):
            self.helper.await_attestation(self.plan)
        self.approval_node["databaseId"] = 71
        self.original["head"]["repo"]["full_name"] = "other/project"
        with self.assertRaises(ValueError):
            self.helper.await_attestation(self.plan)
        self.assertNotIn("fixture", self.helper.assignee_logins(self.original))

    def test_cli_requires_the_explicit_selector_and_supports_draft_only_verification(self):
        args = ["copilot-cs-endorse", "check-ci", "--repo", "example/project", "--pr", "12",
                "--defer-collaborator-approvals", "Collaborator Review", "1001",
                "collaborator-review", "3"]
        with mock.patch.dict(os.environ, {"CODESPACES": ""}), mock.patch.object(
                sys, "argv", args), redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            self.assertEqual(self.helper.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["required_ci"], "deferred_collaborator_approvals")
        self.signed_deferral()
        receipt_file = self.root / "receipt.json"
        receipt_file.write_text(json.dumps(self.receipt))
        args = ["copilot-cs-endorse", "finish", "--receipt", str(receipt_file),
                "--approve-plan", self.approve("replace"), "--draft-only"]
        with mock.patch.dict(os.environ, {"CODESPACES": ""}), mock.patch.object(
                sys, "argv", args), redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            self.assertEqual(self.helper.main(), 0)
        self.assertFalse(json.loads(output.getvalue())["ready_for_review"])


class ForkApprovalDeferralTests(ApprovalDeferralTests, ForkRepositoryTestCase):
    """Exercise the same ordering gates with distinct source and policy repositories."""

    def test_deferral_requires_the_enforced_upstream_workflow_and_its_source_fork(self):
        run = self.add_required_workflow()
        self.assertEqual(self.prepare_deferral()["ci_deferral"]["check_run_id"], 71)
        run["head_repository"]["full_name"] = "other/project"
        with self.assertRaisesRegex(ValueError, "missing required workflow"):
            self.prepare_deferral()
