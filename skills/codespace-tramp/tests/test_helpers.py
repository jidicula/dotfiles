"""Regression tests for Git configuration and Codespace runner helpers.

Run with: python3 -m unittest discover -s skills/codespace-tramp/tests -v
Only Python's standard library, Emacs, Git, and system shell tools are used.
"""

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest


SETUP = Path(__file__).resolve().parents[1] / "setup"
DOTFILES = SETUP.parents[2]
DEADLINE_ERROR = (
    "error getting ssh server details: failed to invoke SSH RPC: "
    "rpc error: code = DeadlineExceeded desc = context deadline exceeded"
)
UNAVAILABLE_ERROR = (
    "error getting ssh server details: failed to invoke SSH RPC: "
    "rpc error: code = Unavailable desc = connection error: "
    'desc = "error reading server preface: use of closed network connection"'
)


class HelperTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cs-test-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {
            "HOME": str(self.root),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(self.root),
            "LC_ALL": "C",
        }

    def executable(self, name, body):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        path.chmod(0o755)

    def unix_socket(self, path):
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(path))

    def run_command(self, argv, timeout=20):
        return subprocess.run(
            argv,
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def emacs(self, expression):
        result = self.run_command(
            [
                shutil.which("emacs"),
                "-Q",
                "--batch",
                "-L",
                str(SETUP),
                "-l",
                "copilot-cs-jobs",
                "--eval",
                expression,
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout


class GitConfigurationTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.env["GIT_CONFIG_SYSTEM"] = "/dev/null"
        self.dotfiles = self.root / "dotfiles"
        self.dotfiles.mkdir()
        (self.root / ".config").mkdir()
        for name in (
            "gitconfig",
            "gitconfig-local",
            "gitconfig-personal",
            "gitconfig-work",
            "gitconfig-codespaces",
            "gitignore",
            "git-commit-message",
        ):
            shutil.copyfile(DOTFILES / name, self.dotfiles / name)
        (self.dotfiles / "git-templates").mkdir()
        (self.dotfiles / "emacs-plus").mkdir()
        self.project = self.root / "workspaces" / "example"
        self.project.mkdir(parents=True)
        self.local_config = self.root / ".gitconfig-local"

        # Run the actual Git installation block, without sudo or package setup.
        script = (DOTFILES / "script" / "setup").read_text()
        _, marker, block = script.partition("# copy Git configs and templates\n")
        self.assertTrue(marker)
        self.install_block, marker, _ = block.partition("\nif [[ $CODESPACES ]]; then")
        self.assertTrue(marker)

    def install(self):
        result = self.run_command(
            [
                "bash",
                "-c",
                'set -eu\nDOTFILESDIR="$1"\n' + self.install_block,
                "git-config-setup",
                str(self.dotfiles),
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def git(self, *arguments):
        result = self.run_command(["git", "-C", str(self.project), *arguments])
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def initialize_repo(self):
        self.git("init", "--quiet", "--template=")
        self.git("remote", "add", "origin", "https://github.com/github/example.git")

    def assert_hardlink(self):
        self.assertTrue(self.local_config.is_file())
        self.assertFalse(self.local_config.is_symlink())
        self.assertTrue(self.local_config.samefile(self.dotfiles / "gitconfig-local"))

    def test_local_setup_hardlinks_config_and_keeps_ssh_rewrite(self):
        self.install()
        self.assert_hardlink()
        self.initialize_repo()
        self.assertEqual(
            self.git("remote", "get-url", "origin"),
            "git@github.com:github/example.git",
        )

    def test_local_setup_is_idempotent(self):
        self.install()
        inode = self.local_config.stat().st_ino
        self.install()
        self.assert_hardlink()
        self.assertEqual(self.local_config.stat().st_ino, inode)

    def test_local_setup_replaces_a_symlink_with_a_hardlink(self):
        self.local_config.symlink_to(self.dotfiles / "gitconfig-local")
        self.install()
        self.assert_hardlink()

    def test_local_setup_refreshes_link_after_source_replacement(self):
        self.install()
        replacement = self.dotfiles / "replacement"
        replacement.write_text("[test]\n\tupdated = true\n")
        replacement.replace(self.dotfiles / "gitconfig-local")
        self.install()
        self.assert_hardlink()
        self.assertEqual(self.local_config.read_text(), "[test]\n\tupdated = true\n")

    def test_codespace_setup_omits_local_config_and_keeps_shared_defaults(self):
        self.env["CODESPACES"] = "true"
        self.install()
        self.assertFalse(self.local_config.exists())
        self.initialize_repo()
        self.assertEqual(
            self.git("remote", "get-url", "origin"),
            "https://github.com/github/example.git",
        )
        self.assertEqual(self.git("config", "--get", "merge.conflictstyle"), "diff3")
        self.assertEqual(self.git("config", "--get", "commit.gpgsign"), "true")
        self.assertEqual(self.git("config", "--get", "gpg.format"), "openpgp")
        self.assertEqual(
            self.git("config", "--get", "credential.helper"),
            "/.codespaces/bin/gitcredential_github.sh",
        )

    def test_login_wrapper_preserves_shared_git_configuration(self):
        self.env["CODESPACES"] = "true"
        self.install()
        self.initialize_repo()
        command = shlex.join(
            ["git", "-C", str(self.project), "config", "--get", "merge.conflictstyle"]
        )
        wrapper = self.emacs(
            f"(princ (copilot-cs--login-shell-command {json.dumps(command)}))"
        )
        result = self.run_command(["sh", "-c", wrapper])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "diff3")


class CodespaceDependencyTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.bash = shutil.which("bash")
        self.calls = self.root / "package-calls"
        self.env.update(
            {
                "PATH": str(self.bin),
                "CODESPACES": "true",
                "CODESPACE_NAME": "example-work-repository",
                "FAKE_PACKAGE_CALLS": str(self.calls),
                "FAKE_LFS_PATH": str(self.bin / "git-lfs"),
                "FAKE_PACKAGE_FAILURE": "",
            }
        )
        script = (DOTFILES / "script" / "setup").read_text()
        _, marker, block = script.partition("\nif [[ $CODESPACES ]]; then\n")
        self.assertTrue(marker)
        block, marker, _ = block.partition("\n# Enable touchID sudo authentication")
        self.assertTrue(marker)
        self.setup_block = "if [[ $CODESPACES ]]; then\n" + block
        self.executable("apt-get", "raise SystemExit('expected sudo invocation')\n")
        self.executable(
            "sudo",
            """import json, os, sys
from pathlib import Path
with open(os.environ["FAKE_PACKAGE_CALLS"], "a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[2] == os.environ["FAKE_PACKAGE_FAILURE"]:
    print("fixture package operation failed", file=sys.stderr)
    sys.exit(23)
if sys.argv[1:3] == ["apt-get", "install"]:
    binary = Path(os.environ["FAKE_LFS_PATH"])
    binary.write_text("#!/bin/sh\\nexit 0\\n")
    binary.chmod(0o755)
""",
        )

    def invoke(self):
        return self.run_command([self.bash, "-c", "set -eu\n" + self.setup_block])

    def recorded_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_minimal_setup_installs_missing_git_lfs(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.recorded_calls(),
            [
                ["apt-get", "update"],
                ["apt-get", "install", "-y", "--no-install-recommends", "git-lfs"],
            ],
        )
        self.assertIn("setup without dotfiles", result.stdout)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.recorded_calls()), 2, "setup reinstalled Git LFS")

    def test_existing_git_lfs_skips_package_operations(self):
        self.executable("git-lfs", "print('fixture Git LFS')\n")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded_calls(), [])
        self.assertIn("setup without dotfiles", result.stdout)

    def test_failed_package_operations_stop_setup(self):
        for operation in ("update", "install"):
            with self.subTest(operation=operation):
                self.env["FAKE_PACKAGE_FAILURE"] = operation
                result = self.invoke()
                self.assertEqual(result.returncode, 23, result.stderr)
                self.assertIn("fixture package operation failed", result.stderr)
                self.assertNotIn("setup without dotfiles", result.stdout)
                self.assertFalse((self.bin / "git-lfs").exists())
        self.assertEqual(
            [call[1] for call in self.recorded_calls()], ["update", "update", "install"]
        )

    def test_unsupported_package_manager_requires_explicit_installation(self):
        (self.bin / "apt-get").unlink()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("install git-lfs in this Codespace", result.stderr)
        self.assertNotIn("setup without dotfiles", result.stdout)
        self.assertEqual(self.recorded_calls(), [])

    def test_local_setup_does_not_use_the_codespace_installer(self):
        self.env["CODESPACES"] = ""
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded_calls(), [])


