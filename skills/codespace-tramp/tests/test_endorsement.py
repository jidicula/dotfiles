"""Endorsement regressions using local Git fixtures and an isolated test agent."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock
from urllib.parse import quote

from test_helpers import HelperTestCase, SETUP


class GitHubPaginationTests(HelperTestCase):
    def test_workflow_run_pages_are_complete_and_flattened(self):
        helper = self.load_script("copilot-cs-endorse")
        pages = [{"total_count": 2, "workflow_runs": [{"id": 1}]},
                 {"total_count": 2, "workflow_runs": [{"id": 2}]}]
        with mock.patch.object(helper, "run", return_value=json.dumps(pages).encode()) as command:
            self.assertEqual(helper.github_pages("fixture", field="workflow_runs"),
                             [{"id": 1}, {"id": 2}])
        self.assertIn("--paginate", command.call_args.args[0])
        self.assertIn("--slurp", command.call_args.args[0])

    def test_truncated_changing_or_malformed_workflow_pages_are_rejected(self):
        helper = self.load_script("copilot-cs-endorse")
        for pages in (
            [], [{}], [{"total_count": 2, "workflow_runs": [{"id": 1}]}],
            [{"total_count": 1, "workflow_runs": [{"id": 1}]},
             {"total_count": 2, "workflow_runs": [{"id": 2}]}],
            [{"total_count": True, "workflow_runs": []}],
            [{"total_count": 0, "workflow_runs": None}],
        ):
            with self.subTest(pages=pages), mock.patch.object(
                    helper, "run", return_value=json.dumps(pages).encode()):
                with self.assertRaisesRegex(ValueError, "paginated GitHub response"):
                    helper.github_pages("fixture", field="workflow_runs")


class CommitWorkflowDiscoveryTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.helper = self.load_script("copilot-cs-endorse")
        self.head = "a" * 40
        self.suite = {
            "id": "CS_100",
            "workflowRun": {
                "databaseId": 100, "runAttempt": 1, "event": "pull_request",
                "file": {"path": ".github/workflows/required.yml"},
                "workflow": {"databaseId": 456},
            },
        }

    def page(self, nodes, *, total=None, more=False, cursor=None):
        return {"data": {"repository": {"object": {
            "oid": self.head,
            "checkSuites": {
                "nodes": nodes, "totalCount": len(nodes) if total is None else total,
                "pageInfo": {"hasNextPage": more, "endCursor": cursor},
            },
        }}}}

    def discover(self, *responses):
        with mock.patch.object(self.helper, "github", side_effect=responses) as api, \
                mock.patch.object(self.helper, "github_pages") as search:
            result = self.helper.commit_workflow_suites("example/project", self.head)
            search.assert_not_called()
        return result, api.call_args_list

    def test_all_commit_suites_are_paginated_and_non_workflow_suites_are_ignored(self):
        unrelated = {"id": "CS_other_app", "workflowRun": None}
        dynamic = {"id": "CS_code_quality", "workflowRun": {
            **self.suite["workflowRun"], "databaseId": 200, "event": "dynamic", "file": None,
        }}
        result, calls = self.discover(
            self.page([unrelated, dynamic], total=3, more=True, cursor="next"),
            self.page([self.suite], total=3),
        )
        self.assertEqual(result, [self.suite])
        self.assertEqual(len(calls), 2)
        for call, cursor in zip(calls, (None, "next")):
            self.assertEqual(call.args[:2], ("graphql", "POST"))
            self.assertEqual(call.args[2]["variables"], {
                "owner": "example", "name": "project", "head": self.head, "cursor": cursor,
            })
            self.assertIn("checkSuites(first: 100", call.args[2]["query"])

    def test_a_commit_without_workflow_suites_returns_no_candidates(self):
        self.assertEqual(self.discover(self.page([]))[0], [])

    def test_missing_partial_or_wrong_revision_responses_fail_closed(self):
        missing_repository = {"data": {"repository": None}}
        wrong_revision = self.page([self.suite])
        wrong_revision["data"]["repository"]["object"]["oid"] = "b" * 40
        for response in (
            {}, missing_repository, wrong_revision,
            {**self.page([self.suite]), "errors": [{"message": "partial response"}]},
            self.page([self.suite], total=2),
            self.page([self.suite], total=True),
            self.page([self.suite], total=-1),
            self.page([self.suite], more=1),
            self.page([self.suite], total=2, more=True),
            self.page([], total=1, more=True, cursor="next"),
        ):
            with self.subTest(response=response):
                with self.assertRaisesRegex(ValueError, "workflow"):
                    self.discover(response)

    def test_malformed_suite_and_workflow_identities_fail_closed(self):
        for suite in (
            None, {}, {"id": "", "workflowRun": None}, {"id": "CS_100"},
            {"id": "CS_100", "workflowRun": []},
            {"id": "CS_100", "workflowRun": {}},
            {**self.suite, "workflowRun": {**self.suite["workflowRun"], "databaseId": True}},
            {**self.suite, "workflowRun": {**self.suite["workflowRun"], "file": None}},
        ):
            with self.subTest(suite=suite):
                with self.assertRaisesRegex(ValueError, "workflow"):
                    self.discover(self.page([suite]))

    def test_changed_counts_duplicate_suites_and_cursor_cycles_fail_closed(self):
        first = self.page([self.suite], total=2, more=True, cursor="next")
        other = {**self.suite, "id": "CS_200"}
        for second in (
            self.page([other], total=3),
            self.page([self.suite], total=2),
            self.page([other], total=2, more=True, cursor="next"),
        ):
            with self.subTest(second=second):
                with self.assertRaisesRegex(ValueError, "workflow"):
                    self.discover(first, second)

    def test_api_failure_does_not_fall_back_to_a_repository_run_search(self):
        with self.assertRaisesRegex(RuntimeError, "fixture API failure"):
            self.discover(RuntimeError("fixture API failure"))


class EndorsementTestCase(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.helper = self.load_script("copilot-cs-endorse")
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
            CODESPACES="true", SSH_AUTH_SOCK=str(self.root / "test-agent.sock"),
            GIT_AUTHOR_DATE="2025-01-02T03:04:05+0530",
            GIT_COMMITTER_DATE="2025-02-03T04:05:06-0400",
        )
        self.key_file = self.root / "test-only"
        self.command("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key_file))
        self.key_file.chmod(0o600)
        self.key = self.helper.public_key(Path(str(self.key_file) + ".pub").read_text())
        self.agent = subprocess.Popen(
            ["ssh-agent", "-D", "-a", self.env["SSH_AUTH_SOCK"]],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop_agent)
        deadline = time.monotonic() + 5
        while not Path(self.env["SSH_AUTH_SOCK"]).is_socket() and time.monotonic() < deadline:
            if self.agent.poll() is not None:
                self.fail("isolated fixture agent failed to start")
            time.sleep(0.01)
        self.assertTrue(Path(self.env["SSH_AUTH_SOCK"]).is_socket())
        self.command("ssh-add", str(self.key_file))
        self.env.update(COPILOT_CS_SIGNING_KEY=self.key,
                        COPILOT_CS_SIGNING_SOCKET=self.env["SSH_AUTH_SOCK"])
        environment = mock.patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        tempdir = mock.patch.object(tempfile, "tempdir", str(self.root))
        tempdir.start()
        self.addCleanup(tempdir.stop)
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.git("init", "--quiet", "--initial-branch=main", "--template=")
        self.git("config", "user.name", "Fixture Author")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("commit", "--quiet", "--allow-empty", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "--quiet", "-b", "feature")
        (self.root / "tracked").write_text("reviewed content\n")
        self.git("add", "tracked")
        self.git("commit", "--quiet", "--author=Other Author <other@example.invalid>",
                 "-m", "first\n\nbody with trailing spaces  \n")
        self.first = self.git("rev-parse", "HEAD").strip()
        self.git("commit", "--quiet", "--allow-empty", "-m", "empty commit")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.remote = self.root / "remote.git"
        self.git("init", "--quiet", "--bare", "--template=", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "--quiet", "origin", "main", "feature")
        self.repository = self.helper.Repository()
        # Only the origin-address check is replaced; ref reads and pushes use real Git.
        origin = mock.patch.object(self.repository, "validate_origin")
        origin.start()
        self.addCleanup(origin.stop)
        self.request = {
            "version": 1, "repository": "example/project", "pr": 12,
            "url": "https://github.com/example/project/pull/12",
            "source_branch": "feature", "target_branch": "feature-signed",
            "base_branch": "main", "head": self.head, "base": self.base,
            "attestor": "fixture",
            "publication_options": ["replacement", "replace"],
            "key": self.key, "fingerprint": self.helper.fingerprint(self.key),
        }

    def stop_agent(self):
        if self.agent.poll() is None:
            self.agent.terminate()
        self.agent.communicate(timeout=5)

    def command(self, *arguments):
        result = self.run_command(arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def git(self, *arguments):
        return self.command("git", *arguments)

    def freeze(self):
        self.plan = self.repository.plan(self.request)
        self.plan_id = self.plan["plan_id"]
        return self.plan_id

    def approve(self, publication="replacement"):
        return self.plan_id + ":" + publication

    def sign(self, publication="replacement"):
        self.freeze()
        return self.repository.sign(self.plan_id, self.approve(publication))

    def remote_head(self, branch):
        return self.git("--git-dir=" + str(self.remote), "rev-parse", "refs/heads/" + branch).strip()

    def advance_base(self, *, conflict=False, parent=None):
        remote = "--git-dir=" + str(self.remote)
        blob = self.repository.git(
            remote, "hash-object", "-w", "--stdin", data=b"upstream content\n",
        ).decode().strip()
        path = "tracked" if conflict else "upstream"
        tree = self.repository.git(
            remote, "mktree", data=f"100644 blob {blob}\t{path}\n".encode(),
        ).decode().strip()
        oid = self.git(remote, "-c", "user.name=Fixture Upstream",
                       "-c", "user.email=upstream@example.invalid",
                       "commit-tree", tree, "-p", parent or self.remote_head("main"),
                       "-m", "advance base").strip()
        self.git(remote, "update-ref", "refs/heads/main", oid)
        return oid

    def follow_up(self, previous, count=3):
        self.git("checkout", "--quiet", "-b", "followup", previous["head"])
        added = []
        for index in range(count):
            self.git("commit", "--quiet", "--allow-empty", "-m", f"followup {index}")
            added.append(self.git("rev-parse", "HEAD").strip())
        self.request.update(
            version=2, source_branch="followup", target_branch="followup-signed",
            head=self.git("rev-parse", "HEAD").strip(),
        )
        self.git("push", "--quiet", "origin", "followup")
        return added

    def base_merge_follow_up(self, previous, *, count=1, trailing_signature_newline=False):
        self.follow_up(previous, count=0)
        base = self.advance_base()
        self.git("fetch", "--quiet", "origin", "main")
        key = self.root / "platform-test-only"
        self.command("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key))
        key.chmod(0o600)
        self.command("ssh-add", str(key))
        self.git("-c", "user.name=GitHub", "-c", "user.email=noreply@github.com",
                 "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}",
                 "merge", "--quiet", "--no-ff", "-S", "-m", "Merge main", base)
        merge = self.git("rev-parse", "HEAD").strip()
        if trailing_signature_newline:
            raw = self.repository.git("cat-file", "commit", merge)
            header, _, message = raw.partition(b"\n\n")
            merge = self.repository.git(
                "hash-object", "-t", "commit", "-w", "--stdin",
                data=header + b"\n \n\n" + message,
            ).decode().strip()
            self.git("update-ref", "refs/heads/followup", merge)
        for index in range(count):
            self.git("commit", "--quiet", "--allow-empty", "-m", f"repair {index}")
        self.request.update(
            version=3, base=base, head=self.git("rev-parse", "HEAD").strip(),
            preserve_base_merges=[merge],
        )
        self.git("push", "--quiet", "origin", "followup")
        return merge

    def commit_data(self, oid):
        raw = self.repository.git("cat-file", "commit", oid)
        result = json.loads(self.git(
            "show", "-s", "--format="
            '{"sha":"%H","tree":{"sha":"%T"},"parents":"%P"}', oid,
        ))
        result["parents"] = [{"sha": parent} for parent in result["parents"].split()]
        blocks, message = self.repository.commit(oid)
        for field, prefix in (("author", "a"), ("committer", "c")):
            values = self.git("show", "-s", f"--format=%{prefix}n%x00%{prefix}e%x00%{prefix}I", oid)
            name, email, date = values.rstrip("\n").split("\0")
            result[field] = {"name": name, "email": email, "date": date}
        result["message"] = message.decode()
        signatures = [block[7:].replace(b"\n ", b"\n") + b"\n"
                      for block in blocks if block.startswith(b"gpgsig ")]
        result["verification"] = {
            "verified": bool(signatures),
            "reason": "valid" if signatures else "unsigned",
            "signature": signatures[0].decode().rstrip("\n") + "\n" if signatures else None,
            "payload": self.repository.payload(oid, {}).decode() if signatures else None,
        }
        self.assertEqual(hashlib.sha1(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest(), oid)
        return result


class IncrementalEndorsementTests(EndorsementTestCase):
    def test_only_three_new_commits_are_signed_and_prior_two_are_byte_identical(self):
        previous = self.sign()
        added = self.follow_up(previous)
        preserved = list(previous["mapping"].values())
        before = {oid: self.repository.git("cat-file", "commit", oid) for oid in preserved}
        self.freeze()
        self.assertEqual(self.plan["commits"], preserved + added)
        self.assertEqual(self.plan["preserved_commits"], preserved)
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(
                self.helper, "run", wraps=self.helper.run) as commands:
            receipt = self.repository.sign(self.plan_id, self.approve("replace"))
        signatures = [call for call in commands.call_args_list
                      if call.args[0][:3] == ["ssh-keygen", "-Y", "sign"]]
        self.assertEqual(len(signatures), 3)
        self.assertIn("signing 1/3:", output.getvalue())
        self.assertIn("signing 3/3:", output.getvalue())
        for oid in preserved:
            self.assertEqual(receipt["mapping"][oid], oid)
            self.assertEqual(self.repository.git("cat-file", "commit", oid), before[oid])
        for oid in added:
            self.assertNotEqual(receipt["mapping"][oid], oid)
            self.assertEqual(self.repository.payload(receipt["mapping"][oid], {}),
                             self.repository.payload(oid, receipt["mapping"]))
        first_parent = self.git("show", "-s", "--format=%P", receipt["mapping"][added[0]]).strip()
        self.assertEqual(first_parent, previous["head"])
        self.assertEqual(self.git("rev-parse", "followup").strip(), self.request["head"])
        self.assertEqual(self.repository.verify(self.plan_id), receipt)
        self.repository.push(self.plan_id, self.approve("replace"))
        self.assertEqual(self.remote_head("followup"), receipt["head"])
        with mock.patch.object(self.helper, "signed_commit", side_effect=AssertionError("resigned")):
            self.assertEqual(self.repository.sign(self.plan_id, self.approve("replace")), receipt)

    def test_committer_proposal_applies_only_to_new_commits(self):
        previous = self.sign()
        added = self.follow_up(previous)
        preserved = list(previous["mapping"].values())
        self.request["committer"] = {"name": "New Endorser", "email": "new@example.invalid"}
        receipt = self.sign()
        for oid in preserved:
            self.assertEqual(receipt["mapping"][oid], oid)
            self.assertEqual(self.commit_data(oid)["committer"]["name"], "Fixture Author")
        for oid in added:
            self.assertEqual(self.commit_data(receipt["mapping"][oid])["committer"]["name"],
                             "New Endorser")
        self.repository.verify(self.plan_id)

    def test_another_signing_key_does_not_count_as_prior_endorsement(self):
        previous = self.sign()
        self.follow_up(previous)
        key = self.root / "other-test-only"
        self.command("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key))
        key.chmod(0o600)
        public = self.helper.public_key(Path(str(key) + ".pub").read_text())
        self.request.update(key=public, fingerprint=self.helper.fingerprint(public))
        self.freeze()
        self.assertEqual(self.plan["preserved_commits"], [])

    def test_a_signed_descendant_with_an_unsigned_parent_must_be_reconstructed(self):
        self.git("-c", "gpg.format=ssh", "-c", f"user.signingkey={self.key_file}",
                 "commit", "--quiet", "--allow-empty", "-S", "-m", "signed descendant")
        self.request.update(version=2, head=self.git("rev-parse", "HEAD").strip())
        self.git("push", "--quiet", "origin", "feature")
        receipt = self.sign()
        self.assertEqual(self.plan["preserved_commits"], [])
        self.assertNotEqual(receipt["mapping"][self.request["head"]], self.request["head"])

    def test_corrupt_matching_key_signature_blocks_planning(self):
        previous = self.sign()
        original = previous["mapping"][self.first]
        raw = self.repository.git("cat-file", "commit", original) + b"tampered message\n"
        corrupt = self.repository.git(
            "hash-object", "-t", "commit", "-w", "--stdin", data=raw,
        ).decode().strip()
        self.follow_up({"head": corrupt}, count=1)
        records = sorted(self.repository.state.glob("*.plan.json"))
        with self.assertRaisesRegex(RuntimeError, "ssh-keygen failed"):
            self.freeze()
        self.assertEqual(sorted(self.repository.state.glob("*.plan.json")), records)

    def test_fully_endorsed_history_does_not_request_more_signatures(self):
        previous = self.sign()
        self.follow_up(previous, count=0)
        with self.assertRaisesRegex(ValueError, "no new commits require endorsement"):
            self.freeze()

    def test_preserved_selection_is_immutable_and_cannot_be_rewritten_in_receipt(self):
        previous = self.sign()
        self.follow_up(previous)
        receipt = self.sign()
        original = self.plan["preserved_commits"][0]
        modified = copy.deepcopy(receipt)
        modified["mapping"][original] = self.base
        with self.assertRaisesRegex(ValueError, "must remain unchanged"):
            self.helper.signed_receipt_request(modified, self.approve())
        path = self.repository.state / f"{self.plan_id}.receipt.json"
        path.write_text(json.dumps(modified))
        with self.assertRaisesRegex(ValueError, "must remain unchanged"):
            self.repository.verify(self.plan_id)
        plan = {key: copy.deepcopy(value) for key, value in self.plan.items() if key != "plan_id"}
        plan["preserved_commits"] = []
        with self.assertRaisesRegex(ValueError, "digest is invalid"):
            self.helper.reviewed_plan_request(plan, self.plan_id)

    def test_invalid_incremental_selections_are_rejected(self):
        previous = self.sign()
        self.follow_up(previous)
        self.freeze()
        plan = {key: value for key, value in self.plan.items() if key != "plan_id"}
        for preserved in (None, {}, [self.base], [plan["commits"][0]] * 2,
                          list(reversed(plan["preserved_commits"])), plan["commits"]):
            with self.subTest(preserved=preserved):
                changed = dict(plan, preserved_commits=preserved)
                with self.assertRaisesRegex(ValueError, "previously endorsed commit selection"):
                    self.helper.reviewed_plan_request(changed, self.helper.digest(changed))

    def test_legacy_plans_keep_their_original_full_range_semantics(self):
        previous = self.sign()
        self.follow_up(previous)
        self.request["version"] = 1
        self.freeze()
        self.assertNotIn("preserved_commits", self.plan)
        with mock.patch.object(self.helper, "run", wraps=self.helper.run) as commands:
            self.repository.sign(self.plan_id, self.approve())
        signatures = [call for call in commands.call_args_list
                      if call.args[0][:3] == ["ssh-keygen", "-Y", "sign"]]
        self.assertEqual(len(signatures), 5)
        plan = {key: value for key, value in self.plan.items() if key != "plan_id"}
        plan["preserved_commits"] = list(previous["mapping"].values())
        with self.assertRaisesRegex(ValueError, "requires a version 2 request"):
            self.helper.reviewed_plan_request(plan, self.helper.digest(plan))

    def test_malformed_ssh_envelopes_fail_explicitly(self):
        self.assertIsNone(self.helper.signature_public_key(b"-----BEGIN PGP SIGNATURE-----\n"))
        for envelope in (b"", b"SSHSIG\x00\x00\x00\x02",
                         b"SSHSIG\x00\x00\x00\x01\x00\x00\x00\x64short",
                         b"SSHSIG\x00\x00\x00\x01\x00\x00\x00\x04\x00\x00\x00\x01"):
            signature = (b"-----BEGIN SSH SIGNATURE-----\n"
                         + self.helper.base64.b64encode(envelope)
                         + b"\n-----END SSH SIGNATURE-----\n")
            with self.subTest(envelope=envelope), self.assertRaises(ValueError):
                self.helper.signature_public_key(signature)


class BaseMergeEndorsementTests(EndorsementTestCase):
    def test_only_new_child_is_signed_and_base_merge_remains_byte_identical(self):
        previous = self.sign()
        merge = self.base_merge_follow_up(previous)
        retained = [*previous["mapping"].values(), merge]
        before = {oid: self.repository.git("cat-file", "commit", oid) for oid in retained}
        self.request["committer"] = {"name": "New Endorser", "email": "new@example.invalid"}
        self.freeze()
        self.assertEqual(self.plan["preserved_commits"], list(previous["mapping"].values()))
        self.assertEqual(self.plan["preserved_base_merges"], [merge])
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(
                self.helper, "run", wraps=self.helper.run) as commands:
            receipt = self.repository.sign(self.plan_id, self.approve("replace"))
        signatures = [call for call in commands.call_args_list
                      if call.args[0][:3] == ["ssh-keygen", "-Y", "sign"]]
        self.assertEqual(len(signatures), 1)
        self.assertIn("signing 1/1: " + self.request["head"], output.getvalue())
        for oid in retained:
            self.assertEqual(receipt["mapping"][oid], oid)
            self.assertEqual(self.repository.git("cat-file", "commit", oid), before[oid])
        self.assertEqual(self.commit_data(merge)["committer"]["name"], "GitHub")
        self.assertEqual(self.commit_data(receipt["head"])["committer"]["name"], "New Endorser")
        self.assertEqual(self.git("show", "-s", "--format=%P", receipt["head"]).strip(), merge)
        self.assertEqual(self.repository.verify(self.plan_id), receipt)
        self.repository.push(self.plan_id, self.approve("replace"))
        self.assertEqual(self.remote_head("followup"), receipt["head"])
        with mock.patch.object(self.helper, "signed_commit", side_effect=AssertionError("resigned")):
            self.assertEqual(self.repository.sign(self.plan_id, self.approve("replace")), receipt)

    def test_version_two_still_requires_endorsement_of_a_foreign_signed_merge(self):
        previous = self.sign()
        merge = self.base_merge_follow_up(previous)
        self.request["version"] = 2
        del self.request["preserve_base_merges"]
        self.freeze()
        self.assertNotIn("preserved_base_merges", self.plan)
        self.assertNotIn(merge, self.plan["preserved_commits"])
        with mock.patch.object(self.helper, "run", wraps=self.helper.run) as commands:
            self.repository.sign(self.plan_id, self.approve())
        self.assertEqual(sum(call.args[0][:3] == ["ssh-keygen", "-Y", "sign"]
                             for call in commands.call_args_list), 2)

    def test_preserved_base_merge_mapping_and_classification_are_immutable(self):
        merge = self.base_merge_follow_up(self.sign())
        receipt = self.sign()
        changed = copy.deepcopy(receipt)
        changed["mapping"][merge] = self.base
        with self.assertRaisesRegex(ValueError, "must remain unchanged"):
            self.helper.signed_receipt_request(changed, self.approve())
        path = self.repository.state / f"{self.plan_id}.receipt.json"
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "must remain unchanged"):
            self.repository.verify(self.plan_id)
        plan = {key: value for key, value in self.plan.items() if key != "plan_id"}
        changed_plan = dict(plan, preserved_base_merges=[])
        with self.assertRaisesRegex(ValueError, "base.merge"):
            self.helper.reviewed_plan_request(changed_plan, self.helper.digest(changed_plan))
        changed_plan = dict(plan, preserved_commits=[*plan["preserved_commits"], merge])
        with self.assertRaisesRegex(ValueError, "base.merge"):
            self.helper.reviewed_plan_request(changed_plan, self.helper.digest(changed_plan))

    def test_base_merge_only_does_not_request_any_new_signatures(self):
        self.base_merge_follow_up(self.sign(), count=0)
        with self.assertRaisesRegex(ValueError, "no new commits require endorsement"):
            self.freeze()

    def test_requested_base_merge_must_be_in_the_reviewed_range(self):
        self.base_merge_follow_up(self.sign())
        self.request["preserve_base_merges"] = [self.base]
        with self.assertRaisesRegex(ValueError, "base.merge"):
            self.freeze()

    def test_preserving_an_ordinary_commit_is_rejected(self):
        self.base_merge_follow_up(self.sign())
        self.request["preserve_base_merges"] = [self.request["head"]]
        with self.assertRaisesRegex(ValueError, "base.merge"):
            self.freeze()

    def test_base_merge_cannot_preserve_a_new_unsigned_first_parent(self):
        previous = self.sign()
        added = self.follow_up(previous, count=1)
        self.git("branch", "-m", "followup", "unsigned-parent")
        self.base_merge_follow_up({"head": added[-1]})
        with self.assertRaisesRegex(ValueError, "base.merge"):
            self.freeze()

    def test_base_merge_requires_the_recorded_base_ancestry(self):
        self.base_merge_follow_up(self.sign())
        self.request["base"] = self.base
        with self.assertRaisesRegex(ValueError, "base.merge"):
            self.freeze()

    def test_a_merge_with_extra_content_cannot_be_preserved(self):
        merge = self.base_merge_follow_up(self.sign(), count=0)
        (self.root / "injected").write_text("not from either parent\n")
        self.git("add", "injected")
        self.git("-c", "user.name=GitHub", "-c", "user.email=noreply@github.com",
                 "-c", "gpg.format=ssh", "-c", f"user.signingkey={self.root / 'platform-test-only'}",
                 "commit", "--quiet", "--amend", "--no-edit", "-S")
        changed = self.git("rev-parse", "HEAD").strip()
        self.assertNotEqual(changed, merge)
        self.request.update(head=changed, preserve_base_merges=[changed])
        self.git("push", "--quiet", "--force-with-lease", "origin", "followup")
        with self.assertRaisesRegex(ValueError, "base.merge"):
            self.freeze()

    def test_unsigned_or_non_platform_merges_cannot_be_preserved(self):
        merge = self.base_merge_follow_up(self.sign(), count=0)
        for mode in ("unsigned", "different-committer"):
            with self.subTest(mode=mode):
                payload = self.repository.payload(
                    merge, {}, {"name": "Not GitHub", "email": "other@example.invalid"}
                    if mode == "different-committer" else None,
                )
                if mode == "different-committer":
                    signature = self.helper.run(
                        ["ssh-keygen", "-Y", "sign", "-n", "git", "-f",
                         str(self.root / "platform-test-only")], data=payload, env=self.env,
                    )
                    payload = self.helper.signed_commit(payload, signature)
                changed = self.repository.git(
                    "hash-object", "-t", "commit", "-w", "--stdin", data=payload,
                ).decode().strip()
                self.git("update-ref", "refs/heads/followup", changed)
                self.git("push", "--quiet", "--force-with-lease", "origin", "followup")
                self.request.update(head=changed, preserve_base_merges=[changed])
                with self.assertRaisesRegex(ValueError, "base.merge"):
                    self.freeze()

    def test_base_merge_opt_in_requires_a_new_request_version(self):
        self.base_merge_follow_up(self.sign())
        for version in (1, 2):
            with self.subTest(version=version):
                request = dict(self.request, version=version)
                with self.assertRaisesRegex(ValueError, "version 3"):
                    self.helper.validate_request(request)
        for merges in (None, [], {}, [self.base, self.base], ["not-an-oid"]):
            with self.subTest(merges=merges):
                request = dict(self.request, preserve_base_merges=merges)
                with self.assertRaisesRegex(ValueError, "base.merge"):
                    self.helper.validate_request(request)


class NoRequiredCIPlanTests(EndorsementTestCase):
    def test_exception_is_versioned_and_bound_to_the_immutable_plan(self):
        self.request.update(version=4, ci_exception="no_required_ci")
        receipt = self.sign()
        self.assertEqual(receipt["plan"]["request"]["ci_exception"], "no_required_ci")
        self.assertEqual(self.repository.verify(self.plan_id), receipt)
        altered = copy.deepcopy(receipt["plan"])
        del altered["request"]["ci_exception"]
        with self.assertRaisesRegex(ValueError, "digest"):
            self.helper.reviewed_plan_request(altered, self.plan_id)
        with self.assertRaisesRegex(ValueError, "explicit approval"):
            self.repository.sign(self.plan_id, "0" * 64 + ":replace")

    def test_legacy_versions_and_malformed_exceptions_are_rejected(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "version 4"):
                    self.helper.validate_request(dict(
                        self.request, version=version, ci_exception="no_required_ci"))
        for exception in (None, False, True, {}, "all_ci", ""):
            with self.subTest(exception=exception):
                with self.assertRaisesRegex(ValueError, "explicit no-required-CI"):
                    self.helper.validate_request(dict(
                        self.request, version=4, ci_exception=exception))
        with self.assertRaisesRegex(ValueError, "explicit no-required-CI"):
            self.helper.validate_request(dict(self.request, version=4))
        request = dict(self.request, version=4, ci_exception="no_required_ci")
        del request["attestor"]
        with self.assertRaisesRegex(ValueError, "attestor"):
            self.helper.validate_request(request)

    def test_exception_preserves_prior_endorsements_and_verified_base_merges(self):
        previous = self.sign()
        merge = self.base_merge_follow_up(previous)
        self.request.update(version=4, ci_exception="no_required_ci")
        receipt = self.sign()
        self.assertEqual(receipt["plan"]["preserved_base_merges"], [merge])
        self.assertEqual(receipt["mapping"][merge], merge)
        for oid in previous["mapping"].values():
            self.assertEqual(receipt["mapping"][oid], oid)
        self.assertEqual(self.repository.verify(self.plan_id), receipt)


class SignedHistoryTests(EndorsementTestCase):
    def test_signing_progress_precedes_requests_and_verification_never_resigns(self):
        output = io.StringIO()
        original_run = self.helper.run
        requests = []
        transport = self.load_script("copilot-gh-retry")
        progress = transport.SigningProgressNotifier()
        cursor = 0

        def track(arguments, **kwargs):
            nonlocal cursor
            if arguments[:3] == ["ssh-keygen", "-Y", "sign"]:
                requests.append(arguments)
                self.assertIn(f"] signing {len(requests)}/2:", output.getvalue())
                progress.feed(output.getvalue()[cursor:].encode())
                cursor = output.tell()
                self.assertEqual(notify.call_args_list,
                                 [mock.call(index, 2) for index in range(1, len(requests) + 1)])
            return original_run(arguments, **kwargs)

        with redirect_stdout(output), mock.patch.object(self.helper, "run", side_effect=track), \
                mock.patch.object(transport, "notify_signature") as notify:
            self.sign()
        self.assertEqual(len(requests), 2)
        output.seek(0)
        output.truncate()
        with redirect_stdout(output), mock.patch.object(self.helper, "run", wraps=original_run) as calls, \
                mock.patch.object(transport, "notify_signature") as notify:
            self.repository.verify(self.plan_id)
            self.repository.sign(self.plan_id, self.approve())
            progress.feed(output.getvalue().encode())
            notify.assert_not_called()
        self.assertIn("] verifying 1/2:", output.getvalue())
        self.assertIn("] verifying 2/2:", output.getvalue())
        self.assertNotIn("] signing ", output.getvalue())
        self.assertFalse(any(call.args[0][:3] == ["ssh-keygen", "-Y", "sign"]
                             for call in calls.call_args_list))

    def test_payload_failure_does_not_announce_a_signing_request(self):
        self.freeze()
        output = io.StringIO()
        with redirect_stdout(output), \
                mock.patch.object(self.repository, "payload",
                                  side_effect=ValueError("fixture payload failure")), \
                mock.patch.object(self.helper, "run", wraps=self.helper.run) as calls:
            with self.assertRaisesRegex(ValueError, "fixture payload failure"):
                self.repository.sign(self.plan_id, self.approve())
        self.assertNotIn("] signing ", output.getvalue())
        self.assertFalse(any(call.args[0][:3] == ["ssh-keygen", "-Y", "sign"]
                             for call in calls.call_args_list))

    def test_ecdsa_agent_signatures_verify_with_native_git(self):
        key = self.root / "test-only-ecdsa"
        self.command("ssh-keygen", "-q", "-t", "ecdsa", "-b", "256", "-N", "", "-f", str(key))
        key.chmod(0o600)
        self.command("ssh-add", str(key))
        public = self.helper.public_key(Path(str(key) + ".pub").read_text())
        self.request.update(key=public, fingerprint=self.helper.fingerprint(public))
        with mock.patch.dict(os.environ, COPILOT_CS_SIGNING_KEY=public):
            receipt = self.sign()
        allowed = self.root / "allowed"
        allowed.write_text("endorser " + public + "\n")
        self.git("-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={allowed}",
                 "verify-commit", receipt["head"])
        signature = self.commit_data(receipt["head"])["verification"]["signature"].encode()
        self.assertEqual(self.helper.signature_public_key(signature), public)

    def test_signatures_preserve_empty_commits_metadata_and_worktree(self):
        before = self.git("status", "--porcelain")
        receipt = self.sign()
        self.assertEqual(self.plan["commits"], [self.first, self.head])
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.head)
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.head)
        self.assertEqual(self.git("rev-parse", "feature-signed").strip(), receipt["head"])
        self.assertEqual(self.remote_head("feature"), self.head)
        self.assertEqual(self.git("status", "--porcelain"), before)
        allowed = self.root / "allowed-signers"
        allowed.write_text("endorser " + self.key + "\n")
        for original, signed in receipt["mapping"].items():
            self.assertNotEqual(original, signed)
            self.assertEqual(self.repository.payload(signed, {}),
                             self.repository.payload(original, receipt["mapping"]))
            self.git("-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={allowed}",
                     "verify-commit", signed)
        self.assertEqual(self.repository.verify(self.plan_id), receipt)

    def test_explicit_committer_changes_only_identity_and_preserves_dates_and_authors(self):
        self.request["committer"] = {"name": "Human Endorser", "email": "endorser@example.invalid"}
        receipt = self.sign()
        for original, signed in receipt["mapping"].items():
            blocks, _ = self.repository.commit(original)
            previous = next(block for block in blocks if block.startswith(b"committer "))
            timestamp, timezone = previous.rsplit(b" ", 2)[1:]
            expected = b"committer Human Endorser <endorser@example.invalid> " + timestamp + b" " + timezone
            payload = self.repository.payload(original, receipt["mapping"])
            self.assertEqual(self.repository.payload(signed, {}),
                             payload.replace(previous + b"\n", expected + b"\n", 1))
            before, after = self.commit_data(original), self.commit_data(signed)
            self.assertEqual(before["author"], after["author"])
            self.assertEqual(before["committer"]["date"], after["committer"]["date"])
        self.assertEqual(self.repository.verify(self.plan_id), receipt)
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.head)

    def test_committer_change_invalidates_an_existing_plan_approval(self):
        self.freeze()
        previous_plan = self.plan_id
        self.request["committer"] = {"name": "Human Endorser", "email": "endorser@example.invalid"}
        self.freeze()
        self.assertNotEqual(self.plan_id, previous_plan)
        with self.assertRaisesRegex(ValueError, "AND publication"):
            self.repository.sign(self.plan_id, previous_plan + ":replacement")
        self.assertFalse(list(self.repository.state.glob("*.receipt.json")))

    def test_malformed_committer_identity_is_rejected_before_signing(self):
        for committer in (
            None, {}, {"name": "Only name"}, {"email": "only@example.invalid"},
            {"name": "Endorser", "email": "endorser@example.invalid", "date": "changed"},
            {"name": "", "email": "endorser@example.invalid"},
            {"name": "Bad\nheader", "email": "endorser@example.invalid"},
            {"name": "Bad <name>", "email": "endorser@example.invalid"},
            {"name": "Endorser", "email": "invalid"},
            {"name": "Endorser", "email": "bad\n@example.invalid"},
        ):
            with self.subTest(committer=committer), self.assertRaisesRegex(ValueError, "committer"):
                self.repository.plan(dict(self.request, committer=committer))
        self.assertFalse(list(self.repository.state.glob("*.receipt.json")))

    def test_merge_topology_and_shared_ancestors_are_preserved(self):
        self.git("checkout", "--quiet", "-b", "side", self.first)
        (self.root / "side").write_text("side change\n")
        self.git("add", "side")
        self.git("commit", "--quiet", "-m", "side change")
        side = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "--quiet", "feature")
        self.git("merge", "--quiet", "--no-ff", "side", "-m", "merge")
        self.request["head"] = self.git("rev-parse", "HEAD").strip()
        self.git("push", "--quiet", "origin", "feature")
        receipt = self.sign()
        self.assertEqual(len(receipt["mapping"]), 4)
        parents = self.git("show", "-s", "--format=%P", receipt["head"]).strip().split()
        self.assertEqual(parents, [receipt["mapping"][self.head], receipt["mapping"][side]])
        self.repository.verify(self.plan_id)

    def test_approval_requires_both_exact_revision_and_publication_choice(self):
        self.freeze()
        for approval in (None, self.plan_id, "0" * 64 + ":replacement", self.plan_id + ":automatic"):
            with self.subTest(approval=approval), self.assertRaisesRegex(ValueError, "AND publication"):
                self.repository.sign(self.plan_id, approval)
        self.assertFalse(list(self.repository.state.glob("*.receipt.json")))

    def test_disallowed_rewrite_cannot_be_approved(self):
        self.request["publication_options"] = ["replacement"]
        self.freeze()
        with self.assertRaisesRegex(ValueError, "AND publication"):
            self.repository.sign(self.plan_id, self.approve("replace"))

    def test_missing_or_wrong_forwarded_key_stops_before_signing(self):
        self.freeze()
        for variable, value in (("COPILOT_CS_SIGNING_KEY", ""),
                                ("COPILOT_CS_SIGNING_SOCKET", str(self.root / "missing"))):
            with self.subTest(variable=variable), mock.patch.dict(os.environ, {variable: value}):
                with self.assertRaises(ValueError):
                    self.repository.sign(self.plan_id, self.approve())
        self.assertFalse(list(self.repository.state.glob("*.receipt.json")))

    def test_partial_signing_failure_does_not_create_receipt_or_branch(self):
        self.freeze()
        original_run = self.helper.run
        signatures = []

        def fail_second(arguments, **kwargs):
            if arguments[:3] == ["ssh-keygen", "-Y", "sign"]:
                signatures.append(arguments)
                if len(signatures) == 2:
                    raise RuntimeError("fixture signing refusal")
            return original_run(arguments, **kwargs)

        with mock.patch.object(self.helper, "run", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "fixture signing refusal"):
                self.repository.sign(self.plan_id, self.approve())
        self.assertEqual(len(signatures), 2)
        self.assertFalse(list(self.repository.state.glob("*.receipt.json")))
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", "refs/heads/feature-signed"), "")
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.head)

    def test_existing_receipt_reuses_signatures_but_not_another_publication_choice(self):
        receipt = self.sign()
        with mock.patch.object(self.helper, "signed_commit", side_effect=AssertionError("resigned")):
            self.assertEqual(self.repository.sign(self.plan_id, self.approve()), receipt)
            with self.assertRaisesRegex(ValueError, "publication choice differs"):
                self.repository.sign(self.plan_id, self.approve("replace"))

    def test_stale_local_source_or_conflicting_remote_base_blocks_signing(self):
        self.freeze()
        self.git("update-ref", "refs/heads/feature", self.first)
        with self.assertRaisesRegex(ValueError, "local source branch changed"):
            self.repository.sign(self.plan_id, self.approve())
        self.git("update-ref", "refs/heads/feature", self.head)
        self.advance_base(conflict=True)
        with self.assertRaisesRegex(ValueError, "merge conflicts"):
            self.repository.sign(self.plan_id, self.approve())
        self.assertFalse(list(self.repository.state.glob("*.receipt.json")))

    def test_clean_base_advances_preserve_the_plan_and_reuse_its_signatures(self):
        self.freeze()
        record = self.repository.state / f"{self.plan_id}.plan.json"
        frozen = record.read_bytes()
        base = self.advance_base()
        refs = self.git("show-ref")
        index = (self.root / ".git" / "index").read_bytes()
        fetch_head = self.root / ".git" / "FETCH_HEAD"
        fetch_head.write_text("previous fetch\n")
        receipt = self.repository.sign(self.plan_id, self.approve())
        self.assertEqual(record.read_bytes(), frozen)
        self.assertEqual(receipt["plan"]["request"]["base"], self.base)
        self.assertEqual(receipt["plan"]["commits"], [self.first, self.head])
        self.assertEqual((self.root / ".git" / "index").read_bytes(), index)
        self.assertEqual(fetch_head.read_text(), "previous fetch\n")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.head)
        self.assertEqual(self.git("show-ref").replace(
            f"{receipt['head']} refs/heads/feature-signed\n", ""), refs)
        self.assertEqual(self.git("cat-file", "-t", base).strip(), "commit")
        self.advance_base()
        with mock.patch.object(self.helper, "signed_commit", side_effect=AssertionError("resigned")):
            self.assertEqual(self.repository.sign(self.plan_id, self.approve()), receipt)
        self.assertEqual(record.read_bytes(), frozen)

    def test_base_advance_during_signing_does_not_discard_signatures(self):
        self.freeze()
        original = self.helper.run
        advanced = False

        def advance_during_signature(arguments, **kwargs):
            nonlocal advanced
            if arguments[:3] == ["ssh-keygen", "-Y", "sign"] and not advanced:
                advanced = True
                self.advance_base()
            return original(arguments, **kwargs)

        with mock.patch.object(self.helper, "run", side_effect=advance_during_signature):
            receipt = self.repository.sign(self.plan_id, self.approve())
        self.assertEqual(receipt["plan_id"], self.plan_id)
        self.assertEqual(self.repository.verify(self.plan_id), receipt)

    def test_remote_base_rewind_does_not_reuse_approval(self):
        self.request["base"] = self.advance_base()
        self.freeze()
        self.git("--git-dir=" + str(self.remote), "update-ref", "refs/heads/main", self.base)
        with self.assertRaisesRegex(ValueError, "without a fast-forward"):
            self.repository.sign(self.plan_id, self.approve())
        self.assertFalse(list(self.repository.state.glob("*.receipt.json")))

    def test_base_advance_during_mergeability_check_rechecks_the_same_plan(self):
        self.freeze()
        self.advance_base()
        original = self.repository.check_base_merge
        advanced = False

        def advance_after_check(*arguments):
            nonlocal advanced
            original(*arguments)
            if not advanced:
                advanced = True
                self.advance_base()

        with mock.patch.object(self.repository, "check_base_merge", side_effect=advance_after_check) as probe:
            self.repository.snapshot(self.request)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(self.repository.load_plan(self.plan_id)["request"], self.request)

    def test_mergeability_command_failure_is_not_a_clean_merge(self):
        self.freeze()
        self.advance_base()
        original = subprocess.run

        def fail_merge(arguments, **kwargs):
            if arguments[:3] == ["git", "merge-tree", "--write-tree"]:
                return subprocess.CompletedProcess(arguments, 128, b"", b"fixture merge failure")
            return original(arguments, **kwargs)

        with mock.patch.object(subprocess, "run", side_effect=fail_merge):
            with self.assertRaisesRegex(RuntimeError, "git merge-tree failed.*fixture merge failure"):
                self.repository.sign(self.plan_id, self.approve())
        self.assertFalse(list(self.repository.state.glob("*.receipt.json")))

    def test_tracked_worktree_changes_and_existing_signed_branch_stop_planning(self):
        (self.root / "tracked").write_text("unfinished work\n")
        with self.assertRaisesRegex(ValueError, "tracked worktree changes"):
            self.freeze()
        (self.root / "tracked").write_text("reviewed content\n")
        self.git("branch", "feature-signed", self.base)
        with self.assertRaisesRegex(ValueError, "signed branch already exists"):
            self.freeze()

    def test_changed_plan_and_receipt_are_rejected(self):
        self.sign()
        path = self.repository.state / f"{self.plan_id}.receipt.json"
        receipt = json.loads(path.read_text())
        receipt["mapping"][self.first] = self.base
        path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "content or metadata differs"):
            self.repository.verify(self.plan_id)
        plan_path = self.repository.state / f"{self.plan_id}.plan.json"
        plan = json.loads(plan_path.read_text())
        plan["root"] += "-changed"
        plan_path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "plan changed"):
            self.repository.verify(self.plan_id)

    def test_signed_merge_tags_and_git_replacement_objects_are_explicitly_unsupported(self):
        raw = (b"tree " + self.git("rev-parse", "HEAD^{tree}").strip().encode()
               + b"\nmergetag object fixture\n continued\n\nmessage\n")
        with mock.patch.object(self.repository, "git", return_value=raw):
            with self.assertRaisesRegex(ValueError, "merge tags require manual"):
                self.repository.commit(self.head)
        self.git("replace", self.head, self.first)
        with self.assertRaisesRegex(ValueError, "replacement objects"):
            self.helper.Repository()

    def test_origin_fetch_and_push_destinations_must_both_match(self):
        self.git("remote", "set-url", "origin", "https://github.com/example/project.git")
        self.helper.Repository.validate_origin(self.repository, self.request)
        self.git("remote", "set-url", "--push", "origin", "git@github.com:other/project.git")
        with self.assertRaisesRegex(ValueError, "fetch/push URL"):
            self.helper.Repository.validate_origin(self.repository, self.request)

    def test_remote_actions_refuse_local_execution(self):
        self.env["CODESPACES"] = "false"
        result = self.run_command(["python3", str(SETUP / "copilot-cs-endorse"),
                                   "plan", "--request", "{}"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("inside the selected Codespace", result.stderr)

    def test_github_actions_refuse_the_codespace_integration_environment(self):
        for arguments, message in (
            (["prepare", "--repo", "example/project", "--pr", "12"], "preparation locally"),
            (["await-attestation", "--plan", "unused.json"], "attestation tracking locally"),
            (["complete-attestation", "--receipt", "unused.json",
              "--approve-plan", "unused"], "attestation tracking locally"),
        ):
            with self.subTest(action=arguments[0]):
                result = self.run_command(
                    ["python3", str(SETUP / "copilot-cs-endorse"), *arguments])
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_prepare_requires_both_committer_flags_before_any_api_request(self):
        self.env["CODESPACES"] = "false"
        self.executable("gh", "raise AssertionError('unexpected API request')")
        for arguments in (["--committer-name", "Human Endorser"],
                          ["--committer-email", "endorser@example.invalid"]):
            with self.subTest(arguments=arguments):
                result = self.run_command([
                    "python3", str(SETUP / "copilot-cs-endorse"), "prepare",
                    "--repo", "example/project", "--pr", "12", *arguments,
                ])
                self.assertEqual(result.returncode, 1)
                self.assertIn("provide both committer name and email", result.stderr)
                self.assertNotIn("unexpected API request", result.stderr)

    def test_malformed_review_input_is_rejected(self):
        for request in (None, [], {}, dict(self.request, key=None),
                        dict(self.request, target_branch="main"),
                        dict(self.request, attestor=""), dict(self.request, attestor="../other"),
                        dict(self.request, source_branch="../unsafe")):
            with self.subTest(request=request), self.assertRaises((ValueError, RuntimeError)):
                self.repository.plan(request)


class PlanningTests(EndorsementTestCase):
    def setUp(self):
        super().setUp()
        self.request["base"] = self.advance_remote_base(self.base)

    def advance_remote_base(self, parent):
        oid = self.git(
            "--git-dir=" + str(self.remote),
            "-c", "user.name=Fixture Upstream",
            "-c", "user.email=upstream@example.invalid",
            "commit-tree", self.base + "^{tree}", "-p", parent, "-m", "upstream change",
        ).strip()
        self.git("--git-dir=" + str(self.remote), "update-ref", "refs/heads/main", oid)
        return oid

    def test_plan_fetches_missing_reviewed_base_without_changing_refs_or_worktree(self):
        self.request["base"] = self.advance_remote_base(self.request["base"])
        refs = self.git("show-ref")
        status = self.git("status", "--porcelain")
        fetch_head = self.root / ".git" / "FETCH_HEAD"
        fetch_head.write_text("previous fetch receipt\n")
        self.freeze()
        self.assertEqual(self.plan["request"], self.request)
        self.assertEqual(self.plan["commits"], [self.first, self.head])
        self.assertEqual(self.git("cat-file", "-t", self.request["base"]).strip(), "commit")
        self.assertEqual(self.git("show-ref"), refs)
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.assertEqual(fetch_head.read_text(), "previous fetch receipt\n")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.head)
        self.assertFalse(list(self.repository.state.glob("*.receipt.json")))

    def test_existing_base_does_not_fetch(self):
        self.git("fetch", "--quiet", "origin", self.request["base"])
        with mock.patch.object(self.repository, "git", wraps=self.repository.git) as git:
            self.freeze()
        self.assertFalse(any(call.args[0] == "fetch" for call in git.call_args_list))

    def test_history_fetch_streams_progress_without_a_total_duration_limit(self):
        with mock.patch.object(self.helper, "run", wraps=self.helper.run) as run:
            self.freeze()
        fetches = [call for call in run.call_args_list
                   if call.args[0][:2] == ["git", "fetch"]]
        self.assertEqual(len(fetches), 1)
        self.assertIsNone(fetches[0].kwargs["timeout"])
        self.assertTrue(fetches[0].kwargs["echo"])
        self.assertIn("--progress", fetches[0].args[0])
        self.assertEqual(self.plan["request"], self.request)
        self.assertFalse(list(self.repository.state.glob("*.receipt.json")))

    def test_history_fetch_policy_does_not_remove_metadata_deadlines(self):
        with mock.patch.object(self.helper, "run", wraps=self.helper.run) as run:
            self.assertEqual(self.repository.ref("HEAD"), self.head)
        self.assertEqual(run.call_args.kwargs["timeout"], 120)
        self.assertFalse(run.call_args.kwargs["echo"])

    def test_clean_base_advance_before_planning_preserves_the_requested_range(self):
        reviewed = copy.deepcopy(self.request)
        current = self.advance_remote_base(self.request["base"])
        self.freeze()
        self.assertEqual(self.plan["request"], reviewed)
        self.assertEqual(self.plan["commits"], [self.first, self.head])
        self.assertEqual(self.git("cat-file", "-t", current).strip(), "commit")
        self.assertFalse(list(self.repository.state.glob("*.receipt.json")))

    def test_clean_base_advance_during_fetch_preserves_the_request(self):
        original = self.repository.git
        advanced = False

        def advance_after_fetch(*arguments, **kwargs):
            nonlocal advanced
            result = original(*arguments, **kwargs)
            if arguments[0] == "fetch" and not advanced:
                advanced = True
                self.advance_remote_base(self.request["base"])
            return result

        reviewed = copy.deepcopy(self.request)
        with mock.patch.object(self.repository, "git", side_effect=advance_after_fetch):
            self.freeze()
        self.assertEqual(self.plan["request"], reviewed)
        self.assertEqual(self.plan["commits"], [self.first, self.head])

    def test_failed_fetch_does_not_record_a_plan_or_change_the_request(self):
        original = self.repository.git
        reviewed = copy.deepcopy(self.request)

        def fail_fetch(*arguments, **kwargs):
            if arguments[0] == "fetch":
                raise RuntimeError("fixture fetch access denied")
            return original(*arguments, **kwargs)

        with mock.patch.object(self.repository, "git", side_effect=fail_fetch):
            with self.assertRaisesRegex(RuntimeError, "fixture fetch access denied"):
                self.freeze()
        self.assertEqual(self.request, reviewed)
        self.assertFalse(list(self.repository.state.glob("*.plan.json")))
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.head)

    def test_ref_changes_during_fetch_invalidate_the_request(self):
        original = self.repository.git
        for changed_ref in ("main", "feature"):
            with self.subTest(changed_ref=changed_ref):
                self.git("--git-dir=" + str(self.remote), "update-ref",
                         "refs/heads/main", self.request["base"])
                self.git("--git-dir=" + str(self.remote), "update-ref",
                         "refs/heads/feature", self.head)
                self.request["base"] = self.advance_remote_base(self.request["base"])

                def move_ref_after_fetch(*arguments, **kwargs):
                    result = original(*arguments, **kwargs)
                    if arguments[0] == "fetch":
                        self.git("--git-dir=" + str(self.remote), "update-ref",
                                 "refs/heads/" + changed_ref, self.first)
                    return result

                with mock.patch.object(self.repository, "git", side_effect=move_ref_after_fetch):
                    with self.assertRaisesRegex(ValueError, "remote (base|source branch) changed"):
                        self.freeze()
                self.assertFalse(list(self.repository.state.glob("*.plan.json")))


class PublicationTests(EndorsementTestCase):
    def test_pty_push_preserves_terminal_for_git_hooks(self):
        receipt = self.sign()
        hook = self.root / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(
            "#!/bin/sh\n[ -t 1 ] && [ -t 2 ] || { echo 'hook has no terminal' >&2; exit 23; }\n"
            "printf '%s\\n' 'terminal-hook-ran'\n"
        )
        hook.chmod(0o755)
        command = (
            "import runpy, sys\n"
            "helper = runpy.run_path(sys.argv[1])\n"
            "repository = helper['Repository']()\n"
            "repository.validate_origin = lambda request: None\n"
            "repository.push(sys.argv[2], sys.argv[3])\n"
        )
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", command, str(SETUP / "copilot-cs-endorse"),
                 self.plan_id, self.approve()],
                cwd=self.root, env=self.env, stdin=slave, stdout=slave, stderr=slave,
            )
        finally:
            os.close(slave)
        output = bytearray()
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 0.2)
                if readable:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                elif process.poll() is not None:
                    break
            self.assertEqual(process.wait(timeout=5), 0, output.decode(errors="replace"))
            self.assertIn(b"terminal-hook-ran", output)
            self.assertEqual(self.remote_head("feature-signed"), receipt["head"])
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    def test_signing_and_publication_share_one_nonblocking_plan_lock(self):
        self.sign()
        with self.repository.locked_plan(self.plan_id):
            for action in ("sign", "push"):
                with self.subTest(action=action):
                    result = self.run_command([
                        "python3", str(SETUP / "copilot-cs-endorse"), action,
                        "--plan", self.plan_id, "--approve-plan", self.approve(),
                    ])
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("already running", result.stderr)
        self.assertEqual(self.remote_head("feature"), self.head)

    def test_push_does_not_implicitly_publish_tags_or_skip_hooks(self):
        self.git("config", "push.followTags", "true")
        self.git("tag", "-a", "unreviewed-tag", "-m", "unreviewed")
        hook = self.root / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\nprintf '%s\\n' 'fixture hook ran'\nexit 1\n")
        hook.chmod(0o755)
        self.sign()
        with self.assertRaises(RuntimeError):
            self.repository.push(self.plan_id, self.approve())
        hook.write_text("#!/bin/sh\nprintf '%s\\n' 'fixture hook ran'\n")
        self.repository.push(self.plan_id, self.approve())
        self.assertEqual(self.git("--git-dir=" + str(self.remote),
                                  "for-each-ref", "--format=%(refname)", "refs/tags"), "")

    def test_replacement_push_is_create_only_and_original_is_unchanged(self):
        receipt = self.sign()
        with mock.patch.object(self.repository, "git", wraps=self.repository.git) as calls:
            self.assertEqual(self.repository.push(self.plan_id, self.approve()), receipt)
        push = next(call.args for call in calls.call_args_list if call.args[0] == "push")
        self.assertIn("--force-with-lease=refs/heads/feature-signed:", push)
        self.assertEqual(self.remote_head("feature"), self.head)
        self.assertEqual(self.remote_head("feature-signed"), receipt["head"])
        with mock.patch.object(self.repository, "git", wraps=self.repository.git) as calls:
            self.repository.push(self.plan_id, self.approve())
        self.assertFalse(any(call.args[0] == "push" for call in calls.call_args_list))

    def test_in_place_push_uses_the_exact_reviewed_head_and_can_resume(self):
        receipt = self.sign("replace")
        with mock.patch.object(self.repository, "git", wraps=self.repository.git) as calls:
            self.repository.push(self.plan_id, self.approve("replace"))
        push = next(call.args for call in calls.call_args_list if call.args[0] == "push")
        self.assertIn("--force-with-lease=refs/heads/feature:" + self.head, push)
        self.assertEqual(self.remote_head("feature"), receipt["head"])
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.head)
        self.assertEqual(self.repository.sign(self.plan_id, self.approve("replace")), receipt)
        self.assertEqual(self.repository.push(self.plan_id, self.approve("replace")), receipt)

    def test_failed_rewrite_can_use_new_explicit_replacement_approval_without_resigning(self):
        self.sign("replace")
        original_git = self.repository.git

        def reject(*arguments, **kwargs):
            if arguments[0] == "push":
                raise RuntimeError("fixture policy rejects rewriting")
            return original_git(*arguments, **kwargs)

        with mock.patch.object(self.repository, "git", side_effect=reject):
            with self.assertRaisesRegex(RuntimeError, "policy rejects"):
                self.repository.push(self.plan_id, self.approve("replace"))
        with self.assertRaisesRegex(ValueError, "AND publication"):
            self.repository.push(self.plan_id, self.plan_id)
        with mock.patch.object(self.helper, "signed_commit", side_effect=AssertionError("resigned")):
            receipt = self.repository.push(self.plan_id, self.approve("replacement"))
        self.assertEqual(receipt["publication"], "replacement")
        self.assertEqual(self.remote_head("feature"), self.head)
        self.assertEqual(json.loads((self.repository.state /
                                    f"{self.plan_id}.replacement.published.json").read_text()), receipt)

    def test_successful_publication_cannot_switch_methods(self):
        self.sign()
        self.repository.push(self.plan_id, self.approve())
        with self.assertRaisesRegex(ValueError, "already published"):
            self.repository.push(self.plan_id, self.approve("replace"))
        self.assertEqual(self.remote_head("feature"), self.head)

    def test_concurrent_destination_creation_is_not_overwritten_even_when_fast_forwardable(self):
        self.sign()
        original_git = self.repository.git

        def race(*arguments, **kwargs):
            if arguments[0] == "push":
                self.git("--git-dir=" + str(self.remote), "update-ref",
                         "refs/heads/feature-signed", self.base)
            return original_git(*arguments, **kwargs)

        with mock.patch.object(self.repository, "git", side_effect=race):
            with self.assertRaises(RuntimeError):
                self.repository.push(self.plan_id, self.approve())
        self.assertEqual(self.remote_head("feature-signed"), self.base)
        self.assertEqual(self.remote_head("feature"), self.head)

    def test_concurrent_source_change_rejects_the_exact_head_lease(self):
        self.sign("replace")
        original_git = self.repository.git

        def race(*arguments, **kwargs):
            if arguments[0] == "push":
                self.git("--git-dir=" + str(self.remote), "update-ref", "refs/heads/feature", self.first)
            return original_git(*arguments, **kwargs)

        with mock.patch.object(self.repository, "git", side_effect=race):
            with self.assertRaises(RuntimeError):
                self.repository.push(self.plan_id, self.approve("replace"))
        self.assertEqual(self.remote_head("feature"), self.first)

    def test_conflicting_base_change_during_push_does_not_report_publication_complete(self):
        self.sign()
        original_git = self.repository.git

        def race(*arguments, **kwargs):
            result = original_git(*arguments, **kwargs)
            if arguments[0] == "push":
                self.advance_base(conflict=True)
            return result

        with mock.patch.object(self.repository, "git", side_effect=race):
            with self.assertRaisesRegex(ValueError, "merge conflicts"):
                self.repository.push(self.plan_id, self.approve())
        self.assertFalse(list(self.repository.state.glob("*.published.json")))

    def assert_clean_base_advance_during_push(self, publication):
        self.sign(publication)
        original_git = self.repository.git

        def race(*arguments, **kwargs):
            result = original_git(*arguments, **kwargs)
            if arguments[0] == "push":
                self.advance_base()
            return result

        with mock.patch.object(self.repository, "git", side_effect=race):
            receipt = self.repository.push(self.plan_id, self.approve(publication))
        target = "feature" if publication == "replace" else "feature-signed"
        self.assertEqual(self.remote_head(target), receipt["head"])
        self.assertEqual(receipt["plan"]["request"]["base"], self.base)
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.head)

    def test_clean_base_advance_during_replacement_push_preserves_approval(self):
        self.assert_clean_base_advance_during_push("replacement")

    def test_clean_base_advance_during_in_place_push_preserves_approval(self):
        self.assert_clean_base_advance_during_push("replace")

    def test_conflicting_base_advance_before_push_preserves_the_unpublished_receipt(self):
        receipt = self.sign()
        self.advance_base(conflict=True)
        with self.assertRaisesRegex(ValueError, "merge conflicts"):
            self.repository.push(self.plan_id, self.approve())
        self.assertEqual(self.repository.verify(self.plan_id), receipt)
        self.assertFalse(list(self.repository.state.glob("*.published.json")))
        self.assertEqual(self.git("--git-dir=" + str(self.remote),
                                  "for-each-ref", "--format=%(refname)",
                                  "refs/heads/feature-signed"), "")


class GitHubEndorsementTests(EndorsementTestCase):
    def setUp(self):
        super().setUp()
        self.request["version"] = 2
        self.receipt = self.sign()
        self.original = {
            "number": 12, "node_id": "PR_original",
            "html_url": self.request["url"], "state": "open",
            "draft": True, "merged": False, "title": "Implement change",
            "body": "See example/other#23 and PR 45.\r\nOriginal context.",
            "head": {"ref": "feature", "sha": self.head, "repo": {"full_name": "example/project"}},
            "base": {"ref": "main", "sha": self.base, "repo": {"full_name": "example/project"}},
            "labels": [{"name": "review"}], "assignees": [{"login": "maintainer"}],
            "milestone": {"number": 7}, "requested_reviewers": [{"login": "reviewer"}],
            "requested_teams": [{"slug": "team"}],
        }
        self.replacement = None
        self.live_base = self.base
        self.mergeable = "MERGEABLE"
        self.base_relation = "ahead"
        self.operator_keys = [{"key": self.key}]
        self.operator_login = "fixture"
        self.comments = []
        self.calls = []
        self.failure = None
        self.drop_metadata = False
        self.drop_assignment = False
        self.mutate_on_assignment = None
        self.mutate_on_comment = None
        self.required_contexts = ["ci"]
        self.rules = []
        self.workflow_runs = []
        self.listed_workflow_runs = None
        self.workflow_suite_page_size = 100
        self.workflow_sources = {}
        self.workflow_provenance = {}
        self.mutate_on_workflow = None
        self.check_pages = [[{
            "__typename": "CheckRun", "name": "ci", "isRequired": True,
            "status": "COMPLETED", "conclusion": "SUCCESS",
        }]]
        self.check_head = None
        self.published_head = None
        self.mutate_on_checks = None
        self.mutate_on_ready = None
        self.ready_failure = False
        self.drop_ready = False
        self.draft_failure = False
        self.drop_draft = False
        self.mutate_on_draft = None
        self.commits = {oid: self.commit_data(oid) for oid in
                        [*self.receipt["mapping"], *self.receipt["mapping"].values()]}
        api = mock.patch.object(self.helper, "github", side_effect=self.api)
        pages = mock.patch.object(self.helper, "github_pages", side_effect=self.pages)
        api.start()
        pages.start()
        self.addCleanup(api.stop)
        self.addCleanup(pages.stop)

    def pages(self, endpoint, *, field=None):
        self.calls.append(("GET-PAGES", endpoint, None))
        if "/rules/branches/" in endpoint:
            return copy.deepcopy(self.rules)
        if "/actions/runs?" in endpoint:
            self.assertEqual(field, "workflow_runs")
            target = self.replacement or self.original
            self.assertIn("head_sha=" + target["head"]["sha"], endpoint)
            return copy.deepcopy(self.workflow_runs if self.listed_workflow_runs is None
                                 else self.listed_workflow_runs)
        if endpoint == "users/fixture/ssh_signing_keys?per_page=100":
            return copy.deepcopy(self.operator_keys)
        if "/pulls?" in endpoint:
            return [copy.deepcopy(self.replacement)] if self.replacement else []
        if endpoint.endswith("/comments?per_page=100"):
            return copy.deepcopy(self.comments)
        raise AssertionError(endpoint)

    def api(self, endpoint, method="GET", data=None):
        self.calls.append((method, endpoint, data))
        if self.failure == (method, endpoint):
            raise RuntimeError("fixture API failure")
        if endpoint == "user":
            return {"login": self.operator_login}
        if endpoint == "repos/example/project":
            return {"default_branch": "main"}
        if "/compare/" in endpoint:
            base, head = endpoint.split("/compare/", 1)[1].split("?", 1)[0].split("...")
            self.assertEqual(head, self.live_base)
            return {"status": self.base_relation, "base_commit": {"sha": base},
                    "merge_base_commit": {"sha": base}}
        if endpoint.startswith("repositories/"):
            parts = endpoint.split("/")
            source = self.workflow_sources[int(parts[1])]
            if len(parts) == 2:
                return {"id": int(parts[1]), "full_name": source["repository"]}
            self.assertEqual(parts[2], "commits")
            self.assertEqual(parts[3], quote(source["ref"], safe=""))
            return {"sha": source["sha"]}
        if "/actions/runs/" in endpoint:
            matches = [run for run in self.workflow_runs
                       if run["id"] == int(endpoint.rsplit("/", 1)[1])]
            if not matches:
                raise RuntimeError("fixture workflow API failure")
            result = copy.deepcopy(matches[0])
            if self.mutate_on_workflow:
                self.mutate_on_workflow()
            return result
        if endpoint.endswith("/assignees") and method in ("POST", "DELETE"):
            pr = (self.original if endpoint == "repos/example/project/issues/12/assignees"
                  else self.replacement)
            if not self.drop_assignment:
                logins = {login.casefold() for login in data["assignees"]}
                if method == "DELETE":
                    pr["assignees"] = [user for user in pr["assignees"]
                                       if user["login"].casefold() not in logins]
                else:
                    existing = {user["login"].casefold() for user in pr["assignees"]}
                    pr["assignees"].extend({"login": login} for login in data["assignees"]
                                           if login.casefold() not in existing)
            if self.mutate_on_assignment:
                self.mutate_on_assignment(pr)
            return copy.deepcopy(pr)
        if endpoint == "graphql":
            if "EndorsementCommitWorkflows" in data["query"]:
                variables = data["variables"]
                self.assertEqual((variables["owner"], variables["name"]), ("example", "project"))
                offset = int(variables["cursor"] or 0)
                suites = [{
                    "id": run["check_suite_node_id"],
                    "workflowRun": copy.deepcopy(self.workflow_provenance[run["check_suite_node_id"]]),
                } for run in self.workflow_runs]
                end = offset + self.workflow_suite_page_size
                return {"data": {"repository": {"object": {
                    "oid": variables["head"],
                    "checkSuites": {
                        "nodes": suites[offset:end], "totalCount": len(suites),
                        "pageInfo": {"hasNextPage": end < len(suites), "endCursor": str(end)},
                    },
                }}}}
            if "EndorsementWorkflowRun" in data["query"]:
                return {"data": {"node": {
                    "workflowRun": copy.deepcopy(self.workflow_provenance[data["variables"]["id"]]),
                }}}
            pr = (self.original if data["variables"]["id"] == "PR_original"
                  else self.replacement)
            if "EndorsementBaseCompatibility" in data["query"]:
                return {"data": {"node": {
                    "id": pr["node_id"], "state": pr["state"].upper(),
                    "headRefOid": pr["head"]["sha"], "headRefName": pr["head"]["ref"],
                    "baseRefName": pr["base"]["ref"],
                    "baseRef": {"target": {"oid": self.live_base}},
                    "mergeable": self.mergeable,
                }}}
            if "markPullRequestReadyForReview" in data["query"]:
                if self.ready_failure:
                    raise RuntimeError("fixture readiness failure")
                if not self.drop_ready:
                    pr["draft"] = False
                if self.mutate_on_ready:
                    self.mutate_on_ready(pr)
                return {"data": {"markPullRequestReadyForReview": {
                    "pullRequest": {"id": pr["node_id"], "isDraft": pr["draft"]},
                }}}
            if "convertPullRequestToDraft" in data["query"]:
                if self.draft_failure:
                    raise RuntimeError("fixture draft rollback failure")
                if not self.drop_draft:
                    pr["draft"] = True
                if self.mutate_on_draft:
                    self.mutate_on_draft(pr)
                return {"data": {"convertPullRequestToDraft": {
                    "pullRequest": {"id": pr["node_id"], "isDraft": pr["draft"]},
                }}}
            page = int(data["variables"]["cursor"] or 0)
            last = page == len(self.check_pages) - 1
            response = {"data": {"node": {
                "id": pr["node_id"], "state": pr["state"].upper(),
                "headRefOid": pr["head"]["sha"], "headRefName": pr["head"]["ref"],
                "baseRefName": pr["base"]["ref"],
                "mergeable": self.mergeable,
                "baseRef": {
                    "target": {"oid": self.live_base},
                    "branchProtectionRule": {
                        "requiresStatusChecks": bool(self.required_contexts),
                        "requiredStatusCheckContexts": self.required_contexts,
                    },
                },
                "commits": {"nodes": [{"commit": {
                    "oid": self.check_head or pr["head"]["sha"],
                    "statusCheckRollup": {"contexts": {
                        "nodes": copy.deepcopy(self.check_pages[page]),
                        "pageInfo": {"hasNextPage": not last,
                                     "endCursor": None if last else str(page + 1)},
                    }},
                }}]},
            }}}
            if self.mutate_on_checks:
                self.mutate_on_checks(pr)
            return response
        if endpoint == "repos/example/project/pulls/12":
            if method == "PATCH":
                self.original.update(data)
            return copy.deepcopy(self.original)
        if "/git/commits/" in endpoint:
            return copy.deepcopy(self.commits[endpoint.rsplit("/", 1)[1]])
        base_branch = self.original["base"]["ref"]
        if endpoint == f"repos/example/project/git/ref/heads/{quote(base_branch, safe='')}":
            return {"ref": "refs/heads/" + base_branch,
                    "object": {"type": "commit", "sha": self.live_base}}
        for branch, head in ((self.original["head"]["ref"], self.original["head"]["sha"]),
                             (self.request["target_branch"], self.receipt["head"])):
            if endpoint == f"repos/example/project/git/ref/heads/{quote(branch, safe='')}":
                return {"ref": "refs/heads/" + branch,
                        "object": {"type": "commit", "sha": self.published_head or head}}
        if "/branches/" in endpoint:
            return {"commit": {"sha": self.receipt["head"]}}
        if endpoint == "repos/example/project/pulls" and method == "POST":
            self.replacement = copy.deepcopy(self.original)
            self.replacement.update(
                number=13, node_id="PR_replacement",
                html_url="https://github.com/example/project/pull/13",
                title=data["title"], body=data["body"], requested_reviewers=[], requested_teams=[],
                labels=[], assignees=[], milestone=None,
            )
            self.replacement["head"].update(ref=data["head"], sha=self.receipt["head"])
            return copy.deepcopy(self.replacement)
        if endpoint == "repos/example/project/pulls/13":
            return copy.deepcopy(self.replacement)
        if endpoint == "repos/example/project/issues/13" and method == "PATCH":
            if self.drop_metadata:
                return copy.deepcopy(self.replacement)
            self.replacement.update(
                labels=[{"name": name} for name in data["labels"]],
                assignees=[{"login": login} for login in data["assignees"]],
                milestone={"number": data["milestone"]} if data["milestone"] else None,
            )
            return copy.deepcopy(self.replacement)
        if endpoint.endswith("/13/requested_reviewers"):
            self.replacement["requested_reviewers"] = [{"login": name} for name in data["reviewers"]]
            self.replacement["requested_teams"] = [{"slug": name} for name in data["team_reviewers"]]
            return copy.deepcopy(self.replacement)
        if endpoint.endswith("/12/comments") and method == "POST":
            self.comments.append(data)
            if self.mutate_on_comment:
                self.mutate_on_comment()
            return data
        raise AssertionError((method, endpoint, data))

    def finish(self):
        return self.helper.finish(self.receipt, self.approve(self.receipt["publication"]))

    def prepare(self, **options):
        with mock.patch.object(self.helper, "replacement_allowed", return_value=True):
            return self.helper.prepare("example/project", 12, str(self.key_file) + ".pub",
                                       **options)

    def conflicting_base_advance(self, _pr=None):
        self.live_base = self.first
        self.mergeable = "CONFLICTING"

    def assert_read_only_calls(self):
        self.assertTrue(all(
            method in ("GET", "GET-PAGES")
            or (endpoint == "graphql" and any(name in data["query"] for name in (
                "EndorsementRequiredChecks", "EndorsementWorkflowRun",
                "EndorsementCommitWorkflows", "EndorsementBaseCompatibility")))
            for method, endpoint, data in self.calls
        ))

    def assert_read_only_preparation(self):
        self.assertTrue(self.original["draft"])
        self.assertEqual(self.original["state"], "open")
        self.assertIsNone(self.replacement)
        self.assertEqual(self.comments, [])
        self.assert_read_only_calls()

    def add_required_workflow(self, *, run_id=100, path=".github/workflows/required.yml"):
        source = {"repository": "example/policy", "ref": "refs/heads/main", "sha": "a" * 40}
        self.workflow_sources[123] = source
        self.rules.append({"type": "workflows", "parameters": {"workflows": [{
            "repository_id": 123, "path": path, "ref": source["ref"],
        }]}})
        run = {
            "id": run_id, "workflow_id": 456, "run_attempt": 1,
            "workflow_url": "https://api.github.com/repos/example/project/actions/required_workflows/456",
            "check_suite_node_id": f"CS_{run_id}", "event": "pull_request",
            "head_sha": self.original["head"]["sha"], "head_branch": "feature",
            "path": path, "status": "completed", "conclusion": "success",
            "repository": {"full_name": "example/project"},
            "pull_requests": [{
                "number": 12, "head": {"sha": self.original["head"]["sha"]},
                "base": {"ref": "main"},
            }],
        }
        self.workflow_runs.append(run)
        self.workflow_provenance[f"CS_{run_id}"] = {
            "databaseId": run_id, "runAttempt": 1, "event": "pull_request",
            "file": {
                "path": path, "repositoryName": source["repository"],
                "repositoryFileUrl": f"https://github.com/example/policy/blob/{source['sha']}/{path}",
            },
            "workflow": {
                "databaseId": 456,
                "resourcePath": f"/example/project/actions/workflows/required/example/policy/{path}",
            },
        }
        return run

    def test_prepare_verifies_required_workflow_source_and_exact_head(self):
        self.add_required_workflow()
        self.add_required_workflow(run_id=101, path=".github/workflows/other.yml")
        self.assertEqual(self.prepare()["head"], self.head)
        self.assert_read_only_preparation()

    def test_workflow_discovery_does_not_depend_on_an_empty_repository_run_search(self):
        self.add_required_workflow()
        self.listed_workflow_runs = []
        self.assertEqual(self.prepare()["head"], self.head)
        self.assertFalse(any("/actions/runs?" in endpoint for _, endpoint, _ in self.calls))
        self.assert_read_only_preparation()

    def test_partial_repository_run_search_cannot_hide_a_newer_failed_workflow(self):
        older = self.add_required_workflow()
        newer = self.add_required_workflow(run_id=200)
        newer["conclusion"] = "failure"
        self.listed_workflow_runs = [older]
        with self.assertRaisesRegex(ValueError, "required workflow is not succeeding"):
            self.prepare()
        self.assert_read_only_preparation()

    def test_required_workflow_discovery_uses_every_commit_suite_page(self):
        self.add_required_workflow()
        self.add_required_workflow(run_id=200, path=".github/workflows/other.yml")
        self.workflow_suite_page_size = 1
        self.assertEqual(self.prepare()["head"], self.head)
        self.assertEqual(sum(
            endpoint == "graphql" and "EndorsementCommitWorkflows" in data["query"]
            for _, endpoint, data in self.calls
        ), 2)
        self.assert_read_only_preparation()

    def test_a_newer_failed_workflow_on_a_later_suite_page_blocks_preparation(self):
        self.add_required_workflow()
        self.add_required_workflow(run_id=200)["conclusion"] = "failure"
        self.workflow_suite_page_size = 1
        with self.assertRaisesRegex(ValueError, "required workflow is not succeeding"):
            self.prepare()
        self.assert_read_only_preparation()

    def test_run_metadata_must_match_its_discovered_commit_suite(self):
        self.add_required_workflow()
        for change in (
            {"id": 999}, {"check_suite_node_id": "CS_other"},
            {"path": ".github/workflows/other.yml"},
        ):
            with self.subTest(change=change):
                def api(endpoint, method="GET", data=None):
                    result = self.api(endpoint, method, data)
                    if endpoint == "repos/example/project/actions/runs/100":
                        result.update(change)
                    return result
                with mock.patch.object(self.helper, "github", side_effect=api):
                    with self.assertRaisesRegex(ValueError, "does not match its commit check suite"):
                        self.prepare()
                self.assert_read_only_preparation()

    def test_required_workflow_only_policy_still_has_to_report_a_passing_run(self):
        self.add_required_workflow()
        self.required_contexts = []
        self.check_pages[0][0]["isRequired"] = False
        self.assertEqual(self.prepare()["head"], self.head)
        self.workflow_runs.clear()
        with self.assertRaisesRegex(ValueError, "missing required workflow"):
            self.prepare()
        self.assert_read_only_preparation()

    def test_missing_workflow_diagnostics_capture_the_failing_selection_without_extra_reads(self):
        run = self.add_required_workflow()
        run.update(pull_requests=[], display_title="unrelated fixture metadata")
        expected = {
            "repository": "example/project", "pr": 12, "head": self.head,
            "branch": "feature", "base_branch": "main",
            "required_source": "example/policy/.github/workflows/required.yml",
            "runs_returned": 1,
            "candidates": [{"run_id": 100, "rejected_by": ["pull-request-association"]}],
        }
        for action in (self.prepare, self.await_attestation):
            with self.subTest(action=action):
                self.calls.clear()
                with self.assertRaisesRegex(ValueError, "missing required workflow") as failure:
                    action()
                message = str(failure.exception)
                self.assertIn("; workflow selection: ", message)
                self.assertEqual(json.loads(message.split("; workflow selection: ", 1)[1]), expected)
                self.assertNotIn("unrelated fixture metadata", message)
                self.assertFalse(any("/actions/runs?" in endpoint
                                     for _, endpoint, _ in self.calls))
                self.assertEqual(sum("/actions/runs/" in endpoint
                                     for _, endpoint, _ in self.calls), 1)
                self.assertFalse(self.assignment_calls())
                self.assert_read_only_preparation()

    def test_missing_workflow_diagnostics_distinguish_each_run_filter(self):
        run = self.add_required_workflow()
        original = copy.deepcopy(run)
        for change, rejected_by in (
            ({"workflow_url": "https://api.github.com/repos/example/project/actions/workflows/456"},
             ["workflow-url"]),
            ({"head_sha": self.first, "head_branch": "other"}, ["head-sha", "head-branch"]),
            ({"event": "workflow_dispatch"}, ["event"]),
            ({"repository": {"full_name": "example/other"}}, ["repository"]),
            ({"pull_requests": [{"number": 99}]}, ["pull-request-association"]),
            ({"pull_requests": [{"number": 12, "head": {"sha": self.first},
                                 "base": {"ref": "main"}}]}, ["pull-request-association"]),
            ({"pull_requests": [{"number": 12, "head": {"sha": self.head},
                                 "base": {"ref": "other"}}]}, ["pull-request-association"]),
        ):
            with self.subTest(change=change):
                run.clear()
                run.update(copy.deepcopy(original))
                run.update(change)
                with self.assertRaisesRegex(ValueError, "missing required workflow") as failure:
                    self.prepare()
                message = str(failure.exception)
                self.assertIn("; workflow selection: ", message)
                selection = json.loads(message.split("; workflow selection: ", 1)[1])
                self.assertEqual(selection["candidates"],
                                 [{"run_id": 100, "rejected_by": rejected_by}])
                self.assert_read_only_preparation()

    def test_missing_workflow_diagnostics_distinguish_an_absent_path_from_absent_runs(self):
        run = self.add_required_workflow()
        run["path"] = ".github/workflows/unrelated.yml"
        self.workflow_provenance["CS_100"]["file"]["path"] = run["path"]
        for count in (1, 0):
            with self.subTest(count=count):
                with self.assertRaisesRegex(ValueError, "missing required workflow") as failure:
                    self.prepare()
                message = str(failure.exception)
                self.assertIn("; workflow selection: ", message)
                selection = json.loads(message.split("; workflow selection: ", 1)[1])
                self.assertEqual(selection["runs_returned"], count)
                self.assertEqual(selection["candidates"], [])
                self.assert_read_only_preparation()
            self.workflow_runs.clear()

    def test_missing_workflow_diagnostics_identify_a_different_source(self):
        run = self.add_required_workflow()
        source = self.workflow_provenance["CS_100"]["file"]
        source["repositoryName"] = "example/other"
        with self.assertRaisesRegex(ValueError, "missing required workflow") as failure:
            self.prepare()
        message = str(failure.exception)
        self.assertIn("; workflow selection: ", message)
        selection = json.loads(message.split("; workflow selection: ", 1)[1])
        self.assertEqual(selection["candidates"], [{
            "run_id": 100, "rejected_by": ["source-identity"],
            "source": {"repository": "example/other", "path": run["path"]},
        }])
        self.assert_read_only_preparation()

    def test_required_workflow_blocks_preparation_and_assignment_until_success(self):
        run = self.add_required_workflow()
        for status, conclusion in (
            ("queued", None), ("in_progress", None), ("completed", "failure"),
            ("completed", "cancelled"), ("completed", "timed_out"),
            ("completed", "action_required"), ("completed", None),
        ):
            with self.subTest(status=status, conclusion=conclusion):
                run.update(status=status, conclusion=conclusion)
                with self.assertRaisesRegex(ValueError, "required workflow"):
                    self.prepare()
                with self.assertRaisesRegex(ValueError, "required workflow"):
                    self.helper.await_attestation(self.plan)
                self.assertFalse(self.assignment_calls())
        run.update(status="completed", conclusion="success")
        self.assertEqual(self.prepare()["head"], self.head)

    def test_latest_required_workflow_run_cannot_be_replaced_by_an_older_success(self):
        self.add_required_workflow(run_id=100)
        newer = self.add_required_workflow(run_id=200)
        self.workflow_runs.reverse()
        newer.update(status="queued", conclusion=None)
        with self.assertRaisesRegex(ValueError, "required workflow"):
            self.prepare()
        newer.update(status="completed", conclusion="success")
        self.assertEqual(self.prepare()["head"], self.head)

    def test_same_named_workflow_without_required_source_provenance_cannot_satisfy_policy(self):
        run = self.add_required_workflow()
        original_run = copy.deepcopy(run)
        original_provenance = copy.deepcopy(self.workflow_provenance["CS_100"])
        for change in (
            lambda: run.update(workflow_url=run["workflow_url"].replace("/required_workflows/", "/workflows/")),
            lambda: self.workflow_provenance["CS_100"]["file"].update(repositoryName="example/other"),
            lambda: self.workflow_provenance["CS_100"]["file"].update(repositoryFileUrl="https://github.com/example/other/blob/" + "a" * 40 + "/" + run["path"]),
            lambda: self.workflow_provenance["CS_100"]["workflow"].update(resourcePath="/example/project/actions/workflows/required.yml"),
            lambda: self.workflow_provenance["CS_100"].update(databaseId=999),
            lambda: self.workflow_provenance["CS_100"].update(runAttempt=2),
        ):
            with self.subTest(change=change):
                run.clear()
                run.update(copy.deepcopy(original_run))
                self.workflow_provenance["CS_100"] = copy.deepcopy(original_provenance)
                change()
                with self.assertRaisesRegex((ValueError, RuntimeError), "workflow"):
                    self.prepare()
                self.assert_read_only_preparation()

    def test_workflow_for_another_revision_pr_or_event_cannot_satisfy_policy(self):
        run = self.add_required_workflow()
        original = copy.deepcopy(run)
        for change in (
            {"head_sha": self.first}, {"head_branch": "other"},
            {"pull_requests": []}, {"pull_requests": [{"number": 99}]},
            {"pull_requests": None}, {"pull_requests": [{"number": 12, "head": None}]},
            {"repository": None},
            {"event": "workflow_dispatch"}, {"repository": {"full_name": "example/other"}},
        ):
            with self.subTest(change=change):
                run.clear()
                run.update(copy.deepcopy(original))
                run.update(change)
                with self.assertRaisesRegex(ValueError, "workflow"):
                    self.prepare()
                self.assert_read_only_preparation()

    def test_workflow_api_failures_and_revision_races_remain_blocking(self):
        self.add_required_workflow()
        for endpoint in ("repositories/123", "repos/example/project/actions/runs/100"):
            with self.subTest(endpoint=endpoint):
                self.failure = ("GET", endpoint)
                with self.assertRaisesRegex(RuntimeError, "fixture API failure"):
                    self.prepare()
                self.assert_read_only_preparation()
        self.failure = None
        self.mutate_on_workflow = self.conflicting_base_advance
        with self.assertRaisesRegex(ValueError, "base branch changed"):
            self.prepare()
        self.assert_read_only_preparation()

    def test_workflow_quality_gate_is_repeated_on_the_signed_head(self):
        run = self.add_required_workflow()
        self.assertEqual(self.prepare()["head"], self.head)
        for publication in ("replace", "replacement"):
            with self.subTest(publication=publication):
                self.receipt["publication"] = publication
                self.original.update(state="open", draft=True)
                self.original["head"]["sha"] = (
                    self.receipt["head"] if publication == "replace" else self.head)
                self.replacement = None
                self.calls.clear()
                run.update(head_sha=self.head, head_branch="feature")
                with self.assertRaisesRegex(ValueError, "required workflow"):
                    self.finish()
                self.assertFalse(self.readiness_calls())
                target = self.original if publication == "replace" else self.replacement
                run.update(head_sha=self.receipt["head"], head_branch=target["head"]["ref"])
                run["pull_requests"] = [{
                    "number": target["number"], "head": {"sha": self.receipt["head"]},
                    "base": {"ref": "main"},
                }]
                self.assertTrue(self.finish()["ready_for_review"])

    def test_invalid_required_workflow_policies_fail_closed(self):
        self.add_required_workflow()
        policy = self.rules[0]["parameters"]["workflows"][0]
        original = copy.deepcopy(policy)
        for change in ({"repository_id": True}, {"repository_id": 0}, {"path": ""},
                       {"path": "../required.yml"}, {"ref": None}, {"sha": "invalid"}):
            with self.subTest(change=change):
                policy.clear()
                policy.update(original)
                policy.update(change)
                with self.assertRaisesRegex(ValueError, "workflow"):
                    self.prepare()
                self.assert_read_only_preparation()

    def test_pinned_required_workflow_sha_does_not_follow_a_moved_branch(self):
        self.add_required_workflow()
        self.rules[0]["parameters"]["workflows"][0]["sha"] = "a" * 40
        self.workflow_sources[123]["sha"] = "b" * 40
        self.assertEqual(self.prepare()["head"], self.head)
        self.assertFalse(any("/commits/" in endpoint for _, endpoint, _ in self.calls))

    def test_moved_workflow_source_ref_accepts_the_recorded_successful_revision(self):
        run = self.add_required_workflow()
        self.workflow_sources[123]["sha"] = "b" * 40
        for ref in ("refs/heads/main", "refs/tags/production"):
            self.workflow_sources[123]["ref"] = ref
            self.rules[0]["parameters"]["workflows"][0]["ref"] = ref
            self.failure = ("GET", f"repositories/123/commits/{quote(ref, safe='')}")
            for attempt in (1, 2):
                with self.subTest(ref=ref, attempt=attempt):
                    run["run_attempt"] = attempt
                    self.workflow_provenance["CS_100"]["runAttempt"] = attempt
                    self.assertEqual(self.prepare()["head"], self.head)
                    self.assert_read_only_preparation()
        self.assertFalse(any("/commits/" in endpoint for _, endpoint, _ in self.calls))

    def test_pinned_required_workflow_rejects_a_different_recorded_revision(self):
        self.add_required_workflow()
        self.rules[0]["parameters"]["workflows"][0]["sha"] = "b" * 40
        with self.assertRaisesRegex(ValueError, "workflow pinned source revision changed"):
            self.prepare()
        with self.assertRaisesRegex(ValueError, "workflow pinned source revision changed"):
            self.helper.await_attestation(self.plan)
        self.assertFalse(self.assignment_calls())
        self.assert_read_only_preparation()

    def test_required_workflow_revision_url_must_identify_its_exact_source(self):
        self.add_required_workflow()
        source = self.workflow_provenance["CS_100"]["file"]
        url = source["repositoryFileUrl"]
        for invalid in (
            None, 123, url.replace("https:", "http:"),
            url.replace("example/policy", "example/other"),
            url.replace("a" * 40, "main"),
            url.replace("required.yml", "other.yml"),
            url + "?ref=main", url + "#fragment",
        ):
            with self.subTest(url=invalid):
                source["repositoryFileUrl"] = invalid
                with self.assertRaisesRegex(ValueError, "invalid required workflow source revision"):
                    self.prepare()
                self.assert_read_only_preparation()

    def test_missing_workflow_provenance_and_rerun_races_fail_closed(self):
        self.add_required_workflow()
        for change in (
            lambda result: result.update(errors=[{"message": "permission denied"}]),
            lambda result: result.update(data=None),
            lambda result: result["data"].update(repository=None),
            lambda result: result["data"]["repository"]["object"]["checkSuites"]["nodes"][0].update(
                workflowRun=None),
            lambda result: result["data"]["repository"]["object"]["checkSuites"]["nodes"][0][
                "workflowRun"].update(file=None),
        ):
            with self.subTest(change=change):
                def api(endpoint, method="GET", data=None):
                    result = self.api(endpoint, method, data)
                    if endpoint == "graphql" and "EndorsementCommitWorkflows" in data["query"]:
                        change(result)
                    return result
                with mock.patch.object(self.helper, "github", side_effect=api):
                    with self.assertRaisesRegex(ValueError, "workflow"):
                        self.prepare()
        for changed_read in (1, 2):
            reads = 0
            def rerun(endpoint, method="GET", data=None):
                nonlocal reads
                result = self.api(endpoint, method, data)
                if endpoint == "repos/example/project/actions/runs/100":
                    reads += 1
                    if reads == changed_read:
                        result["run_attempt"] += 1
                return result
            with self.subTest(changed_read=changed_read), mock.patch.object(
                    self.helper, "github", side_effect=rerun):
                with self.assertRaisesRegex(ValueError, r"required workflow (?:run )?changed"):
                    self.prepare()
        self.assert_read_only_preparation()

    def readiness_calls(self):
        return [call for call in self.calls if call[1] == "graphql"
                and "markPullRequestReadyForReview" in call[2]["query"]]

    def assignment_calls(self):
        return [call for call in self.calls if call[1].endswith("/assignees")]

    def await_attestation(self):
        return self.helper.await_attestation(self.plan)

    def complete_attestation(self, receipt=None, approval=None):
        return self.helper.complete_attestation(
            self.receipt if receipt is None else receipt,
            self.approve() if approval is None else approval,
        )

    def test_copied_plan_record_preserves_the_complete_attestation_request(self):
        record = self.root / "copied-plan-record.json"
        shutil.copyfile(self.repository.state / f"{self.plan_id}.plan.json", record)
        result = subprocess.run(
            [sys.executable, "-c",
             "import json, sys\n"
             "print(json.dumps(dict(plan_id=sys.argv[1], **json.load(sys.stdin))))",
             self.plan_id],
            cwd=self.root, env=self.env, input=record.read_text(),
            text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        copied = json.loads(result.stdout)
        self.assertEqual(copied, self.plan)
        self.assertTrue(self.helper.await_attestation(copied)["awaiting_attestation"])

    def test_waiting_for_attestation_assigns_only_the_planned_operator_once(self):
        original = copy.deepcopy(self.original)
        result = self.await_attestation()
        self.assertEqual(result, {
            "url": self.original["html_url"], "head": self.head,
            "attestor": "fixture", "awaiting_attestation": True,
        })
        self.assertEqual(self.original["assignees"],
                         [{"login": "maintainer"}, {"login": "fixture"}])
        self.assertEqual(self.await_attestation(), result)
        self.assertEqual(self.assignment_calls(), [
            ("POST", "repos/example/project/issues/12/assignees", {"assignees": ["fixture"]}),
        ])
        original["assignees"] = self.original["assignees"]
        self.assertEqual(self.original, original)
        self.assertFalse(self.readiness_calls())

    def test_attestation_assignment_rechecks_the_quality_gate(self):
        self.check_pages[0][0].update(status="IN_PROGRESS", conclusion=None)
        with self.assertRaisesRegex(ValueError, "required CI"):
            self.await_attestation()
        self.assertEqual(self.original["assignees"], [{"login": "maintainer"}])
        self.assertFalse(self.assignment_calls())

    def test_completed_signatures_remove_only_the_attestor_without_waiting_for_ci(self):
        self.await_attestation()
        self.calls.clear()
        self.check_pages[0][0].update(status="IN_PROGRESS", conclusion=None)
        with mock.patch.object(self.repository, "sign", side_effect=AssertionError("must not re-sign")):
            result = self.complete_attestation()
            self.assertEqual(self.complete_attestation(), result)
        self.assertEqual(result, {
            "url": self.original["html_url"], "head": self.head,
            "attestor": "fixture", "awaiting_attestation": False,
        })
        self.assertEqual(self.original["assignees"], [{"login": "maintainer"}])
        self.assertEqual(self.assignment_calls(), [
            ("DELETE", "repos/example/project/issues/12/assignees", {"assignees": ["fixture"]}),
        ])
        self.assertTrue(self.original["draft"])
        self.assertFalse(self.readiness_calls())

    def test_approval_alone_or_an_incomplete_receipt_does_not_remove_assignment(self):
        self.await_attestation()
        invalid_receipts = [
            self.plan,
            dict(self.receipt, mapping={}),
            dict(self.receipt, head=self.head),
            dict(self.receipt, mapping={**self.receipt["mapping"], self.first: "invalid"}),
            dict(self.receipt, publication="replace"),
        ]
        self.calls.clear()
        for receipt in invalid_receipts:
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                self.complete_attestation(receipt)
        for approval in ("", "wrong-plan:replacement", self.approve("replace")):
            with self.subTest(approval=approval), self.assertRaises(ValueError):
                self.complete_attestation(approval=approval)
        self.assertIn({"login": "fixture"}, self.original["assignees"])
        self.assertFalse(self.assignment_calls())

    def test_assignment_actions_reject_another_operator_or_stale_plan(self):
        self.await_attestation()
        self.calls.clear()
        self.operator_login = "someone-else"
        for action in (self.await_attestation, self.complete_attestation):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "operator"):
                action()
        self.operator_login = "fixture"
        self.original["head"]["sha"] = self.first
        for action in (self.await_attestation, self.complete_attestation):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "changed"):
                action()
        self.assertFalse(self.assignment_calls())
        self.assertIn({"login": "fixture"}, self.original["assignees"])

    def test_assignment_actions_reject_a_changed_plan_or_unknown_attestor(self):
        result = copy.deepcopy(self.plan)
        result["request"]["attestor"] = "other"
        with self.assertRaisesRegex(ValueError, "plan changed"):
            self.helper.await_attestation(result)
        plan = {key: copy.deepcopy(value) for key, value in self.plan.items() if key != "plan_id"}
        del plan["request"]["attestor"]
        with self.assertRaisesRegex(ValueError, "no attestor"):
            self.helper.await_attestation({"plan_id": self.helper.digest(plan), **plan})
        self.assertFalse(self.assignment_calls())

    def test_operator_login_casing_does_not_duplicate_or_prevent_removal(self):
        self.original["assignees"].append({"login": "Fixture"})
        self.operator_login = "FIXTURE"
        self.await_attestation()
        self.assertFalse(self.assignment_calls())
        self.complete_attestation()
        self.assertEqual(self.original["assignees"], [{"login": "maintainer"}])

    def test_prepare_records_current_operator_without_assigning(self):
        self.operator_login = "current-operator"
        self.assertEqual(self.prepare()["attestor"], "current-operator")
        self.assert_read_only_preparation()
        self.assertFalse(self.assignment_calls())
        for login in (None, "", "invalid/login"):
            with self.subTest(login=login):
                self.operator_login = login
                with self.assertRaisesRegex(ValueError, "authenticated operator"):
                    self.prepare()
        self.assertFalse(self.assignment_calls())

    def test_assignment_changes_require_api_confirmation_and_can_resume(self):
        for assigned, method in ((False, "POST"), (True, "DELETE")):
            with self.subTest(method=method):
                self.original["assignees"] = [{"login": "maintainer"}]
                if assigned:
                    self.original["assignees"].append({"login": "fixture"})
                action = self.complete_attestation if assigned else self.await_attestation
                self.failure = (method, "repos/example/project/issues/12/assignees")
                with self.assertRaisesRegex(RuntimeError, "fixture API failure"):
                    action()
                self.failure = None
                self.drop_assignment = True
                with self.assertRaisesRegex(ValueError, "assignment.*not confirmed"):
                    action()
                self.drop_assignment = False
                self.assertEqual(action()["awaiting_attestation"], not assigned)

    def test_assignment_readback_detects_revision_and_other_assignee_changes(self):
        original = copy.deepcopy(self.original)
        for mutation in (
            lambda pr: pr["head"].update(sha=self.first),
            lambda pr: pr.update(assignees=[{"login": "fixture"}]),
        ):
            with self.subTest(mutation=mutation):
                self.original = copy.deepcopy(original)
                self.mutate_on_assignment = mutation
                with self.assertRaisesRegex(ValueError, "changed|assignees"):
                    self.await_attestation()
                self.assertFalse(self.readiness_calls())

    def test_assignment_mutation_response_does_not_replace_a_fresh_readback(self):
        self.drop_assignment = True
        for awaiting in (True, False):
            with self.subTest(awaiting=awaiting):
                self.original["assignees"] = [{"login": "maintainer"}]
                if not awaiting:
                    self.original["assignees"].append({"login": "fixture"})

                def api(endpoint, method="GET", data=None):
                    response = self.api(endpoint, method, data)
                    if endpoint.endswith("/assignees"):
                        response["assignees"] = [{"login": "maintainer"}]
                        if awaiting:
                            response["assignees"].append({"login": "fixture"})
                    return response

                with mock.patch.object(self.helper, "github", side_effect=api):
                    with self.assertRaisesRegex(ValueError, "assignment.*not confirmed"):
                        (self.await_attestation if awaiting else self.complete_attestation)()

    def test_finalisation_removes_attestor_before_both_readiness_transitions(self):
        for publication in ("replacement", "replace"):
            with self.subTest(publication=publication):
                self.original.update(state="open", draft=True)
                self.original["assignees"] = [{"login": "maintainer"}, {"login": "fixture"}]
                self.original["head"]["sha"] = (
                    self.head if publication == "replacement" else self.receipt["head"])
                self.receipt["publication"] = publication
                self.replacement = None
                self.calls.clear()
                self.check_pages[0][0].update(status="IN_PROGRESS", conclusion=None)
                with self.assertRaisesRegex(ValueError, "required CI"):
                    self.finish()
                current = self.replacement if publication == "replacement" else self.original
                self.assertEqual(current["assignees"], [{"login": "maintainer"}])
                self.assertEqual(self.original["assignees"], [{"login": "maintainer"}])
                self.assertTrue(current["draft"])
                self.check_pages[0][0].update(status="COMPLETED", conclusion="SUCCESS")
                self.assertTrue(self.finish()["ready_for_review"])
                self.assertEqual(len(self.assignment_calls()), 1)
                self.assertLess(self.calls.index(self.assignment_calls()[0]),
                                self.calls.index(self.readiness_calls()[0]))

    def test_failed_signature_verification_never_removes_attestor(self):
        self.original["assignees"].append({"login": "fixture"})
        for oid in self.receipt["mapping"].values():
            with self.subTest(commit=oid):
                self.commits[oid]["verification"]["verified"] = False
                with self.assertRaisesRegex(ValueError, "not verified"):
                    self.finish()
                self.assertIn({"login": "fixture"}, self.original["assignees"])
                self.assertFalse(self.assignment_calls())
                self.commits[oid]["verification"]["verified"] = True

    def test_unverified_commit_reports_account_context_without_terminal_controls(self):
        for name in ("GitHub", "GitHub\x1b[2J"):
            with self.subTest(name=name):
                commit = copy.deepcopy(self.commits[self.receipt["head"]])
                commit["verification"].update(verified=False, reason="unknown_key")
                commit["committer"].update(name=name, email="noreply@github.com")
                with self.assertRaisesRegex(ValueError, "not verified") as failure:
                    self.helper.verify_github_commit(self.receipt["head"], commit, self.key)
                message = str(failure.exception)
                self.assertNotIn("\x1b", message)
                self.assertEqual(json.loads(message.split(": ", 1)[1]), {
                    "reason": "unknown_key",
                    "committer": {"name": name, "email": "noreply@github.com"},
                })

    def test_failed_unassignment_blocks_finalisation_and_readiness(self):
        self.original["assignees"].append({"login": "fixture"})
        self.drop_assignment = True
        with self.assertRaisesRegex(ValueError, "assignment.*not confirmed"):
            self.finish()
        self.assertFalse(self.readiness_calls())
        self.assertIsNone(self.replacement)
        self.drop_assignment = False
        self.assertTrue(self.finish()["ready_for_review"])
        self.assertEqual(self.replacement["assignees"], [{"login": "maintainer"}])

    def test_reassignment_during_ci_check_blocks_promotion(self):
        self.mutate_on_checks = lambda pr: pr["assignees"].append({"login": "fixture"})
        with self.assertRaisesRegex(ValueError, "attestor.*assigned"):
            self.finish()
        self.assertFalse(self.readiness_calls())
        self.assertTrue(self.replacement["draft"])

    def test_reassignment_during_promotion_is_not_reported_as_success(self):
        self.mutate_on_ready = lambda pr: pr["assignees"].append({"login": "fixture"})
        with self.assertRaisesRegex(ValueError, "attestor.*assigned"):
            self.finish()

    def test_assignment_cli_confirms_both_transitions_and_surfaces_failures(self):
        plan_file, receipt_file = self.root / "plan.json", self.root / "signed.json"
        plan_file.write_text(json.dumps(self.plan))
        receipt_file.write_text(json.dumps(self.receipt))
        for arguments, method, expected in (
            (["await-attestation", "--plan", str(plan_file)], "POST", True),
            (["complete-attestation", "--receipt", str(receipt_file),
              "--approve-plan", self.approve()], "DELETE", False),
        ):
            with self.subTest(action=arguments[0]), mock.patch.dict(
                    os.environ, {"CODESPACES": ""}), mock.patch.object(
                    sys, "argv", ["copilot-cs-endorse", *arguments]):
                self.failure = (method, "repos/example/project/issues/12/assignees")
                with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(self.helper.main(), 1)
                self.assertEqual(output.getvalue(), "")
                self.assertIn("fixture API failure", errors.getvalue())
                self.failure = None
                with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(self.helper.main(), 0)
                self.assertEqual(json.loads(output.getvalue())["awaiting_attestation"], expected)
                self.assertEqual(errors.getvalue(), "")
        self.assertTrue(self.original["draft"])
        self.assertEqual(self.original["assignees"], [{"login": "maintainer"}])

    def test_old_receipts_without_attestor_keep_existing_assignees(self):
        self.request["version"] = 1
        del self.request["attestor"]
        self.git("branch", "-m", "feature-signed", "prior-signed-fixture")
        self.receipt = self.sign()
        self.original["assignees"].append({"login": "fixture"})
        self.assertTrue(self.finish()["ready_for_review"])
        self.assertCountEqual(self.replacement["assignees"], self.original["assignees"])
        self.assertFalse(self.assignment_calls())

    def test_replacement_is_verified_linked_and_populated_before_original_closes(self):
        result = self.finish()
        self.assertEqual(result["url"], "https://github.com/example/project/pull/13")
        self.assertEqual(self.original["state"], "closed")
        self.assertFalse(self.replacement["draft"])
        self.assertTrue(result["ready_for_review"])
        for field in ("labels", "assignees", "milestone", "requested_reviewers", "requested_teams"):
            self.assertEqual(self.original[field], self.replacement[field])
        self.assertNotIn("\r", self.replacement["body"])
        self.assertNotRegex(self.replacement["body"], r"#\d+")
        self.assertIn("https://github.com/example/other/issues/23", self.replacement["body"])
        self.assertIn("https://github.com/example/project/pull/45", self.replacement["body"])
        self.assertIn(result["url"], self.comments[0]["body"])
        self.assertEqual(self.comments[0]["body"].count("Authored by Copilot, guided by @jidicula."), 1)
        closed = ("PATCH", "repos/example/project/pulls/12", {"state": "closed"})
        self.assertLess(self.calls.index(closed), self.calls.index(self.readiness_calls()[0]))
        self.assertEqual(self.readiness_calls()[0][2]["variables"]["id"], "PR_replacement")

    def test_completed_finish_is_idempotent(self):
        first = self.finish()
        self.calls.clear()
        self.assertEqual(self.finish(), first)
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.comments[0]["body"].count("Authored by Copilot, guided by @jidicula."), 1)
        self.assertFalse(self.readiness_calls())
        self.assertFalse(any(method == "POST" and endpoint != "graphql"
                             for method, endpoint, _ in self.calls))

    def test_existing_crlf_supersession_notice_is_not_duplicated(self):
        first = self.finish()
        self.comments[0]["body"] = self.comments[0]["body"].replace("\n", "\r\n")
        self.calls.clear()
        self.assertEqual(self.finish(), first)
        self.assertEqual(len(self.comments), 1)
        self.assertIn("\r\n", self.comments[0]["body"])
        self.assertFalse(any(method == "POST" and endpoint.endswith("/comments")
                             for method, endpoint, _ in self.calls))

    def test_existing_legacy_supersession_notice_is_not_duplicated(self):
        first = self.finish()
        self.comments[0]["body"] = self.comments[0]["body"].replace(
            "\n\nAuthored by Copilot, guided by @jidicula.\n\n", " ")
        self.calls.clear()
        self.assertEqual(self.finish(), first)
        self.assertEqual(len(self.comments), 1)
        self.assertFalse(any(method == "POST" and endpoint.endswith("/comments")
                             for method, endpoint, _ in self.calls))

    def draft_calls(self):
        return [call for call in self.calls if call[1] == "graphql"
                and "convertPullRequestToDraft" in call[2]["query"]]

    def test_readiness_triggered_failure_returns_the_unchanged_signed_pr_to_draft(self):
        for publication in ("replace", "replacement"):
            with self.subTest(publication=publication):
                self.receipt["publication"] = publication
                self.original.update(state="open", draft=True)
                self.original["head"]["sha"] = (
                    self.receipt["head"] if publication == "replace" else self.head)
                self.replacement = None
                self.check_pages = [[{
                    "__typename": "CheckRun", "name": "ci", "isRequired": True,
                    "status": "COMPLETED", "conclusion": "SKIPPED",
                }]]
                self.assertTrue(self.finish()["ready_for_review"])
                current = self.replacement or self.original
                self.check_pages.append([{
                    "__typename": "CheckRun", "name": "ci", "isRequired": True,
                    "status": "COMPLETED", "conclusion": "FAILURE",
                }])
                self.calls.clear()
                with mock.patch.object(self.repository, "sign",
                                       side_effect=AssertionError("must not sign")):
                    with self.assertRaisesRegex(ValueError, "returned.*draft"):
                        self.finish()
                self.assertTrue(current["draft"])
                self.assertEqual(current["head"]["sha"], self.receipt["head"])
                self.assertEqual(len(self.draft_calls()), 1)
                self.assertFalse(self.readiness_calls())
                with self.assertRaisesRegex(ValueError, "required CI"):
                    self.finish()
                self.assertEqual(len(self.draft_calls()), 1)

    def test_failure_visible_during_readiness_is_not_reported_as_success(self):
        self.mutate_on_ready = lambda _pr: self.check_pages[0][0].update(conclusion="FAILURE")
        with self.assertRaisesRegex(ValueError, "returned.*draft"):
            self.finish()
        self.assertTrue(self.replacement["draft"])
        self.assertEqual(len(self.draft_calls()), 1)

    def test_pending_readiness_triggered_ci_does_not_demote_or_complete_finalisation(self):
        self.mutate_on_ready = lambda _pr: self.check_pages[0][0].update(
            status="IN_PROGRESS", conclusion=None)
        with self.assertRaisesRegex(ValueError, "required CI.*IN_PROGRESS"):
            self.finish()
        self.assertFalse(self.replacement["draft"])
        self.assertFalse(self.draft_calls())
        self.mutate_on_ready = None
        self.check_pages[0][0].update(status="COMPLETED", conclusion="SUCCESS")
        self.calls.clear()
        self.assertTrue(self.finish()["ready_for_review"])
        self.assertFalse(self.readiness_calls())
        self.assertFalse(self.draft_calls())

    def test_ci_rollback_checks_for_concurrent_revision_changes(self):
        for change in (
            lambda pr: pr["head"].update(sha="0" * 40),
            lambda pr: pr["head"].update(ref="other-branch"),
            lambda pr: pr["base"].update(ref="other-base"),
            lambda pr: pr.update(state="closed"),
            self.conflicting_base_advance,
        ):
            with self.subTest(change=change):
                self.original.update(state="open")
                self.replacement = None
                self.live_base = self.base
                self.mergeable = "MERGEABLE"
                self.mutate_on_checks = None
                self.check_pages[0][0]["conclusion"] = "SUCCESS"
                self.finish()
                self.calls.clear()
                self.check_pages[0][0]["conclusion"] = "FAILURE"
                self.mutate_on_checks = change
                with self.assertRaisesRegex(ValueError, "changed|open"):
                    self.finish()
                self.assertFalse(self.draft_calls())

    def test_unreadable_or_incomplete_ci_never_causes_a_draft_rollback(self):
        self.finish()
        self.failure = ("POST", "graphql")
        with self.assertRaisesRegex(RuntimeError, "fixture API failure"):
            self.finish()
        self.failure = None
        for conclusion in (None, "UNKNOWN"):
            with self.subTest(conclusion=conclusion):
                self.check_pages[0][0]["conclusion"] = conclusion
                with self.assertRaisesRegex(ValueError, "required CI"):
                    self.finish()
                self.assertFalse(self.replacement["draft"])
                self.assertFalse(self.draft_calls())

    def test_ci_rollback_must_be_confirmed_and_can_resume_without_republication(self):
        self.finish()
        self.check_pages[0][0]["conclusion"] = "FAILURE"
        self.draft_failure = True
        with self.assertRaisesRegex(RuntimeError, "draft rollback failure"):
            self.finish()
        self.assertFalse(self.replacement["draft"])
        self.draft_failure = False
        self.drop_draft = True
        with self.assertRaisesRegex(ValueError, "draft.*not confirmed"):
            self.finish()
        self.assertFalse(self.replacement["draft"])
        self.drop_draft = False
        with self.assertRaisesRegex(ValueError, "returned.*draft"):
            self.finish()
        self.assertTrue(self.replacement["draft"])
        self.assertEqual(len(self.comments), 1)

    def test_commit_status_failure_after_readiness_returns_the_pr_to_draft(self):
        self.finish()
        self.check_pages = [[{
            "__typename": "StatusContext", "context": "ci", "isRequired": True,
            "state": "ERROR",
        }]]
        with self.assertRaisesRegex(ValueError, "returned.*draft"):
            self.finish()
        self.assertTrue(self.replacement["draft"])

    def test_required_workflow_failure_after_readiness_returns_the_pr_to_draft(self):
        self.finish()
        self.check_pages[0][0].update(status="IN_PROGRESS", conclusion=None)
        run = self.add_required_workflow()
        run.update(head_sha=self.receipt["head"], head_branch=self.replacement["head"]["ref"],
                   conclusion="failure")
        run["pull_requests"] = [{
            "number": 13, "head": {"sha": self.receipt["head"]}, "base": {"ref": "main"},
        }]
        with self.assertRaisesRegex(ValueError, "returned.*draft"):
            self.finish()
        self.assertTrue(self.replacement["draft"])

    def test_api_failures_keep_original_open_and_can_resume(self):
        for failure in (
            ("POST", "repos/example/project/pulls"),
            ("PATCH", "repos/example/project/issues/13"),
            ("POST", "repos/example/project/pulls/13/requested_reviewers"),
            ("POST", "repos/example/project/issues/12/comments"),
            ("PATCH", "repos/example/project/pulls/12"),
        ):
            with self.subTest(failure=failure):
                self.failure = failure
                with self.assertRaisesRegex(RuntimeError, "fixture API failure"):
                    self.finish()
                self.assertEqual(self.original["state"], "open")
        self.failure = None
        self.finish()
        self.assertEqual(self.original["state"], "closed")
        self.assertEqual(len(self.comments), 1)

    def test_original_or_replacement_changes_prevent_closure(self):
        for target, field in (("original", "head"), ("replacement", "head"),
                              ("original", "base"), ("replacement", "base")):
            with self.subTest(target=target, field=field):
                self.comments.clear()
                self.original["head"]["sha"] = self.head
                self.original["base"]["ref"] = "main"
                if self.replacement:
                    self.replacement["head"]["sha"] = self.receipt["head"]
                    self.replacement["base"]["ref"] = "main"
                change = {"ref": "other-base"} if field == "base" else {"sha": "0" * 40}
                self.mutate_on_comment = lambda: getattr(self, target)[field].update(change)
                with self.assertRaisesRegex(ValueError, "changed"):
                    self.finish()
                self.assertEqual(self.original["state"], "open")

    def test_existing_unrelated_replacement_draft_is_not_adopted(self):
        self.replacement = copy.deepcopy(self.original)
        with self.assertRaisesRegex(ValueError, "not this endorsement"):
            self.finish()
        self.assertEqual(self.original["state"], "open")

    def test_silently_dropped_metadata_prevents_closure(self):
        self.drop_metadata = True
        with self.assertRaisesRegex(ValueError, "metadata differs"):
            self.finish()
        self.assertEqual(self.original["state"], "open")
        self.assertEqual(self.comments, [])

    def test_original_metadata_change_prevents_closure(self):
        self.mutate_on_comment = lambda: self.original.update(body="New context during publication")
        with self.assertRaisesRegex(ValueError, "original PR changed"):
            self.finish()
        self.assertEqual(self.original["state"], "open")

    def test_in_place_finish_does_not_create_or_close_prs(self):
        self.receipt["publication"] = "replace"
        self.original["head"]["sha"] = self.receipt["head"]
        result = self.finish()
        self.assertEqual(result["url"], self.original["html_url"])
        self.assertEqual(self.original["state"], "open")
        self.assertIsNone(self.replacement)
        self.assertFalse(self.original["draft"])
        self.assertTrue(result["ready_for_review"])
        self.assertEqual(self.readiness_calls()[0][2]["variables"]["id"], "PR_original")
        self.assertTrue(all(method == "GET" or method == "GET-PAGES" or endpoint == "graphql"
                            for method, endpoint, _ in self.calls))

    def test_replacement_finish_accepts_historical_pr_base_snapshots(self):
        self.original["base"]["sha"] = "f" * 40
        result = self.finish()
        self.assertEqual(result["url"], "https://github.com/example/project/pull/13")
        self.assertEqual(self.replacement["base"]["sha"], "f" * 40)
        self.assertEqual(self.original["state"], "closed")

    def test_in_place_finish_accepts_historical_pr_base_snapshot(self):
        self.receipt["publication"] = "replace"
        self.original["head"]["sha"] = self.receipt["head"]
        self.original["base"]["sha"] = "f" * 40
        result = self.finish()
        self.assertEqual(result["url"], self.original["html_url"])
        self.assertTrue(result["ready_for_review"])

    def prepare_base_merge(self, *, count=1, trailing_signature_newline=False):
        merge = self.base_merge_follow_up(
            self.receipt, count=count, trailing_signature_newline=trailing_signature_newline,
        )
        self.original["head"].update(ref=self.request["source_branch"], sha=self.request["head"])
        self.live_base = self.request["base"]
        self.commits.update({oid: self.commit_data(oid) for oid in
                             self.repository.commits(self.request)})
        with mock.patch.object(self.helper, "replacement_allowed", return_value=True):
            request = self.helper.prepare(
                "example/project", 12, str(self.key_file) + ".pub",
                preserve_base_merges=[merge],
            )
        self.assertEqual(request, self.request)
        return merge

    def test_prepare_explicitly_records_only_verified_platform_base_merges(self):
        merge = self.prepare_base_merge()
        self.assertEqual(self.request["version"], 3)
        self.assertEqual(self.request["preserve_base_merges"], [merge])
        self.assert_read_only_preparation()

    def test_github_signature_empty_continuation_preserves_the_exact_merge(self):
        merge = self.prepare_base_merge(trailing_signature_newline=True)
        original = self.repository.git("cat-file", "commit", merge)
        self.assertIn(b"\n \n\n", original)
        with mock.patch.object(self.helper, "run", wraps=self.helper.run) as commands:
            self.receipt = self.sign("replace")
        self.assertEqual(sum(call.args[0][:3] == ["ssh-keygen", "-Y", "sign"]
                             for call in commands.call_args_list), 1)
        self.original["head"]["sha"] = self.receipt["head"]
        self.commits.update({oid: self.commit_data(oid) for oid in self.receipt["mapping"].values()})
        self.assertTrue(self.finish()["ready_for_review"])
        self.assertEqual(self.receipt["mapping"][merge], merge)
        self.assertEqual(self.repository.git("cat-file", "commit", merge), original)

    def test_base_merge_may_precede_a_clean_recorded_base_advance(self):
        merge = self.prepare_base_merge()
        self.live_base = self.advance_base()
        with mock.patch.object(self.helper, "replacement_allowed", return_value=True):
            self.request = self.helper.prepare(
                "example/project", 12, str(self.key_file) + ".pub",
                preserve_base_merges=[merge],
            )
        self.assertEqual(self.request["base"], self.live_base)
        self.receipt = self.sign("replace")
        self.original["head"]["sha"] = self.receipt["head"]
        self.commits.update({oid: self.commit_data(oid) for oid in self.receipt["mapping"].values()})
        self.assertTrue(self.finish()["ready_for_review"])
        self.assertEqual(self.receipt["mapping"][merge], merge)

    def test_base_merge_cannot_claim_unconfirmed_github_base_ancestry(self):
        merge = self.prepare_base_merge()
        self.live_base = self.advance_base()
        self.base_relation = "diverged"
        with mock.patch.object(self.helper, "replacement_allowed", return_value=True):
            with self.assertRaisesRegex(ValueError, "base.merge.*ancestor"):
                self.helper.prepare(
                    "example/project", 12, str(self.key_file) + ".pub",
                    preserve_base_merges=[merge],
                )
        self.assert_read_only_preparation()

    def test_base_merge_preparation_rejects_unverified_or_changed_api_objects(self):
        merge = self.prepare_base_merge()
        original = copy.deepcopy(self.commits[merge])
        for field, value in (("verified", False), ("reason", "bad_email"),
                             ("signature", None), ("payload", "different payload")):
            with self.subTest(field=field):
                self.commits[merge] = copy.deepcopy(original)
                self.commits[merge]["verification"][field] = value
                with self.assertRaises((ValueError, RuntimeError)):
                    self.helper.prepare(
                        "example/project", 12, str(self.key_file) + ".pub",
                        preserve_base_merges=[merge],
                    )
                self.assert_read_only_preparation()

    def test_base_merge_preparation_requires_the_default_branch(self):
        merge = self.prepare_base_merge()
        original_api = self.api

        def api(endpoint, method="GET", data=None):
            if endpoint == "repos/example/project":
                return {"default_branch": "other"}
            return original_api(endpoint, method, data)

        with mock.patch.object(self.helper, "github", side_effect=api):
            with self.assertRaisesRegex(ValueError, "default branch"):
                self.helper.prepare(
                    "example/project", 12, str(self.key_file) + ".pub",
                    preserve_base_merges=[merge],
                )

    def test_finish_preserves_platform_merge_and_still_requires_signed_head_ci(self):
        merge = self.prepare_base_merge()
        self.freeze()
        self.assertTrue(self.await_attestation()["awaiting_attestation"])
        self.receipt = self.sign("replace")
        self.original["head"]["sha"] = self.receipt["head"]
        self.commits.update({oid: self.commit_data(oid) for oid in self.receipt["mapping"].values()})
        self.check_pages[0][0].update(status="IN_PROGRESS", conclusion=None)
        with self.assertRaisesRegex(ValueError, "quality gate blocked"):
            self.finish()
        self.assertTrue(self.original["draft"])
        self.check_pages[0][0].update(status="COMPLETED", conclusion="SUCCESS")
        self.assertTrue(self.finish()["ready_for_review"])
        self.assertEqual(self.receipt["mapping"][merge], merge)
        self.assertEqual(self.commits[merge]["committer"]["name"], "GitHub")

    def test_finish_rechecks_preserved_platform_verification_before_readiness(self):
        merge = self.prepare_base_merge()
        self.receipt = self.sign("replace")
        self.original["head"]["sha"] = self.receipt["head"]
        self.commits.update({oid: self.commit_data(oid) for oid in self.receipt["mapping"].values()})
        self.commits[merge]["verification"]["verified"] = False
        with self.assertRaisesRegex(ValueError, "GitHub"):
            self.finish()
        self.assertTrue(self.original["draft"])
        self.assertEqual(self.readiness_calls(), [])

    def test_base_merge_prepare_cli_keeps_the_explicit_scope(self):
        merge = self.prepare_base_merge()
        args = ["copilot-cs-endorse", "prepare", "--repo", "example/project", "--pr", "12",
                "--key", str(self.key_file) + ".pub", "--preserve-base-merge", merge]
        with mock.patch.dict(os.environ, {"CODESPACES": ""}), mock.patch.object(
                sys, "argv", args), mock.patch.object(
                self.helper, "replacement_allowed", return_value=True), redirect_stdout(
                io.StringIO()) as output:
            self.assertEqual(self.helper.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["preserve_base_merges"], [merge])

    def test_incremental_finish_preserves_prior_committer_and_requires_signed_head_ci(self):
        previous = self.receipt
        self.follow_up(previous)
        self.request["committer"] = {"name": "New Endorser", "email": "new@example.invalid"}
        self.freeze()
        self.original["head"].update(ref=self.request["source_branch"], sha=self.request["head"])
        self.assertTrue(self.await_attestation()["awaiting_attestation"])
        self.receipt = self.sign("replace")
        self.original["head"].update(ref=self.request["source_branch"], sha=self.receipt["head"])
        self.commits = {oid: self.commit_data(oid) for oid in
                        [*self.receipt["mapping"], *self.receipt["mapping"].values()]}
        self.check_pages[0][0].update(status="IN_PROGRESS", conclusion=None)
        with self.assertRaisesRegex(ValueError, "quality gate blocked"):
            self.finish()
        self.assertTrue(self.original["draft"])
        self.check_pages[0][0].update(status="COMPLETED", conclusion="SUCCESS")
        self.assertTrue(self.finish()["ready_for_review"])
        for oid in self.plan["preserved_commits"]:
            self.assertEqual(self.receipt["mapping"][oid], oid)
            self.assertEqual(self.commits[oid]["committer"]["name"], "Fixture Author")

    def test_finish_verifies_the_explicit_committer_without_accepting_other_metadata_changes(self):
        self.git("branch", "-m", "feature-signed", "previous-signed-fixture")
        self.request["committer"] = {"name": "Human Endorser", "email": "endorser@example.invalid"}
        self.receipt = self.sign("replace")
        self.original["head"]["sha"] = self.receipt["head"]
        self.commits = {oid: self.commit_data(oid) for oid in
                        [*self.receipt["mapping"], *self.receipt["mapping"].values()]}
        result = self.finish()
        self.assertEqual(result["head"], self.receipt["head"])
        after = self.commits[self.receipt["head"]]
        self.assertEqual(after["committer"]["name"], "Human Endorser")
        self.assertEqual(after["committer"]["email"], "endorser@example.invalid")
        after["committer"]["date"] = "2026-01-01T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "published commit differs"):
            self.finish()

    def test_finish_rejects_unapproved_committer_identity_changes(self):
        self.commits[self.receipt["head"]]["committer"]["email"] = "other@example.invalid"
        with self.assertRaisesRegex(ValueError, "published commit differs"):
            self.finish()

    def test_required_checks_gate_both_publication_methods(self):
        for publication in ("replacement", "replace"):
            for status, conclusion in (
                ("QUEUED", None), ("IN_PROGRESS", None), ("COMPLETED", "FAILURE"),
                ("COMPLETED", "CANCELLED"), ("COMPLETED", "TIMED_OUT"),
                ("COMPLETED", "ACTION_REQUIRED"), ("COMPLETED", "STALE"),
                ("COMPLETED", None), ("COMPLETED", "UNRECOGNISED"),
            ):
                with self.subTest(publication=publication, status=status, conclusion=conclusion):
                    self.original.update(state="open", draft=True)
                    self.replacement = None
                    self.calls.clear()
                    self.receipt["publication"] = publication
                    self.original["head"]["sha"] = (
                        self.receipt["head"] if publication == "replace" else self.head)
                    self.check_pages[0][0].update(status=status, conclusion=conclusion)
                    with self.assertRaisesRegex(ValueError, "required CI.*ci"):
                        self.finish()
                    current = self.original if publication == "replace" else self.replacement
                    self.assertTrue(current["draft"])
                    self.assertFalse(self.readiness_calls())

    def test_github_success_conclusions_allow_readiness(self):
        self.receipt["publication"] = "replace"
        self.original["head"]["sha"] = self.receipt["head"]
        for conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            with self.subTest(conclusion=conclusion):
                self.original["draft"] = True
                self.check_pages[0][0]["conclusion"] = conclusion
                self.assertTrue(self.finish()["ready_for_review"])
                self.assertFalse(self.original["draft"])

    def test_optional_failures_do_not_block_required_checks(self):
        self.check_pages[0].append({
            "__typename": "CheckRun", "name": "optional", "isRequired": False,
            "status": "COMPLETED", "conclusion": "FAILURE",
        })
        self.assertTrue(self.finish()["ready_for_review"])

    def test_required_commit_statuses_must_succeed(self):
        self.check_pages = [[{
            "__typename": "StatusContext", "context": "ci", "isRequired": True,
            "state": "PENDING",
        }]]
        for state in ("PENDING", "EXPECTED", "FAILURE", "ERROR"):
            with self.subTest(state=state):
                self.check_pages[0][0]["state"] = state
                with self.assertRaisesRegex(ValueError, "required CI"):
                    self.finish()
                self.assertTrue(self.replacement["draft"])
        self.check_pages[0][0]["state"] = "SUCCESS"
        self.assertTrue(self.finish()["ready_for_review"])

    def test_no_reported_required_checks_keeps_the_signed_pr_in_draft(self):
        self.required_contexts = []
        for nodes in ([], [{
            "__typename": "CheckRun", "name": "optional", "isRequired": False,
            "status": "COMPLETED", "conclusion": "SUCCESS",
        }]):
            with self.subTest(nodes=nodes):
                self.check_pages = [nodes]
                with self.assertRaisesRegex(ValueError, "no required CI checks"):
                    self.finish()
                self.assertTrue(self.replacement["draft"])
                self.assertFalse(self.readiness_calls())

    def test_missing_classic_or_ruleset_required_checks_prevent_readiness(self):
        for ruleset in (False, True):
            with self.subTest(ruleset=ruleset):
                self.required_contexts = ["ci"] if ruleset else ["ci", "not-started"]
                self.rules = ([{
                    "type": "required_status_checks",
                    "parameters": {"required_status_checks": [
                        {"context": "not-started", "integration_id": 123},
                    ]},
                }] if ruleset else [])
                with self.assertRaisesRegex(ValueError, "missing required CI.*not-started"):
                    self.finish()
                self.assertTrue(self.replacement["draft"])
                self.assertFalse(self.readiness_calls())

    def test_required_checks_are_paginated_before_promotion(self):
        self.check_pages.append([{
            "__typename": "CheckRun", "name": "later-page", "isRequired": True,
            "status": "COMPLETED", "conclusion": "FAILURE",
        }])
        with self.assertRaisesRegex(ValueError, "required CI.*later-page"):
            self.finish()
        self.assertFalse(self.readiness_calls())
        queries = [call[2]["variables"]["cursor"] for call in self.calls
                   if call[1] == "graphql"]
        self.assertEqual(queries, [None, "1"])
        self.check_pages[1][0]["conclusion"] = "SUCCESS"
        self.assertTrue(self.finish()["ready_for_review"])

    def test_checks_for_the_unsigned_head_cannot_promote_the_signed_pr(self):
        self.assertEqual(self.prepare()["head"], self.head)
        self.check_head = self.head
        with self.assertRaisesRegex(ValueError, "CI.*expected PR head"):
            self.finish()
        self.assertFalse(self.readiness_calls())
        self.assertTrue(self.replacement["draft"])

    def test_changed_revision_or_closed_pr_during_ci_prevents_promotion(self):
        for change in (
            lambda pr: pr["head"].update(sha="0" * 40),
            lambda pr: pr["head"].update(ref="another-branch"),
            lambda pr: pr["base"].update(ref="another-base"),
            lambda pr: pr.update(state="closed"),
            self.conflicting_base_advance,
            lambda pr: setattr(self, "published_head", self.first),
        ):
            with self.subTest(change=change):
                self.original.update(state="open")
                self.live_base = self.base
                self.mergeable = "MERGEABLE"
                self.published_head = None
                self.replacement = None
                self.calls.clear()
                self.mutate_on_checks = change
                with self.assertRaisesRegex(ValueError, "changed|open"):
                    self.finish()
                self.assertFalse(self.readiness_calls())

    def test_readiness_api_failure_resumes_without_another_pr_or_signature(self):
        self.ready_failure = True
        with self.assertRaisesRegex(RuntimeError, "readiness failure"):
            self.finish()
        self.assertEqual(self.original["state"], "closed")
        self.assertTrue(self.replacement["draft"])
        self.ready_failure = False
        with mock.patch.object(self.repository, "sign", side_effect=AssertionError("must not sign")):
            self.assertTrue(self.finish()["ready_for_review"])
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(sum(call[:2] == ("POST", "repos/example/project/pulls")
                             for call in self.calls), 1)

    def test_promotion_must_be_confirmed_on_the_same_signed_revision(self):
        self.drop_ready = True
        with self.assertRaisesRegex(ValueError, "ready.*not confirmed"):
            self.finish()
        self.assertTrue(self.replacement["draft"])
        self.drop_ready = False
        self.mutate_on_ready = lambda pr: pr["head"].update(sha="0" * 40)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.finish()

    def test_in_place_ready_completion_is_idempotent_but_preparation_stays_draft_only(self):
        self.receipt["publication"] = "replace"
        self.original["head"]["sha"] = self.receipt["head"]
        result = self.finish()
        self.calls.clear()
        self.assertEqual(self.finish(), result)
        self.assertFalse(self.readiness_calls())
        with self.assertRaisesRegex(ValueError, "draft"):
            self.helper.prepare("example/project", 12, str(self.key_file) + ".pub")

    def test_unreadable_ci_or_policy_never_falls_back_to_readiness(self):
        self.failure = ("POST", "graphql")
        with self.assertRaisesRegex(RuntimeError, "fixture API failure"):
            self.finish()
        self.assertTrue(self.replacement["draft"])
        self.assertFalse(self.readiness_calls())
        self.failure = None
        with mock.patch.object(self.helper, "github_pages", side_effect=RuntimeError("policy unavailable")):
            with self.assertRaisesRegex(RuntimeError, "policy unavailable"):
                self.finish()
        self.assertTrue(self.replacement["draft"])
        self.assertFalse(self.readiness_calls())

    def test_partial_graphql_errors_or_missing_rollups_prevent_readiness(self):
        for invalid in (
            lambda response: response.update(errors=[{"message": "permission denied"}]),
            lambda response: response.update(data=None),
            lambda response: response["data"].update(node=None),
            lambda response: response["data"]["node"]["commits"]["nodes"][0]["commit"].update(
                statusCheckRollup=None),
            lambda response: response["data"]["node"]["commits"]["nodes"][0]["commit"][
                "statusCheckRollup"]["contexts"].update(nodes=None),
        ):
            with self.subTest(invalid=invalid):
                def api(endpoint, method="GET", data=None):
                    response = self.api(endpoint, method, data)
                    if endpoint == "graphql":
                        invalid(response)
                    return response
                with mock.patch.object(self.helper, "github", side_effect=api):
                    with self.assertRaises(ValueError):
                        self.finish()
                self.assertTrue(self.replacement["draft"])
                self.assertFalse(self.readiness_calls())

    def test_unverifiable_workflow_or_malformed_rules_keep_the_draft(self):
        for rule in (
            {"type": "workflows", "parameters": {"workflows": []}},
            {"type": "required_status_checks", "parameters": None},
            {"type": "required_status_checks",
             "parameters": {"required_status_checks": [{"context": ""}]}},
        ):
            with self.subTest(rule=rule):
                self.rules = [rule]
                with self.assertRaises(ValueError):
                    self.finish()
                self.assertTrue(self.replacement["draft"])
                self.assertFalse(self.readiness_calls())

    def test_ruleset_checks_work_without_classic_protection(self):
        self.rules = [{
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "ci", "integration_id": 123}]},
        }]
        def api(endpoint, method="GET", data=None):
            response = self.api(endpoint, method, data)
            if endpoint == "graphql" and "EndorsementRequiredChecks" in data["query"]:
                response["data"]["node"]["baseRef"]["branchProtectionRule"] = None
            return response
        with mock.patch.object(self.helper, "github", side_effect=api):
            self.assertTrue(self.finish()["ready_for_review"])

    def test_finish_cli_reports_pending_then_resumes_the_same_receipt(self):
        receipt = self.root / "published.json"
        receipt.write_text(json.dumps(self.receipt))
        args = ["copilot-cs-endorse", "finish", "--receipt", str(receipt),
                "--approve-plan", self.approve()]
        self.check_pages[0][0].update(status="IN_PROGRESS", conclusion=None)
        with mock.patch.dict(os.environ, {"CODESPACES": ""}), mock.patch.object(sys, "argv", args):
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(self.helper.main(), 1)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("https://github.com/example/project/pull/13", errors.getvalue())
            self.assertIn("ci: IN_PROGRESS", errors.getvalue())
            self.assertTrue(self.replacement["draft"])
            self.check_pages[0][0].update(status="COMPLETED", conclusion="SUCCESS")
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.helper.main(), 0)
            self.assertEqual(json.loads(output.getvalue()), {
                "url": self.replacement["html_url"], "head": self.receipt["head"],
                "replaces": self.request["url"], "ready_for_review": True,
                "ci_scope": "observed_required_checks",
            })

    def test_conflicting_base_advance_blocks_finalization_before_metadata_mutation(self):
        self.conflicting_base_advance()
        with self.assertRaisesRegex(ValueError, "base branch changed"):
            self.finish()
        self.assertIsNone(self.replacement)
        self.assertEqual(self.original["state"], "open")
        self.assert_read_only_calls()

    def test_conflicting_base_advance_during_signature_verification_blocks_both_publications(self):
        verify = self.helper.verify_signature

        def change_base(*arguments):
            verify(*arguments)
            self.conflicting_base_advance()

        for publication in ("replacement", "replace"):
            with self.subTest(publication=publication):
                self.live_base = self.base
                self.mergeable = "MERGEABLE"
                self.calls.clear()
                self.receipt["publication"] = publication
                self.original["head"]["sha"] = (
                    self.receipt["head"] if publication == "replace" else self.head)
                with mock.patch.object(self.helper, "verify_signature", side_effect=change_base):
                    with self.assertRaisesRegex(ValueError, "base branch changed"):
                        self.finish()
                self.assertIsNone(self.replacement)
                self.assertEqual(self.original["state"], "open")
                self.assert_read_only_calls()

    def test_conflicting_base_advance_after_linking_keeps_original_open(self):
        self.mutate_on_comment = self.conflicting_base_advance
        with self.assertRaisesRegex(ValueError, "base branch changed"):
            self.finish()
        self.assertEqual(self.original["state"], "open")
        self.assertEqual(self.replacement["state"], "open")

    def test_clean_base_advance_preserves_the_attestation_plan(self):
        frozen = copy.deepcopy(self.plan)
        self.live_base = self.first
        self.assertTrue(self.helper.await_attestation(self.plan)["awaiting_attestation"])
        self.assertFalse(self.helper.complete_attestation(
            self.receipt, self.approve())["awaiting_attestation"])
        self.assertEqual(self.plan, frozen)
        self.assertEqual(self.plan["request"]["base"], self.base)
        self.assertEqual(self.plan["commits"], [self.first, self.head])

    def test_unknown_mergeability_pauses_and_resumes_the_same_plan(self):
        self.live_base = self.first
        frozen = copy.deepcopy(self.plan)
        for mergeable in ("UNKNOWN", None, False, "unexpected"):
            with self.subTest(mergeable=mergeable):
                self.mergeable = mergeable
                with self.assertRaisesRegex(ValueError, "mergeability is unconfirmed.*same plan"):
                    self.helper.await_attestation(self.plan)
                self.assertFalse(self.assignment_calls())
        self.mergeable = "MERGEABLE"
        self.assertTrue(self.helper.await_attestation(self.plan)["awaiting_attestation"])
        self.assertEqual(self.plan, frozen)

    def test_base_rewind_or_rewrite_cannot_reuse_attestation(self):
        self.live_base = self.first
        for relation in ("behind", "diverged", "identical"):
            with self.subTest(relation=relation):
                self.base_relation = relation
                with self.assertRaisesRegex(ValueError, "without a confirmed fast-forward"):
                    self.helper.await_attestation(self.plan)
                self.assertFalse(self.assignment_calls())

    def test_incomplete_base_comparison_pauses_and_resumes_the_same_plan(self):
        self.live_base = self.first
        frozen = copy.deepcopy(self.plan)
        comparison = {"status": "ahead", "base_commit": {"sha": self.base},
                      "merge_base_commit": {"sha": self.base}}
        for response in (
            None, [], {},
            dict(comparison, status=None),
            dict(comparison, status="unknown"),
            dict(comparison, base_commit={"sha": self.head}),
            dict(comparison, merge_base_commit={}),
            dict(comparison, merge_base_commit={"sha": "invalid"}),
        ):
            with self.subTest(response=response):
                def incomplete(endpoint, method="GET", data=None):
                    result = self.api(endpoint, method, data)
                    return response if "/compare/" in endpoint else result

                with mock.patch.object(self.helper, "github", side_effect=incomplete):
                    with self.assertRaisesRegex(ValueError, "ancestry is unconfirmed.*same plan"):
                        self.helper.await_attestation(self.plan)
                self.assertFalse(self.assignment_calls())
                self.assertEqual(self.plan, frozen)
        self.assertTrue(self.helper.await_attestation(self.plan)["awaiting_attestation"])
        self.assertEqual(self.plan, frozen)

    def test_incomplete_or_mismatched_mergeability_cannot_assign_the_attestor(self):
        self.live_base = self.first
        for change in (
            lambda response: response.update(errors=[{"message": "unavailable"}]),
            lambda response: response.update(data=None),
            lambda response: response["data"].update(node=None),
            lambda response: response["data"]["node"].update(id="PR_other"),
            lambda response: response["data"]["node"].update(headRefOid=self.first),
            lambda response: response["data"]["node"].update(headRefName="other"),
            lambda response: response["data"]["node"].update(baseRefName="other"),
            lambda response: response["data"]["node"].update(baseRef=None),
            lambda response: response["data"]["node"]["baseRef"].update(target={}),
            lambda response: response["data"]["node"].update(state="CLOSED"),
        ):
            with self.subTest(change=change):
                def corrupt(endpoint, method="GET", data=None):
                    response = self.api(endpoint, method, data)
                    if endpoint == "graphql" and "EndorsementBaseCompatibility" in data["query"]:
                        change(response)
                    return response

                with mock.patch.object(self.helper, "github", side_effect=corrupt):
                    with self.assertRaises(ValueError):
                        self.helper.await_attestation(self.plan)
                self.assertFalse(self.assignment_calls())

    def test_base_advance_during_github_mergeability_rechecks_the_same_plan(self):
        self.live_base = self.first
        frozen = copy.deepcopy(self.plan)
        advanced = False

        def advance_after_comparison(endpoint, method="GET", data=None):
            nonlocal advanced
            response = self.api(endpoint, method, data)
            if "/compare/" in endpoint and not advanced:
                advanced = True
                self.live_base = self.head
            return response

        with mock.patch.object(self.helper, "github", side_effect=advance_after_comparison):
            self.helper.confirm_ci_revision(self.request, 12, self.head, "feature")
        comparisons = [endpoint for _, endpoint, _ in self.calls if "/compare/" in endpoint]
        self.assertEqual(len(comparisons), 2)
        self.assertEqual(self.plan, frozen)
        self.assert_read_only_preparation()

    def test_clean_base_advance_during_in_place_verification_preserves_endorsement(self):
        self.receipt["publication"] = "replace"
        self.original["head"]["sha"] = self.receipt["head"]
        original = self.helper.verify_signature

        def advance(*arguments):
            original(*arguments)
            self.live_base = self.first

        with mock.patch.object(self.helper, "verify_signature", side_effect=advance):
            self.assertTrue(self.finish()["ready_for_review"])
        self.assertEqual(self.receipt["plan"]["request"]["base"], self.base)
        self.assertIsNone(self.replacement)

    def test_replacement_resumes_after_a_clean_base_advance_with_original_closed(self):
        self.check_pages[0][0].update(status="IN_PROGRESS", conclusion=None)
        with self.assertRaisesRegex(ValueError, "required CI"):
            self.finish()
        self.assertEqual(self.original["state"], "closed")
        self.assertTrue(self.replacement["draft"])
        self.live_base = self.first
        self.check_pages[0][0].update(status="COMPLETED", conclusion="SUCCESS")
        self.calls.clear()
        self.assertTrue(self.finish()["ready_for_review"])
        self.assertEqual(self.receipt["plan"]["request"]["base"], self.base)
        self.assertFalse(any(method == "POST" and endpoint.endswith("/pulls")
                             for method, endpoint, _ in self.calls))
        merge_queries = [data for _, endpoint, data in self.calls
                         if endpoint == "graphql" and "EndorsementBaseCompatibility" in data["query"]]
        self.assertTrue(merge_queries)
        self.assertTrue(all(data["variables"]["id"] == "PR_replacement" for data in merge_queries))

    def test_clean_base_advance_between_ci_pages_keeps_the_requested_revision(self):
        self.check_pages.append([])
        self.mutate_on_checks = lambda pr: setattr(self, "live_base", self.first)
        self.assertEqual(self.prepare(), self.request)
        self.assert_read_only_preparation()

    def test_a_verified_badge_alone_is_not_an_endorsement(self):
        after = self.commits[self.receipt["head"]]
        after["verification"]["signature"] = self.commits[self.receipt["mapping"][self.first]]["verification"]["signature"]
        with self.assertRaises(RuntimeError):
            self.finish()
        self.assertEqual(self.original["state"], "open")
        self.assertIsNone(self.replacement)

    def test_github_must_report_the_expected_signature_payload_and_metadata(self):
        after = self.commits[self.receipt["head"]]
        after["tree"]["sha"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "published commit differs"):
            self.finish()
        self.assertIsNone(self.replacement)

    def test_check_ci_does_not_begin_endorsement(self):
        original = copy.deepcopy(self.original)
        self.add_required_workflow()
        with mock.patch.object(Path, "read_text") as read_key, mock.patch.object(
                self.helper, "replacement_allowed") as policy, mock.patch.object(
                self.helper, "authenticated_operator") as operator:
            result = self.helper.check_ci("example/project", 12)
        self.assertEqual(result, {
            "repository": "example/project", "pr": 12, "url": self.original["html_url"],
            "head": self.head, "base": self.base,
            "source_branch": "feature", "base_branch": "main",
        })
        read_key.assert_not_called()
        policy.assert_not_called()
        operator.assert_not_called()
        self.assertEqual(self.original, original)
        self.assert_read_only_preparation()

    def test_check_ci_cli_reports_success_only_after_the_quality_gate_passes(self):
        args = ["copilot-cs-endorse", "check-ci", "--repo", "example/project", "--pr", "12"]
        with mock.patch.dict(os.environ, {"CODESPACES": ""}), mock.patch.object(
                sys, "argv", args), mock.patch.object(self.helper, "prepare") as prepare:
            for state in ("pending", "absent", "passing"):
                with self.subTest(state=state):
                    self.required_contexts = [] if state == "absent" else ["ci"]
                    self.check_pages = [[]] if state == "absent" else [[{
                        "__typename": "CheckRun", "name": "ci", "isRequired": True,
                        "status": "IN_PROGRESS" if state == "pending" else "COMPLETED",
                        "conclusion": None if state == "pending" else "SUCCESS",
                    }]]
                    with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                        status = self.helper.main()
                    if state == "passing":
                        self.assertEqual(status, 0)
                        self.assertEqual(json.loads(output.getvalue()), {
                            "repository": "example/project", "pr": 12,
                            "url": self.original["html_url"], "head": self.head, "base": self.base,
                            "source_branch": "feature", "base_branch": "main",
                            "required_ci": "passed",
                        })
                        self.assertEqual(errors.getvalue(), "")
                    else:
                        self.assertEqual(status, 1)
                        self.assertEqual(output.getvalue(), "")
                        self.assertIn("quality gate blocked", errors.getvalue())
                    self.assert_read_only_preparation()
            prepare.assert_not_called()

    def test_check_ci_cli_must_use_local_github_authentication(self):
        args = ["copilot-cs-endorse", "check-ci", "--repo", "example/project", "--pr", "12"]
        with mock.patch.dict(os.environ, {"CODESPACES": "true"}), mock.patch.object(
                sys, "argv", args), redirect_stdout(io.StringIO()) as output, redirect_stderr(
                io.StringIO()) as errors:
            self.assertEqual(self.helper.main(), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("locally", errors.getvalue())
        self.assertEqual(self.calls, [])

    def test_prepare_blocks_unsuccessful_checks_before_reading_key_or_publication_policy(self):
        checks = [{
            "__typename": "CheckRun", "name": "ci", "isRequired": True,
            "status": status, "conclusion": conclusion,
        } for status, conclusion in (
            ("QUEUED", None), ("IN_PROGRESS", None), ("COMPLETED", "FAILURE"),
            ("COMPLETED", "CANCELLED"), ("COMPLETED", "TIMED_OUT"),
            ("COMPLETED", "ACTION_REQUIRED"), ("COMPLETED", "STALE"),
            ("COMPLETED", None), ("COMPLETED", "UNKNOWN"),
        )] + [{
            "__typename": "StatusContext", "context": "ci", "isRequired": True,
            "state": state,
        } for state in ("PENDING", "EXPECTED", "FAILURE", "ERROR")]
        for check in checks:
            with self.subTest(check=check):
                self.check_pages = [[check]]
                with mock.patch.object(Path, "read_text") as read_key, mock.patch.object(
                        self.helper, "replacement_allowed") as policy:
                    with self.assertRaisesRegex(ValueError, "required CI.*ci"):
                        self.helper.prepare("example/project", 12, str(self.key_file) + ".pub")
                read_key.assert_not_called()
                policy.assert_not_called()
                self.assert_read_only_preparation()

    def test_prepare_accepts_passing_required_checks_without_changing_the_draft(self):
        original = copy.deepcopy(self.original)
        for check in [{
            "__typename": "CheckRun", "name": "ci", "isRequired": True,
            "status": "COMPLETED", "conclusion": conclusion,
        } for conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED")] + [{
            "__typename": "StatusContext", "context": "ci", "isRequired": True,
            "state": "SUCCESS",
        }]:
            with self.subTest(check=check):
                self.check_pages = [[check, {
                    "__typename": "CheckRun", "name": "optional", "isRequired": False,
                    "status": "COMPLETED", "conclusion": "FAILURE",
                }]]
                self.assertEqual(self.prepare(), self.request)
                self.assertEqual(self.original, original)
                self.assert_read_only_preparation()
                self.assertTrue(any(endpoint == "graphql" for _, endpoint, _ in self.calls))

    def test_prepare_requires_reported_checks_even_without_configured_contexts(self):
        self.required_contexts = []
        for checks in ([], [{
            "__typename": "CheckRun", "name": "optional", "isRequired": False,
            "status": "COMPLETED", "conclusion": "SUCCESS",
        }]):
            with self.subTest(checks=checks):
                self.check_pages = [checks]
                with self.assertRaisesRegex(ValueError, "no required CI checks"):
                    self.prepare()
                self.assert_read_only_preparation()

    def test_no_required_ci_exception_prepares_only_an_explicit_versioned_scope(self):
        self.required_contexts = []
        self.check_pages = [[], [{
            "__typename": "CheckRun", "name": "optional", "isRequired": False,
            "status": "COMPLETED", "conclusion": "SUCCESS",
        }]]
        with mock.patch.object(Path, "read_text") as read_key:
            with self.assertRaisesRegex(ValueError, "no required CI"):
                self.prepare()
            read_key.assert_not_called()
        with redirect_stderr(io.StringIO()) as errors:
            prepared = self.prepare(allow_no_required_ci=True)
        self.assertEqual(prepared, dict(self.request, version=4, ci_exception="no_required_ci"))
        self.assertIn("not a passing CI result", errors.getvalue())
        self.assert_read_only_preparation()
        with mock.patch.object(Path, "read_text") as read_key:
            checked = self.helper.check_ci("example/project", 12, allow_no_required_ci=True)
        self.assertEqual(checked["required_ci"], "not_configured")
        read_key.assert_not_called()

    def test_no_required_ci_exception_cannot_be_prepared_when_required_ci_passes(self):
        for policy in ("classic", "reported", "workflow"):
            with self.subTest(policy=policy):
                self.required_contexts = ["ci"] if policy == "classic" else []
                self.rules = []
                self.workflow_runs = []
                self.check_pages = [[{
                    "__typename": "CheckRun", "name": "ci", "isRequired": True,
                    "status": "COMPLETED", "conclusion": "SUCCESS",
                }]]
                if policy == "workflow":
                    self.check_pages = [[]]
                    self.add_required_workflow()
                with mock.patch.object(Path, "read_text") as read_key:
                    with self.assertRaisesRegex(ValueError, "prepare without --allow-no-required-ci"):
                        self.prepare(allow_no_required_ci=True)
                    read_key.assert_not_called()
                self.assert_read_only_preparation()

    def test_no_required_ci_exception_accepts_explicitly_null_rollup_not_partial_discovery(self):
        self.required_contexts = []
        api = self.api

        def null_rollup(endpoint, method="GET", data=None):
            result = api(endpoint, method, data)
            if endpoint == "graphql" and "EndorsementRequiredChecks" in data["query"]:
                result["data"]["node"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
            return result

        with mock.patch.object(self.helper, "github", side_effect=null_rollup):
            self.assertEqual(self.prepare(allow_no_required_ci=True)["version"], 4)
            with self.assertRaisesRegex(ValueError, "no required CI"):
                self.prepare()
            self.required_contexts = ["ci"]
            with self.assertRaisesRegex(ValueError, "no required CI"):
                self.prepare(allow_no_required_ci=True)

    def test_no_required_ci_exception_rejects_empty_enabled_classic_policy(self):
        self.required_contexts = []
        self.check_pages = [[]]
        api = self.api
        for null_rollup in (False, True):
            with self.subTest(null_rollup=null_rollup):
                def empty_classic_policy(endpoint, method="GET", data=None):
                    result = api(endpoint, method, data)
                    if endpoint == "graphql" and "EndorsementRequiredChecks" in data["query"]:
                        node = result["data"]["node"]
                        node["baseRef"]["branchProtectionRule"]["requiresStatusChecks"] = True
                        if null_rollup:
                            node["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
                    return result

                with mock.patch.object(self.helper, "github", side_effect=empty_classic_policy):
                    with mock.patch.object(Path, "read_text") as read_key:
                        with self.assertRaisesRegex(ValueError, "no required CI"):
                            self.prepare(allow_no_required_ci=True)
                        read_key.assert_not_called()
        self.assert_read_only_preparation()

    def test_no_required_ci_exception_rejects_rollup_disappearing_during_pagination(self):
        self.required_contexts = []
        self.check_pages = [[], []]
        api = self.api

        def disappearing_rollup(endpoint, method="GET", data=None):
            result = api(endpoint, method, data)
            if (endpoint == "graphql" and "EndorsementRequiredChecks" in data["query"]
                    and data["variables"]["cursor"] is not None):
                result["data"]["node"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
            return result

        with mock.patch.object(self.helper, "github", side_effect=disappearing_rollup):
            with mock.patch.object(Path, "read_text") as read_key:
                with self.assertRaisesRegex(ValueError, "disappeared during pagination"):
                    self.prepare(allow_no_required_ci=True)
                read_key.assert_not_called()
        self.assert_read_only_preparation()

    def test_no_required_ci_exception_does_not_waive_configured_checks_or_workflows(self):
        for policy in ("classic", "ruleset", "workflow", "empty_ruleset"):
            with self.subTest(policy=policy):
                self.required_contexts = []
                self.rules = []
                self.workflow_runs = []
                self.check_pages = [[]]
                if policy == "classic":
                    self.required_contexts = ["ci"]
                elif policy == "workflow":
                    self.add_required_workflow()["conclusion"] = "failure"
                else:
                    self.rules = [{
                        "type": "required_status_checks",
                        "parameters": {"required_status_checks": (
                            [] if policy == "empty_ruleset" else [{"context": "ci"}])},
                    }]
                with mock.patch.object(Path, "read_text") as read_key:
                    with self.assertRaisesRegex(ValueError, "required CI|workflow"):
                        self.prepare(allow_no_required_ci=True)
                    read_key.assert_not_called()
                self.assert_read_only_preparation()

    def test_no_required_ci_exception_keeps_reported_failures_and_pagination_blocking(self):
        self.required_contexts = []
        for status, conclusion in (("QUEUED", None), ("IN_PROGRESS", None),
                                   ("COMPLETED", "FAILURE"), ("COMPLETED", "UNKNOWN")):
            with self.subTest(status=status, conclusion=conclusion):
                self.check_pages = [[], [{
                    "__typename": "CheckRun", "name": "ci", "isRequired": True,
                    "status": status, "conclusion": conclusion,
                }]]
                with self.assertRaisesRegex(ValueError, "required CI.*ci"):
                    self.prepare(allow_no_required_ci=True)
        self.check_pages[1][0].update(status="COMPLETED", conclusion="SUCCESS")
        checked = self.helper.check_ci("example/project", 12, allow_no_required_ci=True)
        self.assertEqual(checked["required_ci"], "passed")
        self.assert_read_only_preparation()

    def test_no_required_ci_exception_fails_closed_on_unreadable_or_malformed_policy(self):
        self.required_contexts = []
        self.check_pages = [[]]
        with mock.patch.object(self.helper, "github_pages", side_effect=RuntimeError("unreadable")):
            with self.assertRaisesRegex(RuntimeError, "unreadable"):
                self.prepare(allow_no_required_ci=True)
        self.failure = ("POST", "graphql")
        with self.assertRaisesRegex(RuntimeError, "fixture API failure"):
            self.prepare(allow_no_required_ci=True)
        self.failure = None
        for rule in ({}, {"type": "workflows", "parameters": {"workflows": []}},
                     {"type": "required_status_checks", "parameters": None}):
            with self.subTest(rule=rule):
                self.rules = [rule]
                with self.assertRaisesRegex(ValueError, "invalid"):
                    self.prepare(allow_no_required_ci=True)
        self.assert_read_only_preparation()

    def test_no_required_ci_exception_preserves_revision_and_assignment_guards(self):
        self.required_contexts = []
        self.check_pages = [[]]
        original = copy.deepcopy(self.original)
        for change in (
            lambda pr: pr["head"].update(sha="0" * 40),
            lambda pr: pr["head"].update(ref="other"),
            lambda pr: pr["base"].update(ref="other"),
            lambda pr: pr.update(draft=False),
            lambda pr: pr.update(state="closed"),
        ):
            with self.subTest(change=change):
                self.original = copy.deepcopy(original)
                self.mutate_on_checks = change
                with self.assertRaisesRegex(ValueError, "changed|open|draft"):
                    self.prepare(allow_no_required_ci=True)
                self.assertFalse(self.readiness_calls())

    def test_no_required_ci_exception_survives_signed_head_and_rechecks_new_requirements(self):
        self.follow_up(self.receipt, count=1)
        self.original["head"].update(
            ref=self.request["source_branch"], sha=self.request["head"])
        self.required_contexts = []
        self.check_pages = [[]]
        self.request = self.prepare(allow_no_required_ci=True)
        self.freeze()
        self.helper.await_attestation(self.plan)
        self.assertIn("fixture", self.helper.assignee_logins(self.original))
        self.receipt = self.repository.sign(self.plan_id, self.approve("replace"))
        self.commits.update({oid: self.commit_data(oid) for oid in (
            *self.receipt["mapping"], *self.receipt["mapping"].values())})
        self.helper.complete_attestation(self.receipt, self.approve("replace"))
        self.assertNotIn("fixture", self.helper.assignee_logins(self.original))
        self.original["head"]["sha"] = self.receipt["head"]
        result = self.finish()
        self.assertEqual(result["ci_scope"], "operator_approved_no_required_ci")
        self.assertTrue(result["ready_for_review"])
        self.required_contexts = ["ci"]
        with self.assertRaisesRegex(ValueError, "missing required CI"):
            self.finish()
        self.check_pages = [[{
            "__typename": "CheckRun", "name": "ci", "isRequired": True,
            "status": "COMPLETED", "conclusion": "FAILURE",
        }]]
        with self.assertRaisesRegex(ValueError, "returned the unchanged signed PR to draft"):
            self.finish()
        self.assertTrue(self.original["draft"])
        self.check_pages[0][0]["conclusion"] = "SUCCESS"
        self.assertEqual(self.finish()["ci_scope"], "observed_required_checks")

        for conclusion in ("SUCCESS", "FAILURE"):
            with self.subTest(added_during_ready=conclusion):
                self.original["draft"] = True
                self.required_contexts = []
                self.check_pages = [[]]

                def add_requirement(_pr):
                    self.required_contexts = ["ci"]
                    self.check_pages = [[{
                        "__typename": "CheckRun", "name": "ci", "isRequired": True,
                        "status": "COMPLETED", "conclusion": conclusion,
                    }]]

                self.mutate_on_ready = add_requirement
                if conclusion == "SUCCESS":
                    self.assertEqual(self.finish()["ci_scope"], "observed_required_checks")
                    self.assertFalse(self.original["draft"])
                else:
                    with self.assertRaisesRegex(
                            ValueError, "returned the unchanged signed PR to draft"):
                        self.finish()
                    self.assertTrue(self.original["draft"])

    def test_no_required_ci_exception_cli_does_not_claim_passing_checks(self):
        self.required_contexts = []
        self.check_pages = [[]]
        args = ["copilot-cs-endorse", "check-ci", "--repo", "example/project", "--pr", "12",
                "--allow-no-required-ci"]
        with mock.patch.dict(os.environ, {"CODESPACES": ""}), mock.patch.object(
                sys, "argv", args), redirect_stdout(io.StringIO()) as output, redirect_stderr(
                io.StringIO()) as errors:
            self.assertEqual(self.helper.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["required_ci"], "not_configured")
        self.assertIn("not a passing CI result", errors.getvalue())
        args[1] = "prepare"
        args += ["--key", str(self.key_file) + ".pub"]
        with mock.patch.dict(os.environ, {"CODESPACES": ""}), mock.patch.object(
                sys, "argv", args), mock.patch.object(
                self.helper, "replacement_allowed", return_value=True), redirect_stdout(
                io.StringIO()) as output, redirect_stderr(io.StringIO()):
            self.assertEqual(self.helper.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["version"], 4)
        self.assertEqual(result["ci_exception"], "no_required_ci")
        self.assertNotIn("required_ci", result)
        self.assert_read_only_preparation()

    def test_prepare_requires_missing_classic_and_ruleset_checks(self):
        for ruleset in (False, True):
            with self.subTest(ruleset=ruleset):
                self.required_contexts = ["ci"] if ruleset else ["ci", "not-started"]
                self.rules = ([{
                    "type": "required_status_checks",
                    "parameters": {"required_status_checks": [{"context": "not-started"}]},
                }] if ruleset else [])
                with self.assertRaisesRegex(ValueError, "missing required CI.*not-started"):
                    self.prepare()
                self.assert_read_only_preparation()

    def test_prepare_checks_every_page_and_rejects_another_heads_results(self):
        self.check_pages.append([{
            "__typename": "CheckRun", "name": "later-page", "isRequired": True,
            "status": "COMPLETED", "conclusion": "FAILURE",
        }])
        with self.assertRaisesRegex(ValueError, "required CI.*later-page"):
            self.prepare()
        self.assertEqual([data["variables"]["cursor"] for _, endpoint, data in self.calls
                          if endpoint == "graphql"], [None, "1"])
        self.assert_read_only_preparation()
        self.check_pages[1][0]["conclusion"] = "SUCCESS"
        self.check_head = self.receipt["head"]
        with self.assertRaisesRegex(ValueError, "CI.*expected PR head"):
            self.prepare()
        self.assert_read_only_preparation()
        self.check_head = None
        self.assertEqual(self.prepare()["head"], self.head)

    def test_prepare_rejects_revision_or_draft_changes_during_the_quality_gate(self):
        original = copy.deepcopy(self.original)
        for change in (
            lambda pr: pr["head"].update(sha="0" * 40),
            lambda pr: pr["head"].update(ref="another-branch"),
            lambda pr: pr["base"].update(ref="another-base"),
            lambda pr: pr.update(state="closed"),
            lambda pr: pr.update(draft=False),
            self.conflicting_base_advance,
            lambda pr: setattr(self, "published_head", self.first),
        ):
            with self.subTest(change=change):
                self.original = copy.deepcopy(original)
                self.live_base = self.base
                self.mergeable = "MERGEABLE"
                self.published_head = None
                self.mutate_on_checks = change
                with self.assertRaisesRegex(ValueError, "changed|open|draft"):
                    self.prepare()
                self.assertFalse(self.readiness_calls())

    def test_prepare_fails_closed_for_unreadable_or_unsupported_quality_policy(self):
        self.failure = ("POST", "graphql")
        with self.assertRaisesRegex(RuntimeError, "fixture API failure"):
            self.prepare()
        self.failure = None
        with mock.patch.object(self.helper, "github_pages", side_effect=RuntimeError("policy unavailable")):
            with self.assertRaisesRegex(RuntimeError, "policy unavailable"):
                self.prepare()
        self.rules = [{"type": "workflows", "parameters": {"workflows": []}}]
        with self.assertRaisesRegex(ValueError, "required workflow rules"):
            self.prepare()
        self.assert_read_only_preparation()

    def test_prepare_cli_emits_no_request_until_required_checks_pass(self):
        args = ["copilot-cs-endorse", "prepare", "--repo", "example/project", "--pr", "12",
                "--key", str(self.key_file) + ".pub"]
        self.check_pages[0][0].update(status="IN_PROGRESS", conclusion=None)
        with mock.patch.dict(os.environ, {"CODESPACES": ""}), mock.patch.object(
                sys, "argv", args), mock.patch.object(
                self.helper, "replacement_allowed", return_value=True):
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(self.helper.main(), 1)
            self.assertEqual(output.getvalue(), "")
            self.assertIn(self.original["html_url"], errors.getvalue())
            self.assertIn("ci: IN_PROGRESS", errors.getvalue())
            self.assert_read_only_preparation()
            self.check_pages[0][0].update(status="COMPLETED", conclusion="SUCCESS")
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(self.helper.main(), 0)
            self.assertEqual(json.loads(output.getvalue()), self.request)
            self.assertEqual(errors.getvalue(), "")
            self.assert_read_only_preparation()

    def test_prepare_reads_source_branch_names_with_slashes(self):
        self.original["head"]["ref"] = "owner/feature"
        request = self.prepare()
        self.assertEqual(request["source_branch"], "owner/feature")
        self.assertIn(("GET", "repos/example/project/git/ref/heads/owner%2Ffeature", None), self.calls)

    def test_prepare_offers_options_without_choosing_a_publication_method(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed), mock.patch.object(
                    self.helper, "replacement_allowed", return_value=allowed):
                result = self.helper.prepare("example/project", 12, str(self.key_file) + ".pub")
            self.assertEqual(result["publication_options"],
                             ["replacement", "replace"] if allowed else ["replacement"])
            self.assertNotIn("mode", result)

    def test_prepare_uses_live_base_ref_instead_of_pr_snapshot(self):
        self.original["base"]["sha"] = "f" * 40
        with mock.patch.object(self.helper, "replacement_allowed", return_value=True):
            request = self.helper.prepare("example/project", 12, str(self.key_file) + ".pub")
        self.assertEqual(request["base"], self.base)
        self.assertEqual(self.repository.plan(request)["commits"], [self.first, self.head])

    def test_prepare_records_explicit_committer_and_checks_operator_key_registration(self):
        committer = {"name": "Human Endorser", "email": "endorser@example.invalid"}
        with mock.patch.object(self.helper, "replacement_allowed", return_value=True):
            request = self.helper.prepare("example/project", 12, str(self.key_file) + ".pub",
                                          committer=committer)
        self.assertEqual(request["committer"], committer)
        self.assertIn(("GET", "user", None), self.calls)
        self.assertIn(("GET-PAGES", "users/fixture/ssh_signing_keys?per_page=100", None), self.calls)
        self.assertFalse(any("/emails" in endpoint for _, endpoint, _ in self.calls))

    def test_prepare_stops_if_operator_has_not_registered_the_signing_key(self):
        self.operator_keys = []
        with mock.patch.object(self.helper, "replacement_allowed", return_value=True):
            with self.assertRaisesRegex(ValueError, "registered.*signing key"):
                self.helper.prepare(
                    "example/project", 12, str(self.key_file) + ".pub",
                    committer={"name": "Human Endorser", "email": "endorser@example.invalid"},
                )

    def test_prepare_reads_base_branch_names_with_slashes(self):
        self.original["base"]["ref"] = "release/current"
        with mock.patch.object(self.helper, "replacement_allowed", return_value=True):
            request = self.helper.prepare("example/project", 12, str(self.key_file) + ".pub")
        self.assertEqual(request["base_branch"], "release/current")
        self.assertIn(("GET", "repos/example/project/git/ref/heads/release%2Fcurrent", None), self.calls)

    def test_prepare_rejects_malformed_live_base_refs(self):
        endpoint = "repos/example/project/git/ref/heads/main"
        responses = [
            None, [], {},
            {"ref": "refs/heads/main", "object": None},
            {"ref": "refs/heads/other", "object": {"type": "commit", "sha": self.base}},
            {"ref": "refs/heads/main", "object": {"type": "tag", "sha": self.base}},
            {"ref": "refs/heads/main", "object": {"type": "commit", "sha": "not-an-oid"}},
            {"ref": "refs/heads/main", "object": {"type": "commit", "sha": None}},
        ]
        for response in responses:
            with self.subTest(response=response):
                def api(path, method="GET", data=None):
                    return response if path == endpoint else self.api(path, method, data)

                with mock.patch.object(self.helper, "github", side_effect=api), mock.patch.object(
                        self.helper, "replacement_allowed", return_value=True):
                    with self.assertRaisesRegex(ValueError, "live branch"):
                        self.helper.prepare("example/project", 12, str(self.key_file) + ".pub")

    def test_prepare_does_not_fall_back_when_live_base_lookup_fails(self):
        self.failure = ("GET", "repos/example/project/git/ref/heads/main")
        with mock.patch.object(self.helper, "replacement_allowed", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "fixture API failure"):
                self.helper.prepare("example/project", 12, str(self.key_file) + ".pub")

    def test_uncertain_or_prohibited_policy_does_not_offer_rewriting(self):
        for response in (RuntimeError("HTTP 403"), [{"type": "non_fast_forward"}]):
            with self.subTest(response=response), mock.patch.object(
                    self.helper, "github", side_effect=response if isinstance(response, Exception)
                    else None, return_value=response):
                with mock.patch("sys.stderr"):
                    self.assertFalse(self.helper.replacement_allowed("example/project", "feature"))

    def test_rule_and_branch_protection_checks_are_both_required(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed), mock.patch.object(self.helper, "github", side_effect=[
                [], {"protected": True}, {"allow_force_pushes": {"enabled": allowed}},
            ]):
                self.assertEqual(self.helper.replacement_allowed("example/project", "feature"), allowed)

    def test_ruleset_only_protection_offers_in_place_publication(self):
        with mock.patch.object(self.helper, "github", side_effect=[
            [{"type": "copilot_code_review"}, {"type": "file_path_restriction"}],
            {"protected": True, "protection": {"enabled": False}},
        ]) as api:
            self.assertTrue(self.helper.replacement_allowed("example/project", "owner/feature"))
        self.assertEqual(api.call_args_list, [
            mock.call("repos/example/project/rules/branches/owner%2Ffeature"),
            mock.call("repos/example/project/branches/owner%2Ffeature"),
        ])

    def test_rewrite_rule_blocks_publication_even_without_classic_protection(self):
        with mock.patch.object(self.helper, "github", side_effect=[
            [{"type": "non_fast_forward"}],
            {"protected": True, "protection": {"enabled": False}},
        ]) as api:
            self.assertFalse(self.helper.replacement_allowed("example/project", "feature"))
        api.assert_called_once_with("repos/example/project/rules/branches/feature")

    def test_missing_classic_summary_does_not_treat_every_404_as_unprotected(self):
        for summary in ({}, {"protection": None}, {"protection": {"enabled": None}}):
            with self.subTest(summary=summary), mock.patch.object(
                self.helper, "github", side_effect=[
                    [], {"protected": True, **summary}, RuntimeError("HTTP 404"),
                ],
            ) as api, mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertFalse(self.helper.replacement_allowed("example/project", "feature"))
                self.assertIn("rewrite permission is uncertain", stderr.getvalue())
                self.assertEqual(api.call_count, 3)


class EndorsementRunnerTests(EndorsementTestCase):
    def test_shipped_helper_fits_the_argument_limit_including_publication_pty(self):
        output = self.emacs(
            f"""(progn
              (require 'cl-lib)
              (require 'json)
              (copilot-cs-use "fixture-codespace" {json.dumps(str(self.root))})
              (cl-letf (((symbol-function 'copilot-cs--run)
                         (lambda (command &rest _arguments)
                           (string-bytes (copilot-cs--launcher "job-payload-check" command)))))
                (let ((plan-id (make-string 64 ?a)))
                  (princ
                   (json-encode
                    (list
                     (copilot-cs-endorse "plan" {json.dumps(json.dumps(self.request))} nil 0)
                     (copilot-cs-endorse "sign" plan-id (concat plan-id ":replacement") 0)
                     (copilot-cs-endorse "verify" plan-id nil 0)
                     (copilot-cs-endorse "push" plan-id (concat plan-id ":replacement") 0)
                     (copilot-cs-endorse "push" plan-id (concat plan-id ":replace") 0 t)))))))"""
        )
        lengths = json.loads(output)
        self.assertEqual(len(lengths), 5)
        for length in lengths:
            self.assertGreater(length, 0)
            self.assertLessEqual(length, 120000)

    def test_helper_compression_failure_stops_before_remote_dispatch(self):
        self.executable(
            "python3",
            "import sys\nprint('fixture compression failure', file=sys.stderr)\nraise SystemExit(42)\n",
        )
        output = self.emacs(
            f"""(progn
              (require 'cl-lib)
              (copilot-cs-use "fixture-codespace" {json.dumps(str(self.root))})
              (cl-letf (((symbol-function 'copilot-cs--run)
                         (lambda (&rest _arguments) (error "unexpected remote dispatch"))))
                (condition-case err
                    (copilot-cs-endorse "plan" {json.dumps(json.dumps(self.request))} nil 0)
                  (error (princ (error-message-string err))))))"""
        )
        self.assertIn("could not compress endorsement helper", output)
        self.assertIn("fixture compression failure", output)
        self.assertNotIn("unexpected remote dispatch", output)

    def test_shipped_helper_runs_through_detached_jobs_and_returns_complete_receipts(self):
        real_git = shutil.which("git")
        self.executable(
            "git",
            f"""import os, sys
if sys.argv[1:3] == ["remote", "get-url"]:
    print("https://github.com/example/project.git")
else:
    os.execv({real_git!r}, ["git", *sys.argv[1:]])
""",
        )
        self.executable(
            "bash",
            """import os, sys
assert sys.argv[1] == "-lc" and len(sys.argv) == 4
os.environ["SSH_AUTH_SOCK"] = "/missing/profile-agent"
os.execv("/bin/sh", ["sh", "-c", sys.argv[2], sys.argv[3]])
""",
        )
        output = self.emacs(
            f"""(progn
              (require 'cl-lib)
              (require 'json)
              (copilot-cs-use "fixture-codespace" {json.dumps(str(self.root))})
              (let ((copilot-cs-tail-bytes 8)
                    (copilot-cs-remote-dir
                     (shell-quote-argument {json.dumps(str(self.root / "jobs"))})))
                (cl-letf (((symbol-function 'copilot-cs--argv)
                           (lambda (command &optional _sign _id)
                             (when (> (string-bytes command) 120000)
                               (error "Helper exceeds Linux single-argument limit"))
                             (list "sh" "-c" command))))
                  (let* ((report (copilot-cs-endorse "plan" {json.dumps(json.dumps(self.request))} nil 10))
                         (id (copilot-cs-job-id report))
                         (plan (json-parse-string (copilot-cs-endorsement-result id)))
                         (plan-id (gethash "plan_id" plan))
                         (approval (concat plan-id ":replacement")))
                    (setq report (copilot-cs-endorse "sign" plan-id approval 10))
                    (princ (copilot-cs-endorsement-result (copilot-cs-job-id report))))))))"""
        )
        receipt = json.loads(output)
        self.assertEqual(receipt["publication"], "replacement")
        self.assertEqual(receipt["plan"]["request"], self.request)
        self.assertEqual(len(receipt["mapping"]), 2)
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.head)
        self.assertEqual(self.git("rev-parse", "feature-signed").strip(), receipt["head"])
