"""Endorsement regressions using local Git fixtures and an isolated test agent."""

import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from unittest import mock

from test_helpers import HelperTestCase, SETUP


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
        for field in ("author", "committer"):
            result[field] = next(block.decode() for block in blocks if block.startswith(field.encode() + b" "))
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
        result = self.run_command(["python3", str(SETUP / "copilot-cs-endorse"),
                                   "prepare", "--repo", "example/project", "--pr", "12"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("preparation locally", result.stderr)

    def test_malformed_review_input_is_rejected(self):
        for request in (None, [], {}, dict(self.request, key=None),
                        dict(self.request, target_branch="main"),
                        dict(self.request, source_branch="../unsafe")):
            with self.subTest(request=request), self.assertRaises((ValueError, RuntimeError)):
                self.repository.plan(request)


class PublicationTests(EndorsementTestCase):
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
            "number": 12, "html_url": self.request["url"], "state": "open",
            "draft": True, "merged": False, "title": "Implement change",
            "body": "See example/other#23 and PR 45.\r\nOriginal context.",
            "head": {"ref": "feature", "sha": self.head, "repo": {"full_name": "example/project"}},
            "base": {"ref": "main", "sha": self.base, "repo": {"full_name": "example/project"}},
            "labels": [{"name": "review"}], "assignees": [{"login": "fixture"}],
            "milestone": {"number": 7}, "requested_reviewers": [{"login": "reviewer"}],
            "requested_teams": [{"slug": "team"}],
        }
        self.replacement = None
        self.comments = []
        self.calls = []
        self.failure = None
        self.drop_metadata = False
        self.mutate_on_comment = None
        self.commits = {oid: self.commit_data(oid) for oid in
                        [*self.receipt["mapping"], *self.receipt["mapping"].values()]}
        api = mock.patch.object(self.helper, "github", side_effect=self.api)
        pages = mock.patch.object(self.helper, "github_pages", side_effect=self.pages)
        api.start()
        pages.start()
        self.addCleanup(api.stop)
        self.addCleanup(pages.stop)

    def pages(self, endpoint):
        self.calls.append(("GET-PAGES", endpoint, None))
        if "/pulls?" in endpoint:
            return [copy.deepcopy(self.replacement)] if self.replacement else []
        if endpoint.endswith("/comments?per_page=100"):
            return copy.deepcopy(self.comments)
        raise AssertionError(endpoint)

    def api(self, endpoint, method="GET", data=None):
        self.calls.append((method, endpoint, data))
        if self.failure == (method, endpoint):
            raise RuntimeError("fixture API failure")
        if endpoint == "repos/example/project/pulls/12":
            if method == "PATCH":
                self.original.update(data)
            return copy.deepcopy(self.original)
        if "/git/commits/" in endpoint:
            return copy.deepcopy(self.commits[endpoint.rsplit("/", 1)[1]])
        if "/branches/" in endpoint:
            return {"commit": {"sha": self.receipt["head"]}}
        if endpoint == "repos/example/project/pulls" and method == "POST":
            self.replacement = copy.deepcopy(self.original)
            self.replacement.update(
                number=13, html_url="https://github.com/example/project/pull/13",
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

    def test_replacement_is_verified_linked_and_populated_before_original_closes(self):
        result = self.finish()
        self.assertEqual(result["url"], "https://github.com/example/project/pull/13")
        self.assertEqual(self.original["state"], "closed")
        self.assertTrue(self.replacement["draft"])
        for field in ("labels", "assignees", "milestone", "requested_reviewers", "requested_teams"):
            self.assertEqual(self.original[field], self.replacement[field])
        self.assertNotIn("\r", self.replacement["body"])
        self.assertNotRegex(self.replacement["body"], r"#\d+")
        self.assertIn("https://github.com/example/other/issues/23", self.replacement["body"])
        self.assertIn("https://github.com/example/project/pull/45", self.replacement["body"])
        self.assertIn(result["url"], self.comments[0]["body"])
        self.assertEqual(self.calls[-1], ("PATCH", "repos/example/project/pulls/12", {"state": "closed"}))

    def test_completed_finish_is_idempotent(self):
        first = self.finish()
        self.calls.clear()
        self.assertEqual(self.finish(), first)
        self.assertEqual(len(self.comments), 1)
        self.assertFalse(any(method == "POST" for method, _, _ in self.calls))

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
                              ("replacement", "base")):
            with self.subTest(target=target, field=field):
                self.comments.clear()
                self.original["head"]["sha"] = self.head
                if self.replacement:
                    self.replacement["head"]["sha"] = self.receipt["head"]
                    self.replacement["base"]["sha"] = self.base
                self.mutate_on_comment = lambda: getattr(self, target)[field].update(sha="0" * 40)
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
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls))

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

    def test_prepare_offers_options_without_choosing_a_publication_method(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed), mock.patch.object(
                    self.helper, "replacement_allowed", return_value=allowed):
                result = self.helper.prepare("example/project", 12, str(self.key_file) + ".pub")
            self.assertEqual(result["publication_options"],
                             ["replacement", "replace"] if allowed else ["replacement"])
            self.assertNotIn("mode", result)

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