class SecretiveTransportTestCase(HelperTestCase):
    rpc_error = DEADLINE_ERROR

    def setUp(self):
        super().setUp()
        self.calls = self.root / "gh-calls"
        self.sleeps = self.root / "sleep-calls"
        agent = self.root / "agent.sock"
        self.unix_socket(agent)
        public = self.root / "public.pub"
        public.write_text("test public-key stand-in\n", encoding="utf-8")
        self.env.update(
            {
                "COPILOT_SECRETIVE_AGENT_SOCKET": str(agent),
                "COPILOT_SECRETIVE_PUBLIC_KEY": str(public),
                "COPILOT_SECRETIVE_STANDIN": str(self.root / "ssh" / "standin"),
                "FAKE_GH_CALLS": str(self.calls),
                "FAKE_GH_FAILURES": "1",
                "FAKE_GH_ERROR": self.rpc_error,
                "FAKE_SLEEP_CALLS": str(self.sleeps),
            }
        )
        self.executable(
            "gh",
            """import json, os, sys
from pathlib import Path
path = Path(os.environ["FAKE_GH_CALLS"])
calls = path.read_text().splitlines() if path.exists() else []
calls.append(json.dumps(sys.argv[1:]))
path.write_text("\\n".join(calls) + "\\n")
if len(calls) <= int(os.environ["FAKE_GH_FAILURES"]):
    print(os.environ["FAKE_GH_ERROR"], file=sys.stderr)
    sys.exit(1)
""",
        )
        self.executable(
            "sleep",
            """import os, sys
with open(os.environ["FAKE_SLEEP_CALLS"], "a") as output:
    output.write(" ".join(sys.argv[1:]) + "\\n")
""",
        )

    def invoke(self, *arguments):
        return self.run_command(["bash", str(SETUP / "copilot-ghcs"), *arguments])

    def recorded_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]


