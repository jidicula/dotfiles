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
            "signature": signatures[0].decode() if signatures else None,
            "payload": self.repository.payload(oid, {}).decode() if signatures else None,
        }
        self.assertEqual(hashlib.sha1(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest(), oid)
        return result


class SignedHistoryTests(EndorsementTestCase):
    def test_signing_progress_precedes_requests_and_verification_never_resigns(self):
        output = io.StringIO()
        original_run = self.helper.run
        requests = []

        def track(arguments, **kwargs):
            if arguments[:3] == ["ssh-keygen", "-Y", "sign"]:
                requests.append(arguments)
                self.assertIn(f"] signing {len(requests)}/2:", output.getvalue())
            return original_run(arguments, **kwargs)

        with redirect_stdout(output), mock.patch.object(self.helper, "run", side_effect=track):
            self.sign()
        self.assertEqual(len(requests), 2)
        output.seek(0)
        output.truncate()
        with redirect_stdout(output), mock.patch.object(self.helper, "run", wraps=original_run) as calls:
            self.repository.verify(self.plan_id)
        self.assertIn("] verifying 1/2:", output.getvalue())
        self.assertIn("] verifying 2/2:", output.getvalue())
        self.assertNotIn("] signing ", output.getvalue())
        self.assertFalse(any(call.args[0][:3] == ["ssh-keygen", "-Y", "sign"]
                             for call in calls.call_args_list))

    def test_ecdsa_agent_signatures_verify_with_native_git(self):
        key = self.root / "test-only-ecdsa"
        self.command("ssh-keygen", "-q", "-t", "ecdsa", "-b", "256", "-N", "", "-f", str(key))
        self.command("ssh-add", str(key))
        public = self.helper.public_key(Path(str(key) + ".pub").read_text())
        self.request.update(key=public, fingerprint=self.helper.fingerprint(public))
        with mock.patch.dict(os.environ, COPILOT_CS_SIGNING_KEY=public):
            receipt = self.sign()
        allowed = self.root / "allowed"
        allowed.write_text("endorser " + public + "\n")
        self.git("-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={allowed}",
                 "verify-commit", receipt["head"])

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

    def test_stale_local_source_or_remote_base_invalidates_approval(self):
        self.freeze()
        self.git("update-ref", "refs/heads/feature", self.first)
        with self.assertRaisesRegex(ValueError, "local source branch changed"):
            self.repository.sign(self.plan_id, self.approve())
        self.git("update-ref", "refs/heads/feature", self.head)
        self.git("push", "--quiet", "origin", f"{self.first}:refs/heads/main")
        with self.assertRaisesRegex(ValueError, "remote base changed"):
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

    def test_stale_request_is_rejected_before_fetch(self):
        self.advance_remote_base(self.request["base"])
        with mock.patch.object(self.repository, "git", wraps=self.repository.git) as git:
            with self.assertRaisesRegex(ValueError, "remote base changed"):
                self.freeze()
        self.assertFalse(any(call.args[0] == "fetch" for call in git.call_args_list))
        self.assertFalse(list(self.repository.state.glob("*.plan.json")))

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

    def test_base_change_during_push_does_not_report_publication_complete(self):
        self.sign()
        original_git = self.repository.git

        def race(*arguments, **kwargs):
            result = original_git(*arguments, **kwargs)
            if arguments[0] == "push":
                self.git("--git-dir=" + str(self.remote), "update-ref", "refs/heads/main", self.first)
            return result

        with mock.patch.object(self.repository, "git", side_effect=race):
            with self.assertRaisesRegex(ValueError, "remote base changed"):
                self.repository.push(self.plan_id, self.approve())


class GitHubEndorsementTests(EndorsementTestCase):
    def setUp(self):
        super().setUp()
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
            return copy.deepcopy(self.workflow_runs)
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
        if endpoint.startswith("repositories/"):
            parts = endpoint.split("/")
            source = self.workflow_sources[int(parts[1])]
            if len(parts) == 2:
                return {"id": int(parts[1]), "full_name": source["repository"]}
            self.assertEqual(parts[2], "commits")
            self.assertEqual(parts[3], quote(source["ref"], safe=""))
            return {"sha": source["sha"]}
        if "/actions/runs/" in endpoint:
            result = copy.deepcopy(next(run for run in self.workflow_runs
                                        if run["id"] == int(endpoint.rsplit("/", 1)[1])))
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
            if "EndorsementWorkflowRun" in data["query"]:
                return {"data": {"node": {
                    "workflowRun": copy.deepcopy(self.workflow_provenance[data["variables"]["id"]]),
                }}}
            pr = (self.original if data["variables"]["id"] == "PR_original"
                  else self.replacement)
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
            page = int(data["variables"]["cursor"] or 0)
            last = page == len(self.check_pages) - 1
            response = {"data": {"node": {
                "id": pr["node_id"], "state": pr["state"].upper(),
                "headRefOid": pr["head"]["sha"], "headRefName": pr["head"]["ref"],
                "baseRefName": pr["base"]["ref"],
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

    def prepare(self):
        with mock.patch.object(self.helper, "replacement_allowed", return_value=True):
            return self.helper.prepare("example/project", 12, str(self.key_file) + ".pub")

    def assert_read_only_preparation(self):
        self.assertTrue(self.original["draft"])
        self.assertEqual(self.original["state"], "open")
        self.assertIsNone(self.replacement)
        self.assertEqual(self.comments, [])
        self.assertTrue(all(
            method in ("GET", "GET-PAGES")
            or (endpoint == "graphql" and any(name in data["query"] for name in (
                "EndorsementRequiredChecks", "EndorsementWorkflowRun")))
            for method, endpoint, data in self.calls
        ))

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
                self.assertEqual(sum("/actions/runs?" in endpoint
                                     for _, endpoint, _ in self.calls), 1)
                self.assertFalse(any("/actions/runs/" in endpoint
                                     for _, endpoint, _ in self.calls))
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
                with self.assertRaisesRegex(ValueError, "workflow"):
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
        self.mutate_on_workflow = lambda: setattr(self, "live_base", self.first)
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
            lambda result: result["data"].update(node=None),
            lambda result: result["data"]["node"].update(workflowRun=None),
        ):
            with self.subTest(change=change):
                def api(endpoint, method="GET", data=None):
                    result = self.api(endpoint, method, data)
                    if endpoint == "graphql" and "EndorsementWorkflowRun" in data["query"]:
                        change(result)
                    return result
                with mock.patch.object(self.helper, "github", side_effect=api):
                    with self.assertRaisesRegex(ValueError, "workflow"):
                        self.prepare()
        def rerun(endpoint, method="GET", data=None):
            result = self.api(endpoint, method, data)
            if endpoint == "repos/example/project/actions/runs/100":
                result["run_attempt"] += 1
            return result
        with mock.patch.object(self.helper, "github", side_effect=rerun):
            with self.assertRaisesRegex(ValueError, "required workflow changed"):
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
        plan = {key: copy.deepcopy(self.plan[key]) for key in ("request", "root", "commits")}
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
        closed = ("PATCH", "repos/example/project/pulls/12", {"state": "closed"})
        self.assertLess(self.calls.index(closed), self.calls.index(self.readiness_calls()[0]))
        self.assertEqual(self.readiness_calls()[0][2]["variables"]["id"], "PR_replacement")

    def test_completed_finish_is_idempotent(self):
        first = self.finish()
        self.calls.clear()
        self.assertEqual(self.finish(), first)
        self.assertEqual(len(self.comments), 1)
        self.assertFalse(self.readiness_calls())
        self.assertFalse(any(method == "POST" and endpoint != "graphql"
                             for method, endpoint, _ in self.calls))

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
            lambda pr: setattr(self, "live_base", self.first),
            lambda pr: setattr(self, "published_head", self.first),
        ):
            with self.subTest(change=change):
                self.original.update(state="open")
                self.live_base = self.base
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
            })

    def test_live_base_change_blocks_finalization_before_metadata_mutation(self):
        self.live_base = self.first
        with self.assertRaisesRegex(ValueError, "base branch changed"):
            self.finish()
        self.assertIsNone(self.replacement)
        self.assertEqual(self.original["state"], "open")
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls))

    def test_live_base_change_during_signature_verification_blocks_both_publications(self):
        verify = self.helper.verify_signature

        def change_base(*arguments):
            verify(*arguments)
            self.live_base = self.first

        for publication in ("replacement", "replace"):
            with self.subTest(publication=publication):
                self.live_base = self.base
                self.calls.clear()
                self.receipt["publication"] = publication
                self.original["head"]["sha"] = (
                    self.receipt["head"] if publication == "replace" else self.head)
                with mock.patch.object(self.helper, "verify_signature", side_effect=change_base):
                    with self.assertRaisesRegex(ValueError, "base branch changed"):
                        self.finish()
                self.assertIsNone(self.replacement)
                self.assertEqual(self.original["state"], "open")
                self.assertTrue(all(method == "GET" for method, _, _ in self.calls))

    def test_live_base_change_after_linking_keeps_original_open(self):
        self.mutate_on_comment = lambda: setattr(self, "live_base", self.first)
        with self.assertRaisesRegex(ValueError, "base branch changed"):
            self.finish()
        self.assertEqual(self.original["state"], "open")
        self.assertEqual(self.replacement["state"], "open")

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
            lambda pr: setattr(self, "live_base", self.first),
            lambda pr: setattr(self, "published_head", self.first),
        ):
            with self.subTest(change=change):
                self.original = copy.deepcopy(original)
                self.live_base = self.base
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