class WarmupTests(SecretiveTransportTestCase):
    def test_warmup_retries_rpc_startup_failure_without_changing_identity(self):
        result = self.invoke("ssh", "test-codespace", "true")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertIn("IdentitiesOnly=yes", calls[0])
        self.assertIn(
            f"IdentityAgent={self.env['COPILOT_SECRETIVE_AGENT_SOCKET']}", calls[0]
        )
        self.assertEqual(calls[0][-1], "true")
        self.assertEqual(self.sleeps.read_text().splitlines(), ["5"])
        self.assertIn(self.rpc_error, result.stderr)
        self.assertFalse(list(self.root.glob("copilot-ghcs-warm.*")))

    def test_warmup_stops_after_three_attempts_and_keeps_error(self):
        self.env["FAKE_GH_FAILURES"] = "10"
        result = self.invoke("ssh", "test-codespace", "true")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.recorded_calls()), 3)
        self.assertEqual(self.sleeps.read_text().splitlines(), ["5", "10"])
        self.assertIn(self.rpc_error, result.stderr)
        self.assertFalse(list(self.root.glob("copilot-ghcs-warm.*")))

    def test_signing_refusal_is_not_retried(self):
        self.env["FAKE_GH_ERROR"] = "sign_and_send_pubkey: agent refused operation"
        result = self.invoke("ssh", "test-codespace", "true")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.recorded_calls()), 1)
        self.assertFalse(self.sleeps.exists())

    def test_other_connection_errors_are_not_retried(self):
        self.env["FAKE_GH_ERROR"] = "Host key verification failed."
        result = self.invoke("ssh", "test-codespace", "true")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_rpc_authentication_error_is_not_retried(self):
        self.env["FAKE_GH_ERROR"] = (
            "error getting ssh server details: failed to invoke SSH RPC: "
            "rpc error: code = Unauthenticated desc = authentication failed"
        )
        result = self.invoke("ssh", "test-codespace", "true")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.recorded_calls()), 1)
        self.assertFalse(self.sleeps.exists())

    def test_repository_command_is_never_replayed(self):
        result = self.invoke("ssh", "test-codespace", "true; git push")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_interactive_connection_is_never_replayed(self):
        result = self.invoke("ssh", "test-codespace")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_copy_is_never_replayed(self):
        result = self.invoke("cp", "test-codespace", "local-file", "remote:/tmp/file")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.recorded_calls()), 1)


class UnavailableWarmupTests(WarmupTests):
    rpc_error = UNAVAILABLE_ERROR


class CopyTests(SecretiveTransportTestCase):
    def test_local_session_artifact_uses_explicit_copy_transport(self):
        artifact = self.root / "session-state" / "test0001" / "files" / "change patch"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("fixture patch content\n", encoding="utf-8")
        self.env["FAKE_GH_FAILURES"] = "0"
        result = self.invoke(
            "cp", "test-codespace", str(artifact), "remote:/tmp/test0001.patch"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ["codespace", "cp", "-c", "test-codespace"])
        self.assertEqual(calls[0][-2:], [str(artifact), "remote:/tmp/test0001.patch"])
        self.assertIn("--expand", calls[0])
        self.assertIn("IdentitiesOnly=yes", calls[0])
        self.assertIn(
            f"IdentityAgent={self.env['COPILOT_SECRETIVE_AGENT_SOCKET']}", calls[0]
        )
        self.assertFalse(self.sleeps.exists())

    def test_unsafe_remote_copy_path_is_rejected_before_connecting(self):
        result = self.invoke(
            "cp", "test-codespace", "local-file", "remote:/tmp/patch;other-command"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe remote copy path", result.stderr)
        self.assertFalse(self.calls.exists())


class JobStateTests(HelperTestCase):
    def check_job(self, body, output="", live=False):
        self.emacs(
            f"""(progn
              (require 'cl-lib)
              (let* ((id "job-state-fixture")
                     (buffer (generate-new-buffer " *job-state-fixture*"))
                     (process (make-pipe-process :name "job-state-fixture"
                                                 :buffer buffer :noquery t))
                     (job (list :id id :cmd "fixture command"
                                :cs "original-codespace" :dir "/workspaces/original"
                                :buffer buffer :process process
                                :started (- (float-time) 3000))))
                (unwind-protect
                    (progn
                      (set-process-sentinel process #'ignore)
                      (with-current-buffer buffer (insert {json.dumps(output)}))
                      {"nil" if live else "(delete-process process)"}
                      (puthash id job copilot-cs--jobs)
                      {body})
                  (when (process-live-p process) (delete-process process))
                  (when (buffer-live-p buffer) (kill-buffer buffer)))))"""
        )

    def test_failed_unacknowledged_launch_is_not_reconnected(self):
        for output in (
            "sign_and_send_pubkey: agent refused operation",
            "command output arrived before the connection closed",
            "copilot-cs: no log for this job in this Codespace",
        ):
            with self.subTest(output=output):
                self.check_job(
                    f"""(cl-letf (((symbol-function 'copilot-cs--resume)
                                  (lambda (_) (error "Unexpected reconnect"))))
                      (dotimes (_ 2)
                        (let ((report (copilot-cs-poll id 0)))
                          (unless (and (string-match-p "state=failed" report)
                                       (string-match-p
                                         "remote outcome is unconfirmed" report)
                                       (not (string-match-p
                                              "nothing is running" report)))
                            (error "Incorrect failure report: %s" report))))
                      (unless (equal (copilot-cs-output id) {json.dumps(output)})
                        (error "Original transport output was lost"))
                      (unless (string-match-p "job-state-fixture +failed"
                                              (copilot-cs-status))
                        (error "Failed launch was listed as detached")))""",
                    output=output,
                )

    def test_live_unacknowledged_job_is_connecting_not_running(self):
        self.check_job(
            """(cl-letf (((symbol-function 'copilot-cs--resume)
                         (lambda (_) (error "Unexpected reconnect"))))
                (unless (and (string-match-p "state=connecting"
                                              (copilot-cs-poll id 0))
                             (string-match-p "job-state-fixture +connecting"
                                              (copilot-cs-status)))
                  (error "Unacknowledged connection was reported running")))""",
            live=True,
        )

    def test_live_acknowledged_job_is_running(self):
        self.check_job(
            """(unless (and (string-match-p "state=running"
                                            (copilot-cs-poll id 0))
                           (string-match-p "job-state-fixture +running"
                                            (copilot-cs-status)))
                 (error "Acknowledged job was not reported running"))""",
            output="__COPILOT_CS_ACK_job-state-fixture__\nprogress\n",
            live=True,
        )

    def test_completed_job_never_reconnects(self):
        self.check_job(
            """(cl-letf (((symbol-function 'copilot-cs--resume)
                         (lambda (_) (error "Unexpected reconnect"))))
                (unless (and (string-match-p "state=done rc=0"
                                              (copilot-cs-poll id 0))
                             (string-match-p "job-state-fixture +rc=0"
                                              (copilot-cs-status)))
                  (error "Completed job was not preserved")))""",
            output="completed\n__COPILOT_CS_DONE_job-state-fixture__:0\n",
        )

    def test_partial_or_embedded_ack_does_not_confirm_launch(self):
        for output in (
            "__COPILOT_CS_ACK_job-state-fixture_",
            "prefix __COPILOT_CS_ACK_job-state-fixture__\n",
            "__COPILOT_CS_ACK_other-job__\n",
        ):
            with self.subTest(output=output):
                self.check_job(
                    """(cl-letf (((symbol-function 'copilot-cs--resume)
                                 (lambda (_) (error "Unexpected reconnect"))))
                        (unless (string-match-p "state=failed"
                                                (copilot-cs-poll id 0))
                          (error "Output was mistaken for acknowledgement")))""",
                    output=output,
                )

    def test_acknowledged_disconnect_reconnects_to_original_target(self):
        self.check_job(
            """(let ((copilot-cs-id "different-codespace")
                     (copilot-cs-dir "/workspaces/different")
                     refreshed)
                (cl-letf (((symbol-function 'copilot-cs--start)
                           (lambda (key command label)
                             (unless (and (equal key id)
                                          (equal copilot-cs-id "original-codespace")
                                          (equal copilot-cs-dir "/workspaces/original")
                                          (equal command
                                                 (copilot-cs--attach-command id))
                                          (equal label "fixture command"))
                               (error "Reconnect changed target or replayed command"))
                             (setq refreshed (list :id key :started (float-time))))))
                  (unless (and (eq (copilot-cs--live job) refreshed)
                               (equal (plist-get refreshed :started)
                                      (plist-get job :started)))
                    (error "Acknowledged job was not resumed"))
                  (unless (and (equal copilot-cs-id "different-codespace")
                               (equal copilot-cs-dir "/workspaces/different"))
                    (error "Reconnect changed the selected target"))))""",
            output="__COPILOT_CS_ACK_job-state-fixture__\nprogress\n",
        )

    def test_detached_job_does_not_claim_remote_execution_continues(self):
        self.check_job(
            """(let ((report (copilot-cs--report job)))
                (unless (and (string-match-p "state=detached" report)
                             (string-match-p "remote outcome is unconfirmed" report)
                             (string-match-p "job-state-fixture +detached"
                                              (copilot-cs-status)))
                  (error "Detached job outcome was misreported: %s" report)))""",
            output="__COPILOT_CS_ACK_job-state-fixture__\n",
        )


class DaemonReuseTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        sockets = self.root / "sockets"
        sockets.mkdir()
        self.unix_socket(sockets / "emacs-mcp-server-copilot-test0001.sock")
        self.calls = self.root / "client-calls"
        self.served = self.root / "served"
        self.env.update(
            {
                "COPILOT_AGENT_SESSION_ID": "test0001",
                "COPILOT_MCP_SOCKET_DIR": str(sockets),
                "FAKE_CLIENT_CALLS": str(self.calls),
                "FAKE_SERVED": str(self.served),
                "FAKE_FAIL_REFRESH": "",
            }
        )
        self.executable(
            "emacsclient",
            """import json, os, sys
with open(os.environ["FAKE_CLIENT_CALLS"], "a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ["FAKE_FAIL_REFRESH"] and "copilot-cs-jobs.el" in sys.argv[-1]:
    sys.exit(1)
""",
        )
        self.executable("emacs", "raise SystemExit('unexpected daemon replacement')\n")
        self.executable(
            "socat",
            """import os
from pathlib import Path
Path(os.environ["FAKE_SERVED"]).touch()
""",
        )

    def invoke(self):
        return self.run_command(["bash", str(SETUP / "copilot-emacs-mcp")])

    def test_reconnect_refreshes_runner_and_preserves_session_state(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.served.exists())
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        refresh = next(call[-1] for call in calls if "copilot-cs-jobs.el" in call[-1])
        # Execute the bridge's actual refresh form in a real, isolated Emacs.
        self.env["PATH"] = os.environ["PATH"]
        self.emacs(
            f"""(progn
              (setq copilot-mcp-setup-dir {json.dumps(str(SETUP))}
                    copilot-cs-id "original-codespace"
                    copilot-cs-dir "/workspaces/original"
                    copilot-cs-configured t)
              (puthash "original-job" '(:id "original-job") copilot-cs--jobs)
              (fset 'copilot-cs--login-shell-command (lambda (_) "stale"))
              {refresh}
              (unless (and (equal copilot-cs-id "original-codespace")
                           (equal copilot-cs-dir "/workspaces/original")
                           copilot-cs-configured
                           (gethash "original-job" copilot-cs--jobs)
                           (string-prefix-p "bash -lc "
                             (copilot-cs--login-shell-command "git push")))
                (error "Runner refresh lost state or retained stale code")))"""
        )

    def test_failed_refresh_does_not_serve_stale_runner(self):
        self.env["FAKE_FAIL_REFRESH"] = "1"
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not refresh runner", result.stderr)
        self.assertFalse(self.served.exists())


class JobCancellationTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.job = "job-regression"
        self.stream = None
        self.unrelated = None
        self.addCleanup(self.cleanup_processes)

    @staticmethod
    def live(pid):
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            text=True,
            capture_output=True,
            check=False,
        )
        return result.returncode == 0 and not result.stdout.strip().startswith("Z")

    def cleanup_processes(self):
        paths = [self.root / f"{self.job}.pid", *self.root.glob("worker-*.pid")]
        for path in paths:
            if path.exists():
                pid = int(path.read_text())
                if self.live(pid):
                    command = subprocess.run(
                        ["ps", "-ww", "-p", str(pid), "-o", "args="],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    if str(self.root) in command.stdout or self.job in command.stdout:
                        os.kill(pid, signal.SIGKILL)
        for process in (self.stream, self.unrelated):
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

    def runner_command(self, function, *arguments):
        args = " ".join(json.dumps(argument) for argument in arguments)
        return self.emacs(
            f"""(let ((copilot-cs-dir {json.dumps(str(self.root))})
                     (copilot-cs-remote-dir
                       (shell-quote-argument {json.dumps(str(self.root))})))
                   (princ ({function} {args})))"""
        )

    def start_tree(self, ignore_term=False):
        worker = self.root / "tree.py"
        worker.write_text(
            """import os, signal, subprocess, sys, time
from pathlib import Path
depth = int(sys.argv[1])
def child_changed(_signal, _frame):
    try:
        pid, status = os.waitpid(-1, os.WNOHANG | os.WUNTRACED)
    except ChildProcessError:
        return
    if pid and os.WIFSTOPPED(status):
        sys.exit(128)
signal.signal(signal.SIGCHLD, child_changed)
if sys.argv[2] == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if depth:
    subprocess.Popen([sys.executable, __file__, str(depth - 1), sys.argv[2]])
Path(__file__).with_name(f"worker-{depth}.pid").write_text(str(os.getpid()))
while True:
    time.sleep(0.1)
""",
            encoding="utf-8",
        )
        command = shlex.join(
            [sys.executable, str(worker), "2", "ignore" if ignore_term else "default"]
        )
        launcher = self.runner_command("copilot-cs--launcher", self.job, command)
        self.stream = subprocess.Popen(
            ["sh", "-c", launcher],
            cwd=self.root,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=self.root,
            env=self.env,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            files = list(self.root.glob("worker-*.pid"))
            if len(files) == 3 and all(path.read_text() for path in files):
                return [int(path.read_text()) for path in files]
            time.sleep(0.05)
        self.fail("fixture descendants did not start")

    def cancel(self):
        command = self.runner_command("copilot-cs--stop-command", self.job)
        return self.run_command(["sh", "-c", command], timeout=25)

    def assert_tree_stopped(self, pids, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no live descendants remain", result.stdout)
        self.assertTrue(all(not self.live(pid) for pid in pids))
        self.assertIsNone(self.unrelated.poll(), "unrelated process was signalled")
        deadline = time.monotonic() + 3
        marker = f"__COPILOT_CS_DONE_{self.job}__:"
        while time.monotonic() < deadline:
            text = (self.root / f"{self.job}.log").read_text()
            if marker in text:
                break
            time.sleep(0.05)
        self.assertIn(marker, text, "supervisor did not record an exit code")
        self.assertNotIn(marker + "128", text, "a stopped child ended its parent's wait")
        self.assertFalse((self.root / f"{self.job}.stop-lock").exists())

    def test_stop_terminates_children_and_grandchildren(self):
        pids = self.start_tree()
        self.assert_tree_stopped(pids, self.cancel())

    def test_stop_escalates_when_descendants_ignore_sigterm(self):
        pids = self.start_tree(ignore_term=True)
        result = self.cancel()
        self.assertIn("escalating to SIGKILL", result.stdout)
        self.assert_tree_stopped(pids, result)

    def test_failed_snapshot_resumes_supervisor(self):
        pids = self.start_tree()
        real_ps = shutil.which("ps")
        self.env["FAKE_PS_FAIL"] = "1"
        self.executable(
            "ps",
            f"""import os, sys
if sys.argv[1] == "-e" and os.environ["FAKE_PS_FAIL"] == "1":
    sys.exit(2)
os.execv({real_ps!r}, ["ps", *sys.argv[1:]])
""",
        )
        result = self.cancel()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not inspect the job process tree", result.stderr)
        supervisor = (self.root / f"{self.job}.pid").read_text().strip()
        state = self.run_command([real_ps, "-p", supervisor, "-o", "stat="])
        self.assertEqual(state.returncode, 0, state.stderr)
        self.assertNotIn("T", state.stdout, "supervisor was left suspended")
        self.assertTrue(all(self.live(pid) for pid in pids))
        self.assertFalse((self.root / f"{self.job}.stop-lock").exists())
        self.env["FAKE_PS_FAIL"] = "0"
        self.assert_tree_stopped(pids, self.cancel())

    def test_completed_job_never_signals_a_reused_pid(self):
        (self.root / f"{self.job}.pid").write_text(str(os.getpid()))
        (self.root / f"{self.job}.log").write_text(
            f"__COPILOT_CS_DONE_{self.job}__:0\n"
        )
        result = self.cancel()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already finished", result.stdout)

    def test_completion_marker_must_match_exact_job_id(self):
        self.job = "job.regression"
        (self.root / f"{self.job}.pid").write_text("1\n")
        (self.root / f"{self.job}.log").write_text(
            "__COPILOT_CS_DONE_jobXregression__:0\n"
        )
        result = self.cancel()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid supervisor pid", result.stderr)

    def test_foreign_supervisor_pid_is_rejected(self):
        (self.root / f"{self.job}.pid").write_text(str(os.getpid()))
        result = self.cancel()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not identify this job", result.stderr)

    def test_missing_supervisor_is_not_reported_as_success(self):
        result = self.cancel()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("job has no supervisor pid", result.stderr)

    def test_invalid_supervisor_pid_is_rejected(self):
        (self.root / f"{self.job}.pid").write_text("1\n")
        result = self.cancel()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid supervisor pid", result.stderr)

    def test_stop_uses_original_job_target_after_switching_codespaces(self):
        self.emacs(
            """(progn
              (require 'cl-lib)
              (setq copilot-cs-id "new-codespace" copilot-cs-dir "/workspaces/new")
              (puthash "original" '(:id "original" :cs "old-codespace"
                                   :dir "/workspaces/old") copilot-cs--jobs)
              (cl-letf (((symbol-function 'copilot-cs--live) #'identity)
                        ((symbol-function 'copilot-cs-sh)
                         (lambda (_command _wait)
                           (list copilot-cs-id copilot-cs-dir))))
                (unless (equal (copilot-cs-stop "original")
                               '("old-codespace" "/workspaces/old"))
                  (error "Cancellation targeted the new Codespace"))))"""
        )


if __name__ == "__main__":
    unittest.main()
