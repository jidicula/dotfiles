"""Regression tests for Git configuration and Codespace runner helpers.

Run with: python3 -m unittest discover -s skills/codespace-tramp/tests -v
Only Python's standard library, Emacs, Git, and system shell tools are used.
"""

import fcntl
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


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
REFRESH_ERROR = (
    'getting full codespace details: error making request: Get '
    '"https://api.github.com/user/codespaces/test-codespace?internal=true&refresh=true": '
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

    def load_script(self, name):
        loader = importlib.machinery.SourceFileLoader(name, str(SETUP / name))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
        loader.exec_module(module)
        return module

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

    def test_automatic_tag_signing_is_local_only(self):
        self.install()
        self.initialize_repo()
        self.assertEqual(self.git("config", "--get", "tag.gpgsign"), "true")
        self.local_config.unlink()
        self.assertEqual(self.git("config", "--get", "commit.gpgsign"), "true")
        self.git("-c", "commit.gpgsign=false", "commit", "--quiet",
                 "--allow-empty", "-m", "fixture")
        self.git("tag", "-a", "fixture", "-m", "ordinary annotation")
        self.assertNotIn("BEGIN PGP SIGNATURE", self.git("cat-file", "tag", "fixture"))


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
        public.write_text("ssh-ed25519 AAAA transport-fixture\n", encoding="utf-8")
        signing = self.root / "signing.pub"
        signing.write_text("ssh-ed25519 BBBB endorsement-fixture\n", encoding="utf-8")
        self.env.update(
            {
                "COPILOT_SECRETIVE_AGENT_SOCKET": str(agent),
                "COPILOT_SECRETIVE_PUBLIC_KEY": str(public),
                "COPILOT_SECRETIVE_SIGNING_PUBLIC_KEY": str(signing),
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
if os.environ.get("FAKE_GH_AUTHENTICATED"):
    print('Authenticated to localhost ([127.0.0.1]:1234) using "publickey".', file=sys.stderr, flush=True)
if os.environ.get("FAKE_GH_STDOUT"):
    print(os.environ["FAKE_GH_STDOUT"], flush=True)
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
        self.assertIn("PreferredAuthentications=publickey", calls[0])
        self.assertIn("BatchMode=yes", calls[0])
        self.assertEqual(calls[0][calls[0].index("-F") + 1], "/dev/null")
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
        self.env["FAKE_GH_ERROR"] = (
            self.rpc_error + "\nshell closed: exit status 1"
        )
        result = self.invoke("ssh", "test-codespace", "true; git push")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_interactive_connection_is_never_replayed(self):
        result = self.invoke("ssh", "test-codespace")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_copy_is_never_replayed(self):
        self.env["FAKE_GH_ERROR"] = "scp: Connection closed\nshell closed: exit status 1"
        result = self.invoke("cp", "test-codespace", "local-file", "remote:/tmp/file")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_first_task_recovers_before_ssh_dispatch(self):
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0][-1], "run-once")
        self.assertEqual(self.sleeps.read_text().splitlines(), ["5"])

    def test_copy_recovers_before_scp_dispatch(self):
        result = self.invoke("cp", "test-codespace", "local-file", "remote:/tmp/file")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.recorded_calls()), 2)


class UnavailableWarmupTests(WarmupTests):
    rpc_error = UNAVAILABLE_ERROR


class ApiConnectionTests(WarmupTests):
    rpc_error = (
        "error connecting to api.github.com\n"
        "check your internet connection or https://githubstatus.com"
    )

    def test_metadata_get_retries_without_secretive_or_mutating_flags(self):
        result = self.run_command(
            [sys.executable, str(SETUP / "copilot-gh-retry"), "get",
             "repos/example/project/codespaces/machines", "--jq", ".machines"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], [
            "api", "--method", "GET", "repos/example/project/codespaces/machines",
            "--jq", ".machines",
        ])
        self.assertEqual(calls[0], calls[1])

    def test_metadata_get_rejects_mutating_method_override(self):
        result = self.run_command(
            [sys.executable, str(SETUP / "copilot-gh-retry"), "get",
             "repos/example/project", "--method", "POST"]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_http_errors_are_not_retried(self):
        for status in (401, 403, 404, 410, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.env["FAKE_GH_ERROR"] = f"gh: unavailable (HTTP {status})"
                self.env["FAKE_GH_FAILURES"] = "100"
                before = len(self.recorded_calls()) if self.calls.exists() else 0
                result = self.run_command(
                    [sys.executable, str(SETUP / "copilot-gh-retry"), "get", "user"]
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(len(self.recorded_calls()), before + 1)

    def test_metadata_get_retries_tls_timeout_and_eof_without_replaying_connect(self):
        for diagnostic in ("net/http: TLS handshake timeout", "unexpected EOF"):
            error = f'Get "https://api.github.com/user/codespaces": {diagnostic}'
            with self.subTest(error=error):
                self.calls.unlink(missing_ok=True)
                self.env["FAKE_GH_ERROR"] = error
                result = self.run_command(
                    [sys.executable, str(SETUP / "copilot-gh-retry"),
                     "get", "user/codespaces"]
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(self.recorded_calls()), 2)
                self.calls.unlink()
                result = self.invoke("ssh", "test-codespace", "run-once")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(len(self.recorded_calls()), 1)

    def test_codespace_refresh_retries_before_ssh_and_copy_dispatch(self):
        for diagnostic in ("net/http: TLS handshake timeout", "unexpected EOF"):
            for arguments in (
                ("ssh", "test-codespace", "run-once"),
                ("cp", "test-codespace", "local-file", "remote:/tmp/file"),
            ):
                with self.subTest(diagnostic=diagnostic, mode=arguments[0]):
                    self.calls.unlink(missing_ok=True)
                    self.env["FAKE_GH_ERROR"] = REFRESH_ERROR + diagnostic
                    result = self.invoke(*arguments)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(len(self.recorded_calls()), 2)
                    self.assertEqual(*self.recorded_calls())


class CodespaceDetailsServerErrorTests(WarmupTests):
    rpc_error = (
        "getting full codespace details: error making request: "
        "received response with status code 500"
    )

    def test_stdout_prevents_http_500_retry(self):
        for output in ("__COPILOT_CS_ACK_job-fixture__", "_", "payload"):
            with self.subTest(output=output):
                self.calls.unlink(missing_ok=True)
                self.env["FAKE_GH_STDOUT"] = output
                result = self.invoke("ssh", "test-codespace", "run-once")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, output + "\n")
                self.assertEqual(len(self.recorded_calls()), 1)
                self.assertFalse(self.sleeps.exists())

    def test_authentication_prevents_http_500_retry(self):
        self.env["FAKE_GH_AUTHENTICATED"] = "1"
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.recorded_calls()), 1)
        self.assertFalse(self.sleeps.exists())

    def test_other_codespace_details_http_errors_are_not_retried(self):
        for status in (401, 403, 404, 410, 429, 501, 502, 503, 504, 5000):
            with self.subTest(status=status):
                self.calls.unlink(missing_ok=True)
                self.env["FAKE_GH_ERROR"] = self.rpc_error.replace("500", str(status))
                result = self.invoke("ssh", "test-codespace", "run-once")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(len(self.recorded_calls()), 1)
                self.assertFalse(self.sleeps.exists())


class ConnectionSafetyTests(SecretiveTransportTestCase):
    def test_default_identity_is_separate_from_the_work_signing_alias(self):
        self.env.pop("COPILOT_SECRETIVE_STANDIN")
        self.env["FAKE_GH_FAILURES"] = "0"
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertEqual(result.returncode, 0, result.stderr)
        setting = self.run_command(
            ["git", "config", "--file", str(DOTFILES / "gitconfig-work"),
             "--path", "--get", "user.signingkey"]
        )
        self.assertEqual(setting.returncode, 0, setting.stderr)
        public = Path(setting.stdout.strip())
        self.assertEqual(
            public, self.root / ".ssh" / "secretive-stormbreaker-github-sep-2026.pub",
        )
        standin = self.root / ".ssh" / "secretive-codespaces-agent-sep-2026"
        for path in (standin, Path(str(standin) + ".pub")):
            self.assertTrue(path.is_symlink())
            self.assertEqual(
                path.resolve(), Path(self.env["COPILOT_SECRETIVE_PUBLIC_KEY"]).resolve(),
            )
        arguments = self.recorded_calls()[0]
        self.assertEqual(arguments[arguments.index("-i") + 1], str(standin))
        self.assertFalse(public.exists(), "transport created an endorsement alias")
        for name in ("secretive-codespaces", "secretive-codespaces.pub"):
            self.assertFalse(os.path.lexists(self.root / ".ssh" / name))

    def test_waiting_connection_cannot_replace_an_active_identity(self):
        self.env["FAKE_GH_FAILURES"] = "0"
        result = self.invoke("ssh", "test-codespace", "first")
        self.assertEqual(result.returncode, 0, result.stderr)
        standin = Path(self.env["COPILOT_SECRETIVE_STANDIN"])
        original = os.readlink(standin)
        second_key = self.root / "second-public.pub"
        second_key.write_text("ssh-ed25519 CCCC second-transport-fixture\n")
        environment = dict(self.env, COPILOT_SECRETIVE_PUBLIC_KEY=str(second_key))
        helper = self.load_script("copilot-gh-retry")
        process = None
        try:
            with helper.ConnectionGate(standin.parent / "copilot-ghcs-connect.lock"):
                process = subprocess.Popen(
                    ["bash", str(SETUP / "copilot-ghcs"), "ssh", "test-codespace", "second"],
                    env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                ready, _, _ = select.select([process.stderr], [], [], 5)
                self.assertTrue(ready, "second connection did not reach the signing gate")
                self.assertIn(b"waiting for another Secretive", process.stderr.readline())
                self.assertEqual(os.readlink(standin), original)
                self.assertEqual(os.readlink(str(standin) + ".pub"), original)
            _, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(os.readlink(standin), str(second_key.resolve()))
            self.assertEqual(os.readlink(str(standin) + ".pub"), str(second_key.resolve()))
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=10)

    def test_reusing_an_identity_does_not_recreate_its_links(self):
        self.env["FAKE_GH_FAILURES"] = "0"
        self.assertEqual(self.invoke("ssh", "test-codespace", "first").returncode, 0)
        standin = Path(self.env["COPILOT_SECRETIVE_STANDIN"])
        public = Path(str(standin) + ".pub")
        inodes = (standin.lstat().st_ino, public.lstat().st_ino)
        result = self.invoke("ssh", "test-codespace", "second")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((standin.lstat().st_ino, public.lstat().st_ino), inodes)

    def test_existing_regular_identity_is_not_overwritten(self):
        standin = Path(self.env["COPILOT_SECRETIVE_STANDIN"])
        standin.parent.mkdir()
        standin.symlink_to(self.env["COPILOT_SECRETIVE_PUBLIC_KEY"])
        public = Path(str(standin) + ".pub")
        public.write_text("unrelated owned fixture\n")
        previous = os.readlink(standin)
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to replace non-symlink", result.stderr)
        self.assertEqual(public.read_text(), "unrelated owned fixture\n")
        self.assertEqual(os.readlink(standin), previous)
        self.assertFalse(self.calls.exists())

    def test_public_standin_permission_warning_is_not_retried(self):
        self.env["FAKE_GH_ERROR"] = (
            "WARNING: UNPROTECTED PRIVATE KEY FILE!\n"
            "Permissions 0644 for 'secretive-codespaces' are too open\n"
            "Load key secretive-codespaces: bad permissions\n"
            "shell closed: exit status 255"
        )
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.recorded_calls()), 1)
        self.assertIn("bad permissions", result.stderr)
        self.assertFalse(self.sleeps.exists())

    def test_acknowledged_or_partial_output_is_never_replayed(self):
        for output in (b"__COPILOT_CS_ACK_job-fixture__\n", b"_", b"payload"):
            with self.subTest(output=output):
                self.calls.unlink(missing_ok=True)
                self.executable(
                    "gh",
                    f"""import os, sys
from pathlib import Path
Path(os.environ["FAKE_GH_CALLS"]).write_text("called\\n")
os.write(1, {output!r})
print(os.environ["FAKE_GH_ERROR"], file=sys.stderr)
sys.exit(17)
""",
                )
                result = self.invoke("ssh", "test-codespace", "run-once")
                self.assertEqual(result.returncode, 17)
                self.assertEqual(result.stdout.encode(), output)
                self.assertFalse(self.sleeps.exists())

    def test_only_complete_known_predispatch_errors_are_retried(self):
        helper = self.load_script("copilot-gh-retry")
        for error in (
            DEADLINE_ERROR, UNAVAILABLE_ERROR, ApiConnectionTests.rpc_error,
            REFRESH_ERROR + "net/http: TLS handshake timeout",
            REFRESH_ERROR + "unexpected EOF",
            CodespaceDetailsServerErrorTests.rpc_error,
        ):
            with self.subTest(error=error):
                self.assertTrue(helper.startup_failure(error.encode(), False))
                self.assertFalse(helper.startup_failure(error.encode(), True))
                self.assertFalse(helper.startup_failure(
                    (error + "\nshell closed: exit status 1").encode(), False
                ))
                self.assertFalse(helper.startup_failure(
                    (error + "\ntunnel closed: EOF").encode(), False
                ))
                self.assertFalse(helper.startup_failure(
                    ("remote task printed:\n" + error).encode(), False
                ))
        self.assertFalse(helper.startup_failure(b"", False))
        self.assertFalse(helper.startup_failure(
            b"Permission denied (publickey,password)", False
        ))
        self.assertFalse(helper.startup_failure(
            b"error connecting to use.rel.tunnels.api.visualstudio.com", False
        ))

    def test_read_only_get_still_rejects_partial_or_unrecognized_errors(self):
        helper = self.load_script("copilot-gh-retry")
        error = b'Get "https://api.github.com/user/codespaces": unexpected EOF'
        self.assertTrue(helper.startup_failure(error, False, api_get=True))
        self.assertFalse(helper.startup_failure(error, True, api_get=True))
        self.assertFalse(helper.startup_failure(
            error + b"\nshell closed: exit status 1", False, api_get=True
        ))
        self.assertFalse(helper.startup_failure(
            b'Get "https://example.invalid/path": unexpected EOF', False, api_get=True
        ))
        self.assertFalse(helper.startup_failure(
            b'Post "https://api.github.com/user/codespaces": unexpected EOF',
            False, api_get=True
        ))

    def test_connection_gate_times_out_without_running_gh(self):
        helper = self.load_script("copilot-gh-retry")
        path = self.root / "connection.lock"
        with helper.ConnectionGate(path):
            second = helper.ConnectionGate(path)
            with mock.patch.object(helper.time, "monotonic", side_effect=[0, 121]):
                with self.assertRaisesRegex(TimeoutError, "setup deadline"):
                    second.__enter__()
            self.assertIsNone(second.fd)
        self.assertFalse(self.calls.exists())

    def test_connection_gate_releases_after_failure(self):
        self.env["FAKE_GH_ERROR"] = "sign_and_send_pubkey: agent refused operation"
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertEqual(result.returncode, 1)
        lock = self.root / "ssh" / "copilot-ghcs-connect.lock"
        with lock.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertFalse(self.sleeps.exists())

    def test_streaming_releases_gate_before_job_completion(self):
        self.executable(
            "gh",
            """import os, time
from pathlib import Path
print("__COPILOT_CS_ACK_job-fixture__", flush=True)
while not Path(os.environ["FAKE_GH_CALLS"]).exists():
    time.sleep(0.01)
print("completed", flush=True)
""",
        )
        process = subprocess.Popen(
            ["bash", str(SETUP / "copilot-ghcs"), "ssh", "test-codespace", "run-once"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            ready, _, _ = select.select([process.stdout], [], [], 5)
            self.assertTrue(ready, "runner acknowledgement was buffered")
            self.assertEqual(process.stdout.readline(), b"__COPILOT_CS_ACK_job-fixture__\n")
            with (self.root / "ssh" / "copilot-ghcs-connect.lock").open("rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIsNone(process.poll(), "fixture exited before the gate check")
            self.calls.touch()
            output, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(output, b"completed\n")
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)

    def test_terminating_transport_stops_its_local_children(self):
        child_pid = self.root / "ssh-child.pid"
        self.env["FAKE_CHILD_PID"] = str(child_pid)
        self.executable(
            "gh",
            """import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path(os.environ["FAKE_CHILD_PID"]).write_text(str(child.pid))
print("__COPILOT_CS_ACK_job-fixture__", flush=True)
child.wait()
""",
        )
        process = subprocess.Popen(
            ["bash", str(SETUP / "copilot-ghcs"), "ssh", "test-codespace", "run-once"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            ready, _, _ = select.select([process.stdout], [], [], 5)
            self.assertTrue(ready)
            self.assertEqual(process.stdout.readline(), b"__COPILOT_CS_ACK_job-fixture__\n")
            pid = int(child_pid.read_text())
            process.terminate()
            _, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 143, error)
            deadline = time.monotonic() + 2
            while JobCancellationTests.live(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(JobCancellationTests.live(pid), "SSH child survived the stream")
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)
            if child_pid.exists():
                pid = int(child_pid.read_text())
                if JobCancellationTests.live(pid):
                    os.kill(pid, signal.SIGKILL)


class IdentityPinningTests(SecretiveTransportTestCase):
    def setUp(self):
        super().setUp()
        self.public_key = Path(self.env["COPILOT_SECRETIVE_PUBLIC_KEY"])
        self.agent_calls = self.root / "agent-calls"
        self.gate_path = (
            Path(self.env["COPILOT_SECRETIVE_STANDIN"]).parent
            / "copilot-ghcs-connect.lock"
        )
        self.env.update(
            FAKE_AGENT_CALLS=str(self.agent_calls),
            FAKE_GH_FAILURES="0",
        )
        self.executable(
            "ssh-add",
            """import os, sys
from pathlib import Path
Path(os.environ["FAKE_AGENT_CALLS"]).write_text("queried\\n")
raise SystemExit("automatic agent identity discovery is forbidden")
""",
        )

    def test_configured_identity_is_pinned_without_querying_agent(self):
        before = self.public_key.stat()
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.agent_calls.exists())
        self.assertEqual(len(self.recorded_calls()), 1)
        standin = Path(self.env["COPILOT_SECRETIVE_STANDIN"])
        self.assertEqual(standin.resolve(), self.public_key.resolve())
        self.assertEqual(Path(str(standin) + ".pub").resolve(), self.public_key.resolve())
        after = self.public_key.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns, before.st_mode),
                         (after.st_ino, after.st_mtime_ns, after.st_mode))

    def test_alias_preparation_waits_for_the_signing_gate(self):
        helper = self.load_script("copilot-gh-retry")
        self.gate_path.parent.mkdir()
        process = None
        try:
            with helper.ConnectionGate(self.gate_path):
                process = subprocess.Popen(
                    ["bash", str(SETUP / "copilot-ghcs"), "ssh", "test-codespace", "run-once"],
                    env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                ready, _, _ = select.select([process.stderr], [], [], 5)
                self.assertTrue(ready, "connection did not queue at the signing gate")
                self.assertIn(b"waiting for another Secretive", process.stderr.readline())
                self.assertFalse(self.agent_calls.exists())
            _, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, error)
            self.assertFalse(self.agent_calls.exists())
            self.assertEqual(Path(self.env["COPILOT_SECRETIVE_STANDIN"]).resolve(),
                             self.public_key.resolve())
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=10)

    def test_missing_alias_never_falls_back_to_an_agent_key(self):
        self.env.pop("COPILOT_SECRETIVE_PUBLIC_KEY")
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No such file", result.stderr)
        self.assertFalse(self.agent_calls.exists())
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.sleeps.exists())

    def test_existing_default_alias_does_not_require_discovery(self):
        standin = Path(self.env["COPILOT_SECRETIVE_STANDIN"])
        standin.parent.mkdir()
        standin.symlink_to(self.public_key)
        Path(str(standin) + ".pub").symlink_to(self.public_key)
        self.env.pop("COPILOT_SECRETIVE_PUBLIC_KEY")
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.agent_calls.exists())

    def test_paused_authentication_does_not_query_the_agent(self):
        self.gate_path.parent.mkdir()
        self.gate_path.write_text("3")
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertFalse(self.agent_calls.exists())
        self.assertFalse(self.calls.exists())

    def test_missing_endorsement_key_fails_closed(self):
        Path(self.env["COPILOT_SECRETIVE_SIGNING_PUBLIC_KEY"]).unlink()
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No such file", result.stderr)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.sleeps.exists())

    def test_transport_cannot_use_the_endorsement_key(self):
        self.env["COPILOT_SECRETIVE_PUBLIC_KEY"] = self.env["COPILOT_SECRETIVE_SIGNING_PUBLIC_KEY"]
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different Secretive keys", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_transport_cannot_repoint_an_endorsement_alias(self):
        signing = Path(self.env["COPILOT_SECRETIVE_SIGNING_PUBLIC_KEY"])
        alias = Path(self.env["COPILOT_SECRETIVE_STANDIN"])
        alias.parent.mkdir()
        alias.symlink_to(signing)
        Path(str(alias) + ".pub").symlink_to(signing)
        before = signing.read_bytes()
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("overlaps the protected signing identity", result.stderr)
        self.assertEqual(alias.resolve(), signing.resolve())
        self.assertEqual(signing.read_bytes(), before)
        self.assertFalse(self.calls.exists())


class AuthenticationLimitTests(SecretiveTransportTestCase):
    def setUp(self):
        super().setUp()
        self.env["FAKE_GH_FAILURES"] = "20"
        self.env["FAKE_GH_ERROR"] = "sign_and_send_pubkey: agent refused operation"
        self.gate_path = (
            Path(self.env["COPILOT_SECRETIVE_STANDIN"]).parent
            / "copilot-ghcs-connect.lock"
        )

    def assert_queued_authentication_limit(self):
        helper = self.load_script("copilot-gh-retry")
        self.gate_path.parent.mkdir()
        processes = []
        try:
            with helper.ConnectionGate(self.gate_path):
                for index in range(4):
                    process = subprocess.Popen(
                        ["bash", str(SETUP / "copilot-ghcs"),
                         "ssh", f"codespace-{index}", "run-once"],
                        env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    processes.append(process)
                    ready, _, _ = select.select([process.stderr], [], [], 5)
                    self.assertTrue(ready, "connection did not queue at the signing gate")
                    self.assertIn(b"waiting for another Secretive", process.stderr.readline())
            errors = [process.communicate(timeout=10)[1] for process in processes]
            self.assertEqual(len(self.recorded_calls()), 3)
            self.assertEqual(sorted(process.returncode for process in processes), [1, 1, 1, 75])
            self.assertTrue(any(b"operator approval" in error for error in errors))
            self.assertEqual(self.gate_path.read_text(), "3")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=10)

    def test_queued_connections_stop_after_three_refusals(self):
        self.assert_queued_authentication_limit()

    def test_queued_connections_stop_after_three_terminal_denials(self):
        self.env["FAKE_GH_ERROR"] = "Permission denied (publickey,password)"
        self.assert_queued_authentication_limit()

    def assert_shared_authentication_limit(self):
        for mode, arguments in (
            ("ssh", ("run-once",)),
            ("cp", ("fixture", "remote:/tmp/fixture")),
            ("ssh", ("run-once",)),
        ):
            result = self.invoke(mode, "test-codespace", *arguments)
            self.assertEqual(result.returncode, 1, result.stderr)
        for mode, arguments in (
            ("cp", ("fixture", "remote:/tmp/fixture")),
            ("ssh-sign", ("run-once",)),
            ("ssh", ("--job-id", "job-fixture", "run-once")),
        ):
            result = self.invoke(mode, "other-codespace", *arguments)
            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertIn("operator approval", result.stderr)
        self.assertEqual(len(self.recorded_calls()), 3)
        self.assertEqual(self.gate_path.read_text(), "3")
        self.assertFalse(self.sleeps.exists())

    def test_copies_and_commands_share_the_refusal_limit(self):
        self.assert_shared_authentication_limit()

    def test_copies_and_commands_share_the_terminal_denial_limit(self):
        self.env["FAKE_GH_ERROR"] = (
            "WARNING: UNPROTECTED PRIVATE KEY FILE!\n"
            'Load key "fixture": bad permissions\n'
            "Permission denied (publickey,password)"
        )
        self.assert_shared_authentication_limit()

    def test_terminal_denials_count_once_per_failed_connection(self):
        self.gate_path.parent.mkdir()
        errors = (
            "Permission denied (publickey,password)",
            "codespace@localhost: Permission denied (publickey).",
            "codespace@[::1]: Permission denied (publickey,gssapi-keyex,gssapi-with-mic).",
            "sign_and_send_pubkey: agent refused operation\n"
            "Permission denied (publickey,password)\n"
            "Permission denied (publickey,password)",
        )
        for error in errors:
            with self.subTest(error=error):
                self.gate_path.write_text("0")
                self.env["FAKE_GH_ERROR"] = error
                result = self.invoke("ssh", "test-codespace", "run-once")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(self.gate_path.read_text(), "1")
                self.assertIn("authentication failure 1/3", result.stderr)
        self.assertEqual(len(self.recorded_calls()), len(errors))
        self.assertFalse(self.sleeps.exists())

    def test_fragmented_authentication_errors_survive_stderr_capture_overflow(self):
        self.gate_path.parent.mkdir()
        errors = (
            b"codespace@localhost: Permission denied (publickey,password).",
            b"sign_and_send_pubkey: agent refused operation",
        )
        for error in errors:
            for overflow in (False, True):
                for authenticated in (False, True):
                    with self.subTest(
                        error=error, overflow=overflow, authenticated=authenticated,
                    ):
                        self.gate_path.write_text("2")
                        self.executable(
                            "gh",
                            f"""import json, os, sys, time
with open(os.environ["FAKE_GH_CALLS"], "a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
os.write(2, b"setup diagnostic\\n" * {5000 if overflow else 0})
if {authenticated!r}:
    print('Authenticated to localhost ([127.0.0.1]:1234) using "publickey".', file=sys.stderr, flush=True)
os.write(2, {error[:12]!r})
time.sleep(0.05)
os.write(2, {error[12:]!r})
sys.exit(1)
""",
                        )
                        result = self.invoke("ssh", "test-codespace", "run-once")
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertEqual(
                            self.gate_path.read_text(), "0" if authenticated else "3",
                        )
                        if not authenticated:
                            self.assertIn("authentication failure 3/3", result.stderr)
        self.assertEqual(len(self.recorded_calls()), 8)
        self.assertFalse(self.sleeps.exists())

    def test_successful_exit_does_not_count_a_terminal_denial(self):
        self.gate_path.parent.mkdir()
        self.gate_path.write_text("2")
        self.executable(
            "gh",
            'import sys\nprint("Permission denied (publickey).", file=sys.stderr)\n',
        )
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.gate_path.read_text(), "0")

    def test_communication_failures_count_toward_the_same_limit(self):
        self.env["FAKE_GH_ERROR"] = (
            'sign_and_send_pubkey: signing failed for ECDSA "fixture" from agent: '
            "communication with agent failed"
        )
        for _ in range(3):
            self.assertEqual(self.invoke("ssh", "test-codespace", "run-once").returncode, 1)
        self.assertEqual(self.invoke("ssh", "test-codespace", "blocked").returncode, 75)
        self.assertEqual(len(self.recorded_calls()), 3)

    def test_approved_resume_does_not_replay_commands_or_require_an_agent(self):
        for _ in range(3):
            self.assertEqual(self.invoke("ssh", "test-codespace", "run-once").returncode, 1)
        inode = self.gate_path.stat().st_ino
        refused = self.invoke("resume-auth")
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.gate_path.read_text(), "3")
        self.env["COPILOT_SECRETIVE_AGENT_SOCKET"] = str(self.root / "absent.sock")
        resumed = self.invoke("resume-auth", "--operator-approved")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn("not replayed", resumed.stderr)
        self.assertEqual(self.gate_path.read_text(), "0")
        self.assertEqual(self.gate_path.stat().st_ino, inode)
        self.assertEqual(len(self.recorded_calls()), 3)
        self.env["COPILOT_SECRETIVE_AGENT_SOCKET"] = str(self.root / "agent.sock")
        result = self.invoke("ssh", "test-codespace", "new-command")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.recorded_calls()), 4)
        self.assertEqual(self.gate_path.read_text(), "1")

    def test_approved_resume_waits_for_the_existing_gate(self):
        helper = self.load_script("copilot-gh-retry")
        self.gate_path.parent.mkdir()
        self.gate_path.write_text("3")
        process = None
        try:
            with helper.ConnectionGate(self.gate_path, allow_paused=True):
                process = subprocess.Popen(
                    ["bash", str(SETUP / "copilot-ghcs"),
                     "resume-auth", "--operator-approved"],
                    env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                ready, _, _ = select.select([process.stderr], [], [], 5)
                self.assertTrue(ready, "resume did not queue at the signing gate")
                self.assertIn(b"waiting for another Secretive", process.stderr.readline())
                self.assertEqual(self.gate_path.read_text(), "3")
            _, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(self.gate_path.read_text(), "0")
            self.assertFalse(self.calls.exists())
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=10)

    def test_successful_connection_resets_previous_refusals(self):
        self.assertEqual(self.invoke("ssh", "test-codespace", "first").returncode, 1)
        self.env["FAKE_GH_FAILURES"] = "0"
        self.assertEqual(self.invoke("ssh", "test-codespace", "success").returncode, 0)
        self.assertEqual(self.gate_path.read_text(), "0")
        self.env["FAKE_GH_FAILURES"] = "20"
        for _ in range(3):
            self.assertEqual(self.invoke("ssh", "test-codespace", "next").returncode, 1)
        self.assertEqual(self.invoke("ssh", "test-codespace", "blocked").returncode, 75)
        self.assertEqual(len(self.recorded_calls()), 5)

    def test_remote_signing_errors_do_not_count_as_connection_refusals(self):
        self.assertEqual(self.invoke("ssh", "test-codespace", "first").returncode, 1)
        self.env["FAKE_GH_AUTHENTICATED"] = "1"
        for error in (
            "sign_and_send_pubkey: agent refused operation",
            "Permission denied (publickey,password)",
        ):
            self.env["FAKE_GH_ERROR"] = error
            for _ in range(4):
                self.assertEqual(
                    self.invoke("ssh", "test-codespace", "remote-command").returncode, 1,
                )
        self.assertEqual(self.gate_path.read_text(), "0")
        self.assertEqual(len(self.recorded_calls()), 9)

    def test_remote_output_resets_previous_connection_refusals(self):
        self.assertEqual(self.invoke("ssh", "test-codespace", "first").returncode, 1)
        self.env["FAKE_GH_STDOUT"] = "remote output"
        self.assertEqual(self.invoke("ssh", "test-codespace", "remote-command").returncode, 1)
        self.assertEqual(self.gate_path.read_text(), "0")

    def test_an_older_authenticated_stream_cannot_clear_later_refusals(self):
        helper = self.load_script("copilot-gh-retry")
        self.gate_path.parent.mkdir()
        with helper.ConnectionGate(self.gate_path) as older:
            older.authenticated()
            for _ in range(3):
                self.assertEqual(self.invoke("ssh", "test-codespace", "run-once").returncode, 1)
            older.authenticated()
            older.refused()
        self.assertEqual(self.gate_path.read_text(), "3")
        self.assertEqual(self.invoke("ssh", "test-codespace", "blocked").returncode, 75)
        self.assertEqual(len(self.recorded_calls()), 3)

    def test_protocol_and_permission_errors_do_not_count_as_refusals(self):
        errors = (
            "Agent returned SSH_AGENT_FAILURE",
            "HTTP 403: Must have admin rights to Repository",
            "WARNING: UNPROTECTED PRIVATE KEY FILE!",
            "cp: /tmp/fixture: Permission denied",
            "Permission denied, please try again.",
            "HTTP 403: Permission denied (publickey).",
            "debug1: Permission denied (publickey).",
        )
        self.env["FAKE_GH_FAILURES"] = str(4 * len(errors))
        for error in errors:
            with self.subTest(error=error):
                self.env["FAKE_GH_ERROR"] = error
                for _ in range(4):
                    self.assertEqual(self.invoke("ssh", "test-codespace", "run-once").returncode, 1)
        self.assertEqual(len(self.recorded_calls()), 4 * len(errors))
        self.assertIn(self.gate_path.read_text(), ("", "0"))

    def test_corrupt_refusal_state_fails_closed_until_approved_resume(self):
        self.gate_path.parent.mkdir()
        self.gate_path.write_text("invalid state")
        result = self.invoke("ssh", "test-codespace", "run-once")
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn("invalid authentication state", result.stderr)
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.gate_path.read_text(), "invalid state")
        resumed = self.invoke("resume-auth", "--operator-approved")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self.gate_path.read_text(), "0")


class ConnectionDeadlineTests(SecretiveTransportTestCase):
    def command(self, mode="ssh", marker=None):
        arguments = [
            sys.executable, str(SETUP / "copilot-gh-retry"), "connect",
            "--lock", str(self.root / "connection.lock"),
            "--startup-timeout", "0.5",
        ]
        if marker is not None:
            arguments.extend(["--ack-marker", marker])
        return [*arguments, "--", "gh", "codespace", mode, "-c", "test-codespace"]

    def test_unacknowledged_connection_is_bounded_and_never_replayed(self):
        marker = "__COPILOT_CS_ACK_job-fixture__"
        for output in (
            "",
            "partial command output\n",
            marker[:-1] + "\n",
            "prefix " + marker + "\n",
            "__COPILOT_CS_ACK_other-job__\n",
        ):
            with self.subTest(output=output):
                self.executable(
                    "gh",
                    f"""import os, sys, time
from pathlib import Path
Path(os.environ["FAKE_GH_CALLS"]).write_text("called\\n")
print('Authenticated to localhost ([127.0.0.1]:1234) using "publickey".', file=sys.stderr, flush=True)
sys.stdout.write({output!r})
sys.stdout.flush()
time.sleep(30)
""",
                )
                started = time.monotonic()
                result = self.run_command(self.command(marker=marker), timeout=5)
                self.assertEqual(result.returncode, 124, result.stderr)
                self.assertLess(time.monotonic() - started, 2)
                self.assertIn("remote outcome is unconfirmed", result.stderr)
                self.assertNotIn("retrying (", result.stderr)
                self.assertEqual(result.stdout, output)
                self.assertEqual(self.calls.read_text(), "called\n")
                with (self.root / "connection.lock").open("rb") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_fragmented_exact_ack_keeps_a_long_running_job_alive(self):
        self.executable(
            "gh",
            """import os, time
os.write(1, b"__COPILOT_CS_ACK_")
time.sleep(0.05)
os.write(1, b"job-fixture__\\r\\n")
time.sleep(0.7)
print("completed", flush=True)
""",
        )
        result = self.run_command(self.command(marker="__COPILOT_CS_ACK_job-fixture__"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("completed", result.stdout)

    def test_slow_startup_reports_authentication_and_acknowledgement_without_replaying(self):
        self.executable(
            "gh",
            """import sys, time
time.sleep(0.22)
print('Authenticated to localhost ([127.0.0.1]:1234) using "publickey".', file=sys.stderr, flush=True)
time.sleep(0.22)
print("__COPILOT_CS_ACK_job-fixture__", flush=True)
time.sleep(0.7)
print("completed", flush=True)
""",
        )
        arguments = self.command(marker="__COPILOT_CS_ACK_job-fixture__")[1:]
        arguments[arguments.index("--startup-timeout") + 1] = "1"
        result = self.run_command([
            sys.executable, "-c",
            """import runpy, sys
helper = runpy.run_path(sys.argv[1])
sys.argv = sys.argv[1:]
helper["run_once"].__globals__["SETUP_PROGRESS_INTERVAL"] = 0.05
sys.exit(helper["main"]())
""", *arguments,
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "__COPILOT_CS_ACK_job-fixture__\ncompleted\n")
        self.assertIn("waiting for API/SSH connection setup", result.stderr)
        self.assertIn("SSH authenticated after", result.stderr)
        self.assertIn("waiting for the exact runner acknowledgement", result.stderr)
        self.assertIn("runner acknowledged after", result.stderr)
        self.assertNotIn("retrying (", result.stderr)

    def test_quiet_authenticated_copy_releases_gate_without_limiting_transfer_time(self):
        self.executable(
            "gh",
            """import os, sys, time
from pathlib import Path
sys.stderr.write('Authenticated to localhost ([127.0.0.1]:1234) ')
sys.stderr.flush()
time.sleep(0.05)
sys.stderr.write('using "publickey".\\n')
sys.stderr.flush()
while not Path(os.environ["FAKE_GH_CALLS"]).exists():
    time.sleep(0.01)
""",
        )
        process = subprocess.Popen(
            self.command(mode="cp"), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            ready, _, _ = select.select([process.stderr], [], [], 5)
            self.assertTrue(ready)
            self.assertIn(b'using "publickey".', process.stderr.readline())
            time.sleep(0.7)
            self.assertIsNone(process.poll(), "connection timeout killed an authenticated copy")
            with (self.root / "connection.lock").open("rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.calls.touch()
            output, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(output, b"")
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)

    def test_no_authentication_or_closed_output_cannot_wait_forever(self):
        for body in (
            "import time\ntime.sleep(30)\n",
            "import os, time\nos.close(1)\nos.close(2)\ntime.sleep(30)\n",
        ):
            with self.subTest(body=body):
                self.executable("gh", body)
                started = time.monotonic()
                result = self.run_command(self.command(mode="cp"), timeout=5)
                self.assertEqual(result.returncode, 124, result.stderr)
                self.assertLess(time.monotonic() - started, 2)
                self.assertNotIn("retrying (", result.stderr)

    def test_wrapper_forwards_ack_requirement_and_transport_keepalives(self):
        self.env["FAKE_GH_FAILURES"] = "0"
        result = self.invoke("ssh", "test-codespace", "--job-id", "job-fixture", "command")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded_calls()
        self.assertNotIn("--job-id", calls[0])
        for option in ("LogLevel=VERBOSE", "ServerAliveInterval=15", "ServerAliveCountMax=3"):
            self.assertIn(option, calls[0])
        self.assertEqual(calls[0][-1], "command")

    def test_invalid_timeout_is_rejected_before_dispatch(self):
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value):
                arguments = self.command()
                arguments[arguments.index("--startup-timeout") + 1] = value
                result = self.run_command(arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("finite and greater than zero", result.stderr)
                self.assertFalse(self.calls.exists())

    def test_queue_wait_uses_the_same_startup_budget(self):
        helper = self.load_script("copilot-gh-retry")
        with helper.ConnectionGate(self.root / "connection.lock"):
            started = time.monotonic()
            result = self.run_command(self.command(), timeout=5)
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("connection gate remained busy", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_timeout_stops_a_child_even_after_gh_has_exited(self):
        pid_file = self.root / "orphaned-ssh.pid"
        self.env["FAKE_CHILD_PID"] = str(pid_file)
        self.executable(
            "gh",
            """import os, subprocess, sys
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path(os.environ["FAKE_CHILD_PID"]).write_text(str(child.pid))
""",
        )
        try:
            result = self.run_command(self.command(), timeout=5)
            self.assertEqual(result.returncode, 124, result.stderr)
            self.assertFalse(JobCancellationTests.live(int(pid_file.read_text())))
        finally:
            if pid_file.exists():
                pid = int(pid_file.read_text())
                if JobCancellationTests.live(pid):
                    os.kill(pid, signal.SIGKILL)


class McpFallbackTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.client = self.bin / "copilot-emacs-mcp-call"
        shutil.copyfile(SETUP / "copilot-emacs-mcp-call", self.client)
        self.env["COPILOT_MCP_CALL_TIMEOUT"] = "1"
        self.env["COPILOT_MCP_QUEUE_TIMEOUT"] = "3"

    def invoke(self, expression="(copilot-cs-status)"):
        return self.run_command([sys.executable, str(self.client), expression])

    def test_handshake_and_coalesced_notifications_preserve_the_response(self):
        self.executable(
            "copilot-emacs-mcp",
            """import json, os, sys
initialization = json.loads(sys.stdin.readline())
assert initialization["method"] == "initialize"
notification = {"jsonrpc": "2.0", "method": "notifications/message", "params": {}}
def respond(identifier, result):
    response = {"jsonrpc": "2.0", "id": identifier, "result": result}
    os.write(1, (json.dumps(notification) + "\\n" + json.dumps(response) + "\\n").encode())
respond(initialization["id"], {"protocolVersion": "2024-11-05", "capabilities": {}})
assert json.loads(sys.stdin.readline())["method"] == "notifications/initialized"
call = json.loads(sys.stdin.readline())
assert call["method"] == "tools/call"
assert call["params"] == {"name": "eval-elisp", "arguments": {"expression": "(copilot-cs-status)"}}
respond(call["id"], {"content": [{"type": "text", "text": "fixture jobs"}]})
sys.stdin.read()
""",
        )
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "fixture jobs")

    def test_foreign_and_late_responses_cannot_become_another_invocations_receipt(self):
        self.executable(
            "copilot-emacs-mcp",
            """import json, sys
from pathlib import Path
def respond(identifier, text):
    print(json.dumps({"jsonrpc": "2.0", "id": identifier, "result": {
        "content": [{"type": "text", "text": text}]}}), flush=True)
initialization = json.loads(sys.stdin.readline())
respond(initialization["id"], "initialized")
sys.stdin.readline()
call = json.loads(sys.stdin.readline())
record = Path.home() / "request-ids"
previous = json.loads(record.read_text()) if record.exists() else []
for identifier in [2, *previous]:
    respond(identifier, "another invocation's job")
record.write_text(json.dumps([initialization["id"], call["id"]]))
respond(call["id"], call["params"]["arguments"]["expression"])
sys.stdin.read()
""",
        )
        identifiers = []
        for expression in ("first receipt", "second receipt"):
            result = self.invoke(expression)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expression)
            identifiers.extend(json.loads((self.root / "request-ids").read_text()))
        self.assertEqual(len(set(identifiers)), 4)

    def test_partial_frame_obeys_timeout(self):
        self.env["COPILOT_MCP_CALL_TIMEOUT"] = "0.2"
        self.executable(
            "copilot-emacs-mcp",
            """import os, sys, time
sys.stdin.readline()
os.write(1, b'{"jsonrpc":')
time.sleep(10)
""",
        )
        started = time.monotonic()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out after 0.2s", result.stderr)
        self.assertLess(time.monotonic() - started, 2)

    def test_nonfinite_timeout_is_rejected_before_starting_bridge(self):
        for name in ("COPILOT_MCP_CALL_TIMEOUT", "COPILOT_MCP_QUEUE_TIMEOUT"):
            for value in ("NaN", "inf", "-inf", "0", "-1", "invalid"):
                with self.subTest(name=name, value=value):
                    self.env[name] = value
                    result = self.invoke()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(name + " must be", result.stderr)
            self.env[name] = "1"

    def test_tool_error_is_not_reported_as_success(self):
        helper = self.load_script("copilot-emacs-mcp-call")
        with mock.patch("builtins.print") as output:
            self.assertEqual(helper.print_tool_result({
                "result": {"isError": True, "content": [
                    {"type": "text", "text": "fixture security denial"}
                ]}
            }), 1)
            output.assert_called_once_with("fixture security denial")

    def test_emacs_error_text_is_failure_without_an_iserror_flag(self):
        helper = self.load_script("copilot-emacs-mcp-call")
        for text in ("Error: Security: 'getenv' is blocked.", "Error: Arithmetic error"):
            with self.subTest(text=text), mock.patch("builtins.print") as output:
                result = helper.print_tool_result({
                    "result": {"content": [{"type": "text", "text": text}]}
                })
                self.assertEqual(result, 1)
                output.assert_called_once_with(text)

    def test_successful_elisp_error_shaped_string_remains_successful(self):
        helper = self.load_script("copilot-emacs-mcp-call")
        text = '"Error: this is returned string data"'
        with mock.patch("builtins.print") as output:
            result = helper.print_tool_result({
                "result": {"content": [{"type": "text", "text": text}]}
            })
            self.assertEqual(result, 0)
            output.assert_called_once_with(text)

    def test_parallel_clients_for_one_daemon_do_not_overlap_evaluations(self):
        self.env["COPILOT_AGENT_SESSION_ID"] = "mcp-test-shared"
        self.env["COPILOT_MCP_CALL_TIMEOUT"] = "3"
        self.executable(
            "copilot-emacs-mcp",
            """import json, os, sys, time
from pathlib import Path
root = Path(os.environ["HOME"])
def respond(identifier, result):
    print(json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result}), flush=True)
initialization = json.loads(sys.stdin.readline())
respond(initialization["id"], {})
sys.stdin.readline()
call = json.loads(sys.stdin.readline())
active = root / "active"
try:
    active.mkdir()
except FileExistsError:
    respond(call["id"], {"isError": True, "content": [
        {"type": "text", "text": "overlapping daemon evaluation"}]})
    raise SystemExit(0)
try:
    (root / "started").touch()
    deadline = time.monotonic() + 2
    while not (root / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    respond(call["id"], {"content": [
        {"type": "text", "text": call["params"]["arguments"]["expression"]}]})
finally:
    active.rmdir()
sys.stdin.read()
""",
        )
        processes = []
        try:
            first = subprocess.Popen(
                [sys.executable, str(self.client), "first"],
                cwd=self.root, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            processes.append(first)
            deadline = time.monotonic() + 2
            while not (self.root / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.root / "started").exists())
            second = subprocess.Popen(
                [sys.executable, str(self.client), "second"],
                cwd=self.root, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            processes.append(second)
            time.sleep(0.2)
            (self.root / "release").touch()
            for process, expected in zip(processes, ("first", "second")):
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr + stdout)
                self.assertEqual(stdout.strip(), expected)
        finally:
            (self.root / "release").touch()
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)

    def test_busy_daemon_lock_times_out_before_starting_the_bridge(self):
        self.env["COPILOT_AGENT_SESSION_ID"] = "mcp-test-shared"
        self.env["COPILOT_MCP_QUEUE_TIMEOUT"] = "0.2"
        directory = self.root / ".emacs.d"
        directory.mkdir()
        self.executable("copilot-emacs-mcp", "raise RuntimeError('bridge must not start')")
        with (directory / "copilot-mcp-test.call.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.invoke()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no evaluation was sent", result.stderr)
        self.assertNotIn("bridge must not start", result.stderr)

    def test_queue_budget_is_separate_from_the_response_deadline(self):
        self.env["COPILOT_AGENT_SESSION_ID"] = "mcp-test-shared"
        self.env["COPILOT_MCP_CALL_TIMEOUT"] = "0.2"
        self.executable(
            "copilot-emacs-mcp",
            """import json, sys
initialization = json.loads(sys.stdin.readline())
print(json.dumps({"id": initialization["id"], "result": {}}), flush=True)
sys.stdin.readline()
call = json.loads(sys.stdin.readline())
print(json.dumps({"id": call["id"], "result": {"content": [
    {"type": "text", "text": "queued successfully"}]}}), flush=True)
sys.stdin.read()
""",
        )
        directory = self.root / ".emacs.d"
        directory.mkdir()
        process = None
        try:
            with (directory / "copilot-mcp-test.call.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                process = subprocess.Popen(
                    [sys.executable, str(self.client), "(copilot-cs-status)"],
                    cwd=self.root, env=self.env, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True,
                )
                time.sleep(0.5)
                self.assertIsNone(process.poll())
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "queued successfully")
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)

    def test_different_daemons_have_independent_invocation_locks(self):
        helper = self.load_script("copilot-emacs-mcp-call")
        self.env["COPILOT_AGENT_SESSION_ID"] = "first-daemon"
        with mock.patch.dict(os.environ, self.env, clear=True):
            with helper.invocation_lock(0.2):
                with mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "second-daemon"}):
                    with helper.invocation_lock(0.2):
                        pass

    def test_invocation_failure_releases_the_daemon_lock(self):
        helper = self.load_script("copilot-emacs-mcp-call")
        self.env["COPILOT_AGENT_SESSION_ID"] = "mcp-test-shared"
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                with helper.invocation_lock(0.2):
                    raise RuntimeError("fixture failure")
            with helper.invocation_lock(0.2):
                pass


class McpFormSafetyTests(HelperTestCase):
    def check(self, expression):
        return self.emacs(
            f"""(progn
              (defvar checked-forms nil)
              (defun mcp-server-security--check-form-safety (form)
                (push form checked-forms)
                (when (or (eq form 'blocked-fixture)
                          (eq (car-safe form) 'blocked-fixture))
                  (error "Fixture function denied"))
                (when (and (eq (car-safe form) 'find-file)
                           (equal (cadr form) "/outside"))
                  (error "Fixture path denied"))
                (when (consp form)
                  (dolist (arg (cdr form))
                    (mcp-server-security--check-form-safety arg))))
              (load "copilot-mcp-form-safety" nil t)
              {expression})"""
        )

    def test_quoted_dotted_pairs_keep_their_evaluated_values(self):
        output = self.check(
            """(let* ((form '(mapcar (lambda (entry) (cons (car entry) (cdr entry)))
                                    '(("race-suite" . "job-a")
                                      ("workflow-matrix" . "job-b"))))
                      (before (copy-tree form)))
                 (mcp-server-security--check-form-safety form)
                 (unless (equal form before) (error "Input form was changed"))
                 (unless (member "job-b" checked-forms)
                   (error "Dotted tail was not checked"))
                 (prin1 (eval form t)))"""
        )
        self.assertEqual(output, '(("race-suite" . "job-a") ("workflow-matrix" . "job-b"))')

    def test_improper_tails_retain_function_and_file_checks(self):
        self.check(
            """(dolist (case '(((quote (label . blocked-fixture)) . "Fixture function denied")
                               ((quote (blocked-fixture . "value")) . "Fixture function denied")
                               ((quote (find-file . "/outside")) . "Fixture path denied")
                               ((quote (label value . blocked-fixture)) . "Fixture function denied")))
                 (let ((failure
                        (condition-case err
                            (progn (mcp-server-security--check-form-safety (car case)) nil)
                          (error (error-message-string err)))))
                   (unless (equal failure (cdr case))
                     (error "Expected %S; got %S" (cdr case) failure))))"""
        )

    def test_proper_forms_are_forwarded_unchanged(self):
        self.check(
            """(dolist (form '(nil 7 "text" (list "first" "second") [1 2]))
                 (let (received)
                   (unless (eq (copilot-mcp--check-dotted-form
                                (lambda (value) (setq received value) 'checked) form)
                               'checked)
                     (error "Checker result was not preserved"))
                   (unless (eq received form)
                     (error "Proper form was copied or rewritten"))))"""
        )

    def test_circular_list_spines_fail_without_looping(self):
        self.check(
            """(let ((form (list 'list "value")))
                 (setcdr (last form) form)
                 (let ((failure
                        (condition-case err
                            (progn (mcp-server-security--check-form-safety form) nil)
                          (error (error-message-string err)))))
                   (unless (equal failure "Security: circular MCP forms are unsupported")
                     (error "Unexpected circular-form result: %S" failure))))"""
        )

    def test_reloading_installs_only_one_adapter(self):
        self.check(
            """(load "copilot-mcp-form-safety" nil t)
               (let ((form '(label . "value")))
                 (mcp-server-security--check-form-safety form)
                 (unless (equal (reverse checked-forms) '((label "value") "value"))
                   (error "Unexpected checker calls: %S" checked-forms)))"""
        )


class ShellStartupTests(HelperTestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS terminal-session integration")
    def test_macos_session_restore_is_only_enabled_for_real_terminals(self):
        self.env.update(TERM_PROGRAM="Apple_Terminal", TERM_SESSION_ID="fixture-terminal")
        (self.root / ".zshenv").symlink_to(DOTFILES / "zshenv")
        session_dir = self.root / ".zsh_sessions"
        session_dir.mkdir()
        session_file = session_dir / "fixture-terminal.session"
        restored = self.root / "restored"
        seed = 'printf restored > "$HOME/restored"\n'
        command = 'printf "%s" "${SHELL_SESSION_DID_INIT:-0}" > "$HOME/session-init"'
        for terminal, term in ((False, "xterm-256color"),
                               (True, "xterm-256color"), (True, "dumb")):
            with self.subTest(terminal=terminal, term=term):
                self.env["TERM"] = term
                restored.unlink(missing_ok=True)
                session_file.write_text(seed)
                argv = ["zsh", "-i", "-c", command]
                if terminal:
                    master, slave = os.openpty()
                    try:
                        with subprocess.Popen(
                            argv, cwd=self.root, env=self.env, stdin=slave,
                            stdout=slave, stderr=subprocess.PIPE, text=True,
                        ) as process:
                            _, stderr = process.communicate(timeout=10)
                            self.assertEqual(process.returncode, 0, stderr)
                    finally:
                        os.close(slave)
                        os.close(master)
                else:
                    result = self.run_command(argv)
                    self.assertEqual(result.returncode, 0, result.stderr)
                enabled = terminal and term != "dumb"
                self.assertEqual((self.root / "session-init").read_text(), "1" if enabled else "0")
                self.assertEqual(restored.exists(), enabled)
                if not enabled:
                    self.assertEqual(session_file.read_text(), seed)

    def test_headless_interactive_shell_keeps_shared_config_without_terminal_frameworks(self):
        self.env["CODESPACES"] = "true"
        self.executable("starship", "raise RuntimeError('prompt must not start')")
        (self.root / ".shared_shell_configs").write_text(
            "export SHARED_SHELL_CONFIGS=1\nfixture_shared() { printf shared-ready; }\n"
        )
        result = self.run_command([
            "zsh", "-f", "-i", "-c",
            f"source {shlex.quote(str(DOTFILES / 'zshrc'))} && "
            "fixture_shared && printf '\\n' && alias ls && "
            "if [[ $OSTYPE == darwin* ]]; then functions ec >/dev/null; fi",
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("shared-ready", result.stdout)
        self.assertIn("ls=", result.stdout)

    def test_terminal_shell_still_loads_prompt_plugins_and_highlighting(self):
        self.env.update(SHARED_SHELL_CONFIGS="1", TERM="xterm-256color",
                        HOMEBREW_PREFIX=str(self.root / "brew"))
        highlighting = self.root / "brew/share/zsh-syntax-highlighting"
        highlighting.mkdir(parents=True)
        (highlighting / "zsh-syntax-highlighting.zsh").write_text(
            'printf "highlighting\\n" >> "$HOME/startup"\n'
        )
        framework = self.root / ".oh-my-zsh"
        framework.mkdir()
        (framework / "oh-my-zsh.sh").write_text(
            'printf "plugins: %s\\n" "${plugins[*]}" >> "$HOME/startup"\n'
        )
        self.executable("starship", """print('printf "prompt\\\\n" >> "$HOME/startup"')""")
        master, slave = os.openpty()
        try:
            with subprocess.Popen(
                ["zsh", "-f", "-i", "-c", f"source {shlex.quote(str(DOTFILES / 'zshrc'))}"],
                cwd=self.root, env=self.env, stdin=slave, stdout=slave,
                stderr=subprocess.PIPE, text=True,
            ) as process:
                _, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stderr, "")
        finally:
            os.close(slave)
            os.close(master)
        self.assertEqual((self.root / "startup").read_text().splitlines(), [
            "highlighting", "prompt", "plugins: macos brew kubectl python pip ruby gpg-agent golang",
        ])


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


class SshSigningTests(SecretiveTransportTestCase):
    def setUp(self):
        super().setUp()
        Path(self.env["COPILOT_SECRETIVE_PUBLIC_KEY"]).write_text(
            "ssh-ed25519 AAAA public-only-fixture\n"
        )
        self.env["FAKE_GH_FAILURES"] = "0"

    def test_forwarding_pins_secretive_instead_of_the_parent_agent(self):
        self.env["SSH_AUTH_SOCK"] = str(self.root / "unrelated-agent.sock")
        self.executable(
            "gh",
            """import os
print(os.environ["SSH_AUTH_SOCK"])
""",
        )
        ordinary = self.invoke("ssh", "test-codespace", "ordinary")
        self.assertEqual(ordinary.returncode, 0, ordinary.stderr)
        self.assertEqual(ordinary.stdout.strip(), self.env["SSH_AUTH_SOCK"])
        signing = self.invoke("ssh-sign", "test-codespace", "git-fixture")
        self.assertEqual(signing.returncode, 0, signing.stderr)
        self.assertEqual(signing.stdout.strip(), self.env["COPILOT_SECRETIVE_AGENT_SOCKET"])

    def test_forwarding_and_public_key_export_require_explicit_mode(self):
        result = self.invoke("ssh", "test-codespace", "ordinary")
        self.assertEqual(result.returncode, 0, result.stderr)
        ordinary = self.recorded_calls()[-1]
        self.assertNotIn("-A", ordinary)
        self.assertIn("ForwardAgent=no", ordinary)
        self.assertNotIn("COPILOT_CS_SIGNING_KEY", ordinary[-1])
        result = self.invoke("ssh-sign", "test-codespace", "git-fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        signing = self.recorded_calls()[-1]
        self.assertIn("-A", signing)
        self.assertIn("IdentityAgent=" + self.env["COPILOT_SECRETIVE_AGENT_SOCKET"], signing)
        self.assertIn("COPILOT_CS_SIGNING_KEY='ssh-ed25519 BBBB'", signing[-1])
        self.assertNotIn("ForwardAgent=no", signing)
        self.assertIn('COPILOT_CS_SIGNING_SOCKET="$SSH_AUTH_SOCK"', signing[-1])
        self.assertTrue(signing[-1].endswith("; git-fixture"))
        result = self.invoke("cp", "test-codespace", "fixture", "remote:/tmp/fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("-A", self.recorded_calls()[-1])
        self.assertIn("ForwardAgent=no", self.recorded_calls()[-1])

    def test_signing_mode_rejects_non_public_data_before_connecting(self):
        Path(self.env["COPILOT_SECRETIVE_SIGNING_PUBLIC_KEY"]).write_text(
            "not a public key\n"
        )
        result = self.invoke("ssh-sign", "test-codespace", "git-fixture")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("single OpenSSH public-key line", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_signing_mode_rejects_interactive_invocations(self):
        for arguments in ((), ("-t", "git-fixture")):
            with self.subTest(arguments=arguments):
                result = self.invoke("ssh-sign", "test-codespace", *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls.exists())


class SigningNotificationTests(SecretiveTransportTestCase):
    def setUp(self):
        super().setUp()
        self.helper = self.load_script("copilot-gh-retry")
        self.notices = self.root / "signing-notices"
        self.shell_flags = self.root / "notify-shell-flags"
        self.shell_arguments = self.root / "notify-shell-arguments"
        self.session_id = "01234567-89ab-4cde-8f01-23456789abcd"
        self.env.update(
            SHELL=shutil.which("bash"),
            FAKE_GH_FAILURES="0",
            COPILOT_AGENT_SESSION_ID=self.session_id,
            EMACS_MCP_SOCKET_NAME="copilot-" + self.session_id[:8],
            NOTIFY_LOG=str(self.notices),
            NOTIFY_FLAGS=str(self.shell_flags),
            NOTIFY_ARGUMENTS=str(self.shell_arguments),
        )
        (self.root / ".bashrc").write_text(
            'printf "%s\\n" "$-" > "$NOTIFY_FLAGS"\n'
            'printf "%s\\n" "$#" > "$NOTIFY_ARGUMENTS"\n'
        )
        self.configuration = self.root / ".shared_shell_configs"
        self.configuration.write_text(
            'notify() {\n'
            '  printf "%s\\n" "$*" >> "$NOTIFY_LOG"\n'
            '  printf "\\a"\n'
            '  printf "notification-private-fixture\\n" >&2\n'
            '  return "${NOTIFY_STATUS:-0}"\n'
            '}\n'
        )

    def progress(self, index=1, total=2, oid=None, ending=b"\n"):
        return (f"[copilot-cs-endorse] signing {index}/{total}: "
                f"{oid or 'a' * 40}").encode() + ending

    def transport_output(self, output, status=0, *, stderr=False):
        self.executable(
            "gh",
            f"import os, sys\nos.write({2 if stderr else 1}, {output!r})\n"
            f"sys.exit({status})\n",
        )

    def test_signing_stream_notifies_each_signature_without_changing_output(self):
        output = (
            b"__COPILOT_CS_ACK_job-fixture__\n"
            + self.progress()
            + self.progress(2, 2, "b" * 64, b"\r\n")
            + b"[copilot-cs-endorse] verifying 1/2: " + b"a" * 40 + b"\n"
            + b"\x80\n__COPILOT_CS_DONE_job-fixture__:0\n"
        )
        self.transport_output(output)
        result = subprocess.run(
            ["bash", str(SETUP / "copilot-ghcs"), "ssh-sign", "test-codespace",
             "--job-id", "job-fixture", "fixture-sign"],
            cwd=self.root, env=self.env, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, output)
        self.assertEqual(self.notices.read_text().splitlines(), [
            f"Copilot endorsement (session {self.session_id}) is requesting SSH signature 1/2. "
            "Check Secretive for approval.",
            f"Copilot endorsement (session {self.session_id}) is requesting SSH signature 2/2. "
            "Check Secretive for approval.",
        ])
        self.assertIn("i", self.shell_flags.read_text())
        self.assertEqual(self.shell_arguments.read_text(), "0\n")
        self.assertNotIn(b"notification-private-fixture", result.stderr)
        self.assertNotIn("a" * 40, self.notices.read_text())
        self.assertNotIn("b" * 64, self.notices.read_text())

    def test_zsh_uses_the_same_interactive_shell_function(self):
        shell = shutil.which("zsh")
        if shell is None:
            self.skipTest("zsh is not installed")
        self.env["SHELL"] = shell
        (self.root / ".zshrc").write_text((self.root / ".bashrc").read_text())
        self.transport_output(self.progress(1, 1))
        result = self.invoke("ssh-sign", "test-codespace", "fixture-sign")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.notices.read_text().splitlines(), [
            f"Copilot endorsement (session {self.session_id}) is requesting SSH signature 1/1. "
            "Check Secretive for approval.",
        ])
        self.assertIn("i", self.shell_flags.read_text())
        self.assertEqual(self.shell_arguments.read_text(), "0\n")

    def test_notifications_distinguish_the_originating_sessions(self):
        self.transport_output(self.progress(1, 1))
        sessions = (self.session_id, "FEDCBA98-7654-4321-8ABC-DEF012345678")
        self.env["COPILOT_SESSION_TITLE"] = "private-task-title-fixture"
        self.env["COPILOT_CS_ID"] = "private-repository-fixture"
        for session in sessions:
            self.env["COPILOT_AGENT_SESSION_ID"] = session
            self.env["EMACS_MCP_SOCKET_NAME"] = "copilot-" + session[:8]
            result = self.invoke("ssh-sign", "test-codespace", "fixture-sign")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.notices.read_text().splitlines(), [
            f"Copilot endorsement (session {session}) is requesting SSH signature 1/1. "
            "Check Secretive for approval." for session in sessions
        ])
        self.assertNotIn("private-", self.notices.read_text())

    def test_emacs_job_preserves_the_originating_session_context(self):
        self.transport_output(
            b"__COPILOT_CS_ACK_job-fixture__\n" + self.progress(1, 1)
            + b"__COPILOT_CS_DONE_job-fixture__:0\n"
        )
        self.emacs(
            """(progn
              (copilot-cs-use "test-codespace" "/workspaces/fixture")
              (let* ((job (copilot-cs--start "job-fixture" "fixture-sign" nil t))
                     (process (plist-get job :process))
                     (deadline (+ (float-time) 5)))
                (while (and (process-live-p process) (< (float-time) deadline))
                  (accept-process-output process 0.05))
                (when (process-live-p process)
                  (delete-process process)
                  (error "Fixture signing transport did not finish"))
                (unless (equal (copilot-cs--rc job) 0)
                  (error "Fixture signing transport failed: %s" (copilot-cs--text job)))))"""
        )
        self.assertEqual(self.notices.read_text().splitlines(), [
            f"Copilot endorsement (session {self.session_id}) is requesting SSH signature 1/1. "
            "Check Secretive for approval.",
        ])

    def test_daemon_identifier_supplies_context_when_cli_session_is_missing(self):
        del self.env["COPILOT_AGENT_SESSION_ID"]
        self.transport_output(self.progress(1, 1))
        daemons = ("copilot-01234567", "copilot-pid-4321")
        for daemon in daemons:
            self.env["EMACS_MCP_SOCKET_NAME"] = daemon
            result = self.invoke("ssh-sign", "test-codespace", "fixture-sign")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("session context unavailable", result.stderr)
        self.assertEqual(self.notices.read_text().splitlines(), [
            f"Copilot endorsement (daemon {daemon}) is requesting SSH signature 1/1. "
            "Check Secretive for approval." for daemon in daemons
        ])

    def test_unavailable_context_warns_without_sending_arbitrary_metadata(self):
        self.transport_output(self.progress(1, 1))
        for session, daemon in (
            ("", ""),
            ("private-task-title-fixture", "copilot-private-repository"),
            (self.session_id + "\nprivate-context", "copilot-01234567\nprivate-context"),
            ("a" * 4096, "copilot-" + "b" * 4096),
        ):
            with self.subTest(session=session[:40], daemon=daemon[:40]):
                self.env["COPILOT_AGENT_SESSION_ID"] = session
                self.env["EMACS_MCP_SOCKET_NAME"] = daemon
                result = self.invoke("ssh-sign", "test-codespace", "fixture-sign")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, self.progress(1, 1).decode())
                self.assertIn("session context unavailable", result.stderr)
                self.assertIn("signing continues", result.stderr)
                self.assertNotIn("private-", result.stderr)
        self.assertEqual(self.notices.read_text().splitlines(), [
            "Copilot endorsement (session unavailable) is requesting SSH signature 1/1. "
            "Check Secretive for approval."
        ] * 4)

    def test_shell_startup_cannot_relabel_the_originating_session(self):
        other_session = "fedcba98-7654-4321-8abc-def012345678"
        with (self.root / ".bashrc").open("a") as startup:
            startup.write(f'export COPILOT_AGENT_SESSION_ID="{other_session}"\n')
        self.transport_output(self.progress(1, 1))
        result = self.invoke("ssh-sign", "test-codespace", "fixture-sign")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.session_id, self.notices.read_text())
        self.assertNotIn(other_session, self.notices.read_text())

    def test_notifications_finish_after_authentication_but_before_forwarding(self):
        output = (b"__COPILOT_CS_ACK_job-fixture__\n" + self.progress(1, 1)
                  + b"__COPILOT_CS_DONE_job-fixture__:0\n")
        events = []
        gate = mock.Mock()
        gate.authenticated.side_effect = lambda: events.append("authenticated")
        destination = mock.Mock()
        destination.buffer.write.side_effect = lambda data: events.append(("output", data))
        with mock.patch.object(self.helper, "notify_signature",
                               side_effect=lambda *_: events.append("notified")), \
                mock.patch.object(self.helper.sys, "stdout", destination), \
                mock.patch.object(self.helper, "stop_process",
                                  side_effect=lambda process: process.wait(timeout=5)):
            result = self.helper.run_once(
                [sys.executable, "-c", f"import os; os.write(1, {output!r})"],
                gate, time.monotonic() + 5, "__COPILOT_CS_ACK_job-fixture__",
                notify_signatures=True,
            )
        self.assertEqual(result, (0, b"", True))
        self.assertEqual(events, ["authenticated", "notified", ("output", output)])

    def test_other_transports_and_verification_do_not_notify(self):
        self.transport_output(self.progress())
        for arguments in (
            ("ssh", "test-codespace", "ordinary"),
            ("cp", "test-codespace", "fixture", "remote:/tmp/fixture"),
        ):
            with self.subTest(mode=arguments[0]):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.notices.exists())
        for output, stderr in (
            (self.progress().replace(b"signing", b"verifying"), False),
            (self.progress().removesuffix(b"\n"), False),
            (self.progress(), True),
            (b'Authenticated to localhost ([127.0.0.1]:1234) using "publickey".\n', True),
        ):
            with self.subTest(output=output):
                self.transport_output(output, stderr=stderr)
                result = self.invoke("ssh-sign", "test-codespace", "fixture-sign")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.notices.exists())

    def test_fragmented_records_notify_once_after_the_complete_line(self):
        progress = self.helper.SigningProgressNotifier()
        with mock.patch.object(self.helper, "notify_signature") as notify:
            for byte in self.progress()[:-1]:
                progress.feed(bytes([byte]))
                notify.assert_not_called()
            progress.feed(b"\n" + self.progress(2, 2, "b" * 64, b"\r\n"))
            self.assertEqual(notify.call_args_list, [mock.call(1, 2), mock.call(2, 2)])

    def test_malformed_and_oversized_lines_cannot_become_signing_records(self):
        progress = self.helper.SigningProgressNotifier()
        with mock.patch.object(self.helper, "notify_signature") as notify:
            for output in (
                b"remote: " + self.progress(),
                self.progress(0, 1),
                self.progress(2, 1),
                self.progress(1, 0),
                self.progress(oid="a" * 41),
                self.progress(oid="A" * 40),
                self.progress().replace(b"signing", b"verifying"),
            ):
                progress.feed(output)
            progress.feed(b"x" * 4096)
            progress.feed(self.progress())
            notify.assert_not_called()
            progress.feed(self.progress())
            notify.assert_called_once_with(1, 2)

    def test_repeated_signing_attempts_each_notify(self):
        progress = self.helper.SigningProgressNotifier()
        with mock.patch.object(self.helper, "notify_signature") as notify:
            progress.feed(self.progress(1, 1) * 2)
            self.assertEqual(notify.call_args_list, [mock.call(1, 1), mock.call(1, 1)])

    def test_notification_failure_warns_without_changing_signing_outcome(self):
        self.env["NOTIFY_STATUS"] = "10"
        self.transport_output(self.progress(1, 1), status=17)
        result = self.invoke("ssh-sign", "test-codespace", "fixture-sign")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(result.stdout, self.progress(1, 1).decode())
        self.assertIn("notification failed (exit 10)", result.stderr)
        self.assertIn("signing continues", result.stderr)
        self.assertNotIn("notification-private-fixture", result.stderr)
        self.assertEqual(len(self.notices.read_text().splitlines()), 1)
        self.assertFalse(self.sleeps.exists())

    def test_missing_shell_function_does_not_fall_back_to_an_executable(self):
        self.configuration.write_text("true\n")
        self.executable(
            "notify",
            'from pathlib import Path\nimport os\n'
            'Path(os.environ["NOTIFY_LOG"]).write_text("wrong notifier\\n")\n',
        )
        self.transport_output(self.progress(1, 1))
        for shell in ("bash", "zsh"):
            path = shutil.which(shell)
            if path is None:
                continue
            with self.subTest(shell=shell):
                self.env["SHELL"] = path
                result = self.invoke("ssh-sign", "test-codespace", "fixture-sign")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("notification failed", result.stderr)
                self.assertIn("signing continues", result.stderr)
                self.assertFalse(self.notices.exists())

    def test_missing_shell_or_configuration_warns_and_preserves_success(self):
        self.transport_output(self.progress(1, 1))
        shell = self.env["SHELL"]
        for missing in ("shell", "configuration"):
            with self.subTest(missing=missing):
                if missing == "shell":
                    self.env["SHELL"] = str(self.root / "missing-shell")
                else:
                    self.env["SHELL"] = shell
                    self.configuration.unlink()
                result = self.invoke("ssh-sign", "test-codespace", "fixture-sign")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, self.progress(1, 1).decode())
                self.assertIn("warning: signing notification", result.stderr)
                self.assertIn("signing continues", result.stderr)
                self.assertFalse(self.notices.exists())

    def test_notification_timeout_stops_its_own_process_group(self):
        child_pid = self.root / "notification-child.pid"
        self.env["NOTIFY_CHILD_PID"] = str(child_pid)
        self.executable(
            "notify-blocker",
            'import os, time\nfrom pathlib import Path\n'
            'Path(os.environ["NOTIFY_CHILD_PID"]).write_text(str(os.getpid()))\n'
            'time.sleep(30)\n',
        )
        self.configuration.write_text("notify() ( result=$(notify-blocker) )\n")
        output = self.progress(1, 1)
        destination = mock.Mock()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(self.helper, "SIGNING_NOTIFY_TIMEOUT", 0.5), \
                mock.patch.object(self.helper, "log") as messages, \
                mock.patch.object(self.helper.sys, "stdout", destination), \
                mock.patch.object(self.helper, "stop_process",
                                  side_effect=lambda process: process.wait(timeout=5)):
            started = time.monotonic()
            result = self.helper.run_once(
                [sys.executable, "-c", f"import os; os.write(1, {output!r}); raise SystemExit(17)"],
                mock.Mock(), time.monotonic() + 5, notify_signatures=True,
            )
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result, (17, b"", True))
        self.assertEqual(destination.buffer.write.call_args_list, [mock.call(output)])
        self.assertTrue(child_pid.exists(), "notification fixture never started")
        self.assertFalse(JobCancellationTests.live(int(child_pid.read_text())))
        self.assertTrue(any("notification timed out" in call.args[0]
                            and "signing continues" in call.args[0]
                            for call in messages.call_args_list))

    def test_cancellation_cleans_up_the_notification_and_still_propagates(self):
        process = mock.Mock(pid=12345, returncode=None)
        process.wait.side_effect = [self.helper.Interrupted(signal.SIGTERM), -signal.SIGKILL]
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(self.helper.subprocess, "Popen", return_value=process) as start, \
                mock.patch.object(self.helper.os, "killpg") as kill, \
                mock.patch.object(self.helper, "log") as messages:
            with self.assertRaises(self.helper.Interrupted):
                self.helper.notify_signature(1, 1)
        self.assertTrue(start.call_args.kwargs["start_new_session"])
        kill.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(process.wait.call_count, 2)
        messages.assert_not_called()


class JobStateTests(HelperTestCase):
    def test_job_transport_requires_the_matching_acknowledgement(self):
        self.emacs(
            """(progn
              (copilot-cs-use "fixture-codespace" "/workspaces/fixture")
              (unless (equal (cdr (copilot-cs--argv "launcher" nil "job-fixture"))
                             '("ssh" "fixture-codespace" "--job-id" "job-fixture" "launcher"))
                (error "Job connection has no acknowledgement deadline"))
              (unless (equal (cdr (copilot-cs--argv "server"))
                             '("ssh" "fixture-codespace" "server"))
                (error "Generic server connection expects a runner acknowledgement")))"""
        )

    def test_job_id_extraction_preserves_the_complete_suffix(self):
        self.emacs(
            """(unless
                 (equal (copilot-cs-job-id
                         "job=job-142226-182-af1f state=done rc=0 elapsed=1.0s")
                        "job-142226-182-af1f")
               (error "The job id was truncated"))"""
        )

    def test_job_id_extraction_rejects_non_reports(self):
        self.emacs(
            """(dolist (report '(nil "" "job-142226-182-af1f" "job=../unsafe state=done"))
                (let ((rejected nil))
                  (condition-case nil (copilot-cs-job-id report)
                    (error (setq rejected t)))
                  (unless rejected (error "Invalid report accepted: %S" report))))"""
        )

    def test_only_approved_endorsement_signing_forwards_the_agent(self):
        self.emacs(
            """(progn
              (require 'cl-lib)
              (copilot-cs-use "fixture-codespace" "/workspaces/fixture")
              (let (seen)
                (cl-letf (((symbol-function 'copilot-cs--run)
                           (lambda (command label wait &optional ssh-signing)
                             (setq seen (list command label wait ssh-signing)))))
                  (let ((plan (make-string 64 ?a)))
                    (copilot-cs-endorse "sign" plan (concat plan ":replacement") 0)))
                (unless (and (nth 3 seen)
                             (string-match-p "approve-plan" (car seen))
                             (equal (cadr seen) "endorsement sign")
                             (equal (nth 2 seen) 0)
                             (equal (nth 1 (copilot-cs--argv "ordinary")) "ssh")
                             (equal (nth 1 (copilot-cs--argv "signed" t)) "ssh-sign"))
                  (error "Signing escaped its invocation or lost quoting"))))"""
        )

    def test_unrestricted_signing_api_is_retired(self):
        self.emacs(
            """(progn
              (copilot-cs-use nil ".")
              (let ((rejected nil))
                (condition-case nil (copilot-cs-ssh-git '("commit"))
                  (error (setq rejected t)))
                (unless rejected (error "Local signing was accepted")))
              (copilot-cs-use "fixture-codespace" "/workspaces/fixture")
              (dolist (args '(nil "commit" (commit) ("commit" "-S")))
                (let ((rejected nil))
                  (condition-case nil (copilot-cs-ssh-git args)
                    (error (setq rejected t)))
                  (unless rejected (error "Invalid arguments accepted: %S" args)))))"""
        )

    def test_endorsement_requires_target_and_exact_plan_and_publication_approval(self):
        self.emacs(
            """(progn
              (copilot-cs-use nil ".")
              (let ((rejected nil))
                (condition-case nil (copilot-cs-endorse "plan" "{}")
                  (error (setq rejected t)))
                (unless rejected (error "Local endorsement was accepted")))
              (copilot-cs-use "fixture-codespace" "/workspaces/fixture")
              (dolist (args '(("sign" "bad-id" "bad-id:replace")
                             ("sign" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
                             ("push" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                                     "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
                             ("arbitrary" "{}")))
                (let ((rejected nil))
                  (condition-case nil (apply #'copilot-cs-endorse args)
                    (error (setq rejected t)))
                  (unless rejected (error "Unapproved endorsement accepted: %S" args)))))"""
        )

    def test_plan_verify_and_push_do_not_forward_the_agent(self):
        self.emacs(
            """(progn
              (require 'cl-lib)
              (copilot-cs-use "fixture-codespace" "/workspaces/fixture")
              (cl-letf (((symbol-function 'copilot-cs--run)
                         (lambda (_command _label _wait &optional signing)
                           (when signing (error "Unexpected agent forwarding")))))
                (copilot-cs-endorse "plan" "{\\"fixture\\":\\"'; not a command\\"}")
                (let ((plan (make-string 64 ?a)))
                  (copilot-cs-endorse "verify" plan)
                  (copilot-cs-endorse "push" plan (concat plan ":replace"))))))"""
        )

    def test_endorsement_result_uses_the_named_jobs_complete_output(self):
        self.check_job(
            """(let ((copilot-cs-tail-bytes 8)
                     (copilot-cs--last-id "different-job"))
                (unless (equal (copilot-cs-endorsement-result id)
                               "{\\"plan_id\\":\\"fixture\\"}")
                  (error "Endorsement result was truncated or crossed jobs")))""",
            output='COPILOT_ENDORSEMENT_RESULT {"plan_id":"fixture"}\n'
                   "__COPILOT_CS_DONE_job-state-fixture__:0\n",
        )

    def test_command_deadline_is_separate_from_poll_wait(self):
        self.emacs(
            """(progn
              (require 'cl-lib)
              (let (seen)
                (cl-letf (((symbol-function 'copilot-cs--run)
                           (lambda (command _label wait &optional _sign)
                             (setq seen (list command wait)))))
                  (copilot-cs-timed-sh "git --no-pager log --no-decorate -5" 20 0))
                (unless (and (string-match-p
                               "timeout --verbose --kill-after=5s 20s env COPILOT_CS_LOGIN_DIR="
                               (car seen))
                             (string-match-p "bash -lc" (car seen))
                             (equal (cadr seen) 0))
                  (error "Command deadline became a polling wait"))))"""
        )

    def test_invalid_command_deadlines_are_rejected(self):
        self.emacs(
            """(dolist (seconds '(nil 0 -1 1.5 "20"))
                (let ((rejected nil))
                  (condition-case nil (copilot-cs-timed-sh "true" seconds)
                    (error (setq rejected t)))
                  (unless rejected (error "Invalid deadline accepted: %S" seconds))))"""
        )

    def test_interrupt_uses_the_supported_scoped_cancellation(self):
        self.emacs(
            """(unless (eq (indirect-function 'copilot-cs-interrupt)
                           (indirect-function 'copilot-cs-stop))
                 (error "Interrupt did not use scoped job cancellation"))"""
        )

    def test_reentrant_commands_keep_their_own_default_output(self):
        self.emacs(
            f"""(progn
              (copilot-cs-use nil {json.dumps(str(self.root))})
              (let (nested-output)
                (run-at-time 0.2 nil
                  (lambda ()
                    (copilot-cs-sh "printf nested" 3)
                    (setq nested-output (copilot-cs-output))))
                (let* ((report (copilot-cs-sh "sleep 1; printf outer" 5))
                       (output (copilot-cs-output)))
                  (unless (and (string-match-p "state=done rc=0" report)
                               (equal output "outer")
                               (equal nested-output "nested"))
                    (error "Crossed job outputs: outer=%S nested=%S"
                           output nested-output)))))"""
        )

    def test_reentrant_waits_share_the_outer_deadline(self):
        self.check_job(
            """(let ((started (float-time))
                     (nested-reports nil))
                (dotimes (index 3)
                  (run-at-time (+ 0.025 (* index 0.025)) nil
                    (lambda ()
                      (push (copilot-cs-poll id 2) nested-reports))))
                (copilot-cs-poll id 0.25)
                (unless (and (= (length nested-reports) 3)
                             (< (- (float-time) started) 1))
                  (error "Nested waits exceeded the outer deadline: %.2fs, %S"
                         (- (float-time) started) nested-reports)))""",
            live=True,
        )

    def test_zero_wait_never_starts_another_event_loop_wait(self):
        self.check_job(
            """(cl-letf (((symbol-function 'accept-process-output)
                         (lambda (_process seconds &optional _millis just-one)
                           (unless (and (zerop seconds) (integerp just-one))
                             (error "Zero wait serviced other calls or timers")))))
                (copilot-cs-poll id 0))""",
            live=True,
        )

    def test_poll_restores_its_named_job_as_default_output(self):
        self.check_job(
            """(let ((copilot-cs--last-id "different-job"))
                (copilot-cs-poll id 0)
                (unless (equal (copilot-cs-output) "this job")
                  (error "Poll left the default pointing at another job")))""",
            output="this job\n__COPILOT_CS_DONE_job-state-fixture__:0\n",
        )

    def test_commit_producing_git_commands_get_login_state(self):
        self.emacs(
            """(dolist (command '("git cherry-pick original" "git cherry-pick --continue"
                                 "git revert HEAD" "git rebase origin/main"
                                 "git merge feature" "git am change.patch"
                                 "git pull --ff-only" "true && git commit -m update"))
                (unless (copilot-cs--login-required-p command)
                  (error "Missing login state for %s" command)))"""
        )

    def test_login_profiles_do_not_receive_command_arguments(self):
        workdir = self.root / "work dir's checkout"
        workdir.mkdir()
        self.executable(
            "bash",
            """import os, sys
assert sys.argv[1] == "-lc"
if len(sys.argv) != 4:
    print("login profile received command arguments", file=sys.stderr)
    sys.exit(71)
os.chdir(os.environ["HOME"])
os.execv("/bin/sh", ["sh", "-c", sys.argv[2], sys.argv[3]])
""",
        )
        payload = "literal 'quotes'; $(not-a-command)"
        command = f"pwd; printf '%s\\n' {shlex.quote(payload)}; exit 23"
        expressions = (
            f"(copilot-cs--login-shell-command {json.dumps(command)})",
            f"""(progn
                  (require 'copilot-cs-eglot)
                  (copilot-cs-eglot--login-command
                    {json.dumps(str(workdir))} {json.dumps(command)}))""",
        )
        for expression in expressions:
            with self.subTest(expression=expression):
                wrapper = self.emacs(f"(princ {expression})")
                result = self.run_command(
                    ["sh", "-c", f"cd {shlex.quote(str(workdir))} && {wrapper}"]
                )
                self.assertEqual(result.returncode, 23, result.stderr)
                lines = result.stdout.splitlines()
                self.assertEqual(Path(lines[0]).resolve(), workdir.resolve())
                self.assertEqual(lines[1:], [payload])

    def test_remote_tty_wrapper_preserves_workdir_and_failure(self):
        calls = self.root / "script-calls"
        self.env["FAKE_SCRIPT_CALLS"] = str(calls)
        self.executable("stty", "raise SystemExit(0)\n")
        self.executable(
            "script",
            """import json, os, sys
from pathlib import Path
Path(os.environ["FAKE_SCRIPT_CALLS"]).write_text(json.dumps(sys.argv[1:]))
assert os.environ["SHELL"] == "/bin/sh"
command = sys.argv[sys.argv.index("-c") + 1]
os.execv("/bin/sh", ["sh", "-c", command])
""",
        )
        wrapper = self.emacs(
            '(princ (copilot-cs--tty-shell-command "pwd; exit 23"))'
        )
        result = self.run_command(["/bin/sh", "-c", wrapper])
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), self.root.resolve())
        arguments = json.loads(calls.read_text())
        self.assertIn("-e", arguments)
        self.assertIn("-q", arguments)
        self.assertIn("stty -onlcr", arguments[arguments.index("-c") + 1])
        self.assertEqual(arguments[-1], "/dev/null")

    def test_missing_remote_tty_tool_is_an_explicit_failure(self):
        wrapper = self.emacs('(princ (copilot-cs--tty-shell-command "true"))')
        self.env["PATH"] = str(self.bin)
        result = self.run_command(["/bin/sh", "-c", wrapper])
        self.assertEqual(result.returncode, 127)
        self.assertIn("requires util-linux script", result.stderr)

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

    def test_completed_stream_closes_without_polling(self):
        self.executable(
            "completed-stream",
            """import sys, time
print("source text", flush=True)
marker = "__COPILOT_CS_DONE_job-stream-fixture__:" + sys.argv[1] + "\\n"
split = int(sys.argv[2])
if split:
    print(marker[:split], end="", flush=True)
    time.sleep(0.1)
    print(marker[split:], end="", flush=True)
else:
    print(marker, end="", flush=True)
time.sleep(5)
print("shell closed: exit status 255", file=sys.stderr, flush=True)
raise SystemExit(255)
""",
        )
        marker = "__COPILOT_CS_DONE_job-stream-fixture__:"
        for code, split in ((0, 0), (23, 0), (10, len(marker) - 4), (10, len(marker) + 1)):
            with self.subTest(code=code, split=split):
                command = f"exec {shlex.quote(str(self.bin / 'completed-stream'))} {code} {split}"
                self.emacs(
                    f"""(progn
                      (copilot-cs-use nil ".")
                      (let* ((job (copilot-cs--start "job-stream-fixture" {json.dumps(command)}))
                             (process (plist-get job :process))
                             (buffer (plist-get job :buffer))
                             (deadline (+ (float-time) 3)))
                        (unwind-protect
                            (progn
                              (setq copilot-cs--last-id "other-job")
                              (while (and (process-live-p process)
                                          (< (float-time) deadline))
                                (accept-process-output nil 0.05))
                              (unless (and (not (process-live-p process))
                                           (eq (copilot-cs--state job) 'done)
                                           (equal (copilot-cs--rc job) {code})
                                           (equal copilot-cs--last-id "other-job")
                                           (string-match-p "source text" (copilot-cs--text job))
                                           (not (string-match-p "shell closed"
                                                                (copilot-cs--text job))))
                                (error "Completed stream stayed open or changed the job result")))
                          (when (process-live-p process) (delete-process process))
                          (when (buffer-live-p buffer) (kill-buffer buffer)))))"""
                )

    def test_fragmented_completion_waits_for_the_exit_codes_newline(self):
        marker = "__COPILOT_CS_DONE_job-state-fixture__:"
        output = marker + "10\n"
        for split in (len(marker) - 4, len(marker) + 1):
            with self.subTest(split=split):
                self.check_job(
                    f"""(copilot-cs--filter-output job process {json.dumps(output[:split])})
                       (unless (and (process-live-p process) (null (copilot-cs--rc job)))
                         (error "A partial completion marker closed the stream"))
                       (copilot-cs--filter-output job process {json.dumps(output[split:])})
                       (unless (and (not (process-live-p process))
                                    (equal (copilot-cs--rc job) 10))
                         (error "The complete exit code was not preserved"))""",
                    live=True,
                )

    def test_completed_stream_retains_already_received_transport_diagnostics(self):
        self.check_job(
            """(copilot-cs--filter-output
                 job process
                 "source text\\n__COPILOT_CS_DONE_job-state-fixture__:0\\nshell closed: exit status 255\\n")
               (unless (and (not (process-live-p process))
                            (equal (copilot-cs--rc job) 0)
                            (string-match-p "source text" (copilot-cs--output-text job))
                            (string-match-p "shell closed: exit status 255"
                                            (copilot-cs--output-text job)))
                 (error "Completion discarded diagnostics or changed the command result"))""",
            live=True,
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

    def test_detached_poll_is_passive_and_preserves_the_original_output(self):
        self.check_job(
            """(cl-letf (((symbol-function 'copilot-cs--resume)
                         (lambda (_) (error "Polling opened a connection")))
                        ((symbol-function 'copilot-cs--start)
                         (lambda (&rest _) (error "Polling started a process"))))
                (dotimes (_ 2)
                  (let ((report (copilot-cs-poll id 0)))
                    (unless (and (string-match-p "state=detached" report)
                                 (string-match-p "copilot-cs-attach" report)
                                 (string-match-p "availability" report)
                                 (string-match-p "start it if needed" report)
                                 (not (string-match-p "approval" report))
                                 (string-match-p "progress" report))
                      (error "Incorrect passive report: %s" report))))
                (unless (equal (copilot-cs-output id) "progress")
                  (error "Polling discarded the original log")))""",
            output="__COPILOT_CS_ACK_job-state-fixture__\nprogress\n",
        )

    def test_explicit_attachment_reconnects_to_original_target(self):
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
                             (setq refreshed (list :id key :started (float-time)))))
                          ((symbol-function 'copilot-cs--settle)
                           (lambda (job _) job))
                          ((symbol-function 'copilot-cs--report) #'identity))
                  (unless (and (eq (copilot-cs-attach id 0) refreshed)
                               (equal (plist-get refreshed :started)
                                      (plist-get job :started)))
                    (error "Acknowledged job was not resumed"))
                  (unless (and (equal copilot-cs-id "different-codespace")
                               (equal copilot-cs-dir "/workspaces/different"))
                    (error "Reconnect changed the selected target"))))""",
            output="__COPILOT_CS_ACK_job-state-fixture__\nprogress\n",
        )

    def test_attaching_a_live_or_completed_job_does_not_open_another_connection(self):
        for output, live, expected in (
            ("", True, "connecting"),
            ("__COPILOT_CS_ACK_job-state-fixture__\n", True, "running"),
            ("__COPILOT_CS_DONE_job-state-fixture__:0\n", False, "done"),
        ):
            with self.subTest(state=expected):
                self.check_job(
                    f"""(cl-letf (((symbol-function 'copilot-cs--start)
                                  (lambda (&rest _) (error "Duplicate connection")))
                                 ((symbol-function 'copilot-cs--resume)
                                  (lambda (_) (error "Duplicate attachment"))))
                        (unless (string-match-p "state={expected}"
                                                (copilot-cs-attach id 0))
                          (error "Existing job state changed")))""",
                    output=output, live=live,
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


class JobEnvironmentTests(HelperTestCase):
    def run_locale_job(self, codespace="fixture-codespace"):
        command = 'printf "locale=%s|%s|%s\\\\n" "${LANG-}" "${LC_ALL-}" "${LC_CTYPE-}"'
        locale = "\n".join(
            f'(setenv "{name}" {json.dumps(self.env[name]) if name in self.env else "nil"})'
            for name in ("LANG", "LC_ALL", "LC_CTYPE")
        )
        return self.emacs(
            f"""(progn
              (require 'cl-lib)
              {locale}
              (copilot-cs-use {json.dumps(codespace) if codespace else "nil"}
                              {json.dumps(str(self.root))})
              (let ((copilot-cs-remote-dir
                     (shell-quote-argument {json.dumps(str(self.root))})))
                (cl-letf (((symbol-function 'copilot-cs--argv)
                           (lambda (command &optional _sign _id)
                             (list "sh" "-c" command))))
                  (princ (copilot-cs-sh {json.dumps(command)} 3)))))"""
        )

    def test_codespace_defaults_an_unset_or_empty_lang_to_utf8(self):
        self.env.pop("LC_ALL")
        for value in (None, ""):
            with self.subTest(lang=value):
                if value is not None:
                    self.env["LANG"] = value
                self.assertIn("locale=C.UTF-8||", self.run_locale_job())

    def test_codespace_preserves_explicit_locale_settings(self):
        self.env.update({"LANG": "en_US.UTF-8", "LC_ALL": "C", "LC_CTYPE": "C"})
        self.assertIn("locale=en_US.UTF-8|C|C", self.run_locale_job())

    def test_local_jobs_do_not_change_the_operator_locale(self):
        self.env.pop("LC_ALL")
        self.assertIn("locale=||", self.run_locale_job(codespace=None))


class UnsignedJobTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.config = self.root / ".gitconfig"
        self.config.write_text(
            "[user]\nname = Fixture\nemail = fixture@example.invalid\n"
            "[commit]\ngpgsign = true\n[tag]\ngpgsign = true\n"
        )
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_COUNT="1",
                        GIT_CONFIG_KEY_0="fixture.preserved", GIT_CONFIG_VALUE_0="preserved")
        self.executable(
            "bash",
            """import os, sys
assert sys.argv[1] == "-lc" and len(sys.argv) == 4
os.environ.update(GIT_CONFIG_COUNT="3", GIT_CONFIG_KEY_1="commit.gpgsign",
                  GIT_CONFIG_VALUE_1="true", GIT_CONFIG_KEY_2="tag.gpgsign",
                  GIT_CONFIG_VALUE_2="true")
os.execv("/bin/sh", ["sh", "-c", sys.argv[2], sys.argv[3]])
""",
        )

    def job(self, command, login=False, local=False):
        return self.emacs(
            f"""(progn
              (require 'cl-lib)
              (copilot-cs-use {"nil" if local else '"fixture-codespace"'}
                              {json.dumps(str(self.root))})
              (let ((copilot-cs-remote-dir
                     (shell-quote-argument {json.dumps(str(self.root))})))
                (cl-letf (((symbol-function 'copilot-cs--argv)
                           (lambda (command &optional _sign _id)
                             (list "sh" "-c" command))))
                  (princ ({"copilot-cs-login-sh" if login else "copilot-cs-sh"}
                          {json.dumps(command)} 3)))))"""
        )

    def test_plain_and_login_children_are_unsigned_without_losing_other_config(self):
        command = ("sh -c 'git config --get commit.gpgsign; git config --get tag.gpgsign; "
                   "git config --get fixture.preserved'")
        before = self.config.read_bytes()
        for login in (False, True):
            with self.subTest(login=login):
                result = self.job(command, login=login)
                self.assertIn("state=done rc=0", result)
                self.assertIn("false\nfalse\npreserved", result)
        self.assertEqual(self.config.read_bytes(), before)

    def test_actual_commit_and_annotated_tag_are_unsigned(self):
        result = self.job(
            "git init -q --template= && git commit -q --allow-empty -m unsigned && "
            "git tag -a fixture -m unsigned && "
            "git cat-file commit HEAD && git cat-file tag fixture"
        )
        self.assertIn("state=done rc=0", result)
        self.assertNotIn("gpgsig", result)
        self.assertNotIn("BEGIN SSH SIGNATURE", result)
        self.assertIn("unsigned", result)

    def test_local_test_mode_keeps_signing_defaults(self):
        result = self.job("git config --get commit.gpgsign; git config --get tag.gpgsign", local=True)
        self.assertIn("true\ntrue", result)

    def test_malformed_config_counts_fail_explicitly_before_the_command(self):
        for count in ("invalid", "-1", "999999999999999999999999999999"):
            with self.subTest(count=count):
                self.env["GIT_CONFIG_COUNT"] = count
                result = self.job("printf command-ran")
                self.assertIn("state=done rc=125", result)
                self.assertIn("GIT_CONFIG_COUNT", result)
                self.assertNotIn("command-ran", result)

    def test_decimal_counts_with_leading_zeros_are_normalized(self):
        self.env["GIT_CONFIG_COUNT"] = "0001"
        result = self.job("git config --get fixture.preserved; git config --get commit.gpgsign")
        self.assertIn("preserved\nfalse", result)

    def test_endorsement_publication_can_keep_terminal_dependent_hooks(self):
        self.emacs(
            """(progn
              (require 'cl-lib)
              (copilot-cs-use "fixture-codespace" "/workspaces/fixture")
              (cl-letf (((symbol-function 'copilot-cs--run)
                         (lambda (command _label _wait &optional signing)
                           (unless (and (not signing) (string-match-p "script -q -e -c" command))
                             (error "Publication lost its remote PTY or forwarded the agent")))))
                (let ((plan (make-string 64 ?a)))
                  (copilot-cs-endorse "push" plan (concat plan ":replacement") 0 t)))))"""
        )


class EglotStartupTests(HelperTestCase):
    def test_slow_file_preparation_is_bounded_even_inside_a_timer(self):
        for stage in ("scope", "read", "connect"):
            with self.subTest(stage=stage):
                output = self.emacs(
                    f"""(progn
                      (require 'copilot-cs-eglot)
                      (let ((copilot-cs-eglot-preparation-timeout 0.05)
                            (path "/ghcs:fixture:/workspaces/project/file.rb")
                            (buffer (generate-new-buffer "eglot-start-fixture"))
                            (started (float-time)))
                        (unwind-protect
                            (cl-letf (((symbol-function 'copilot-cs-eglot--basic-path-p)
                                       (lambda (_) t))
                                      ((symbol-function 'copilot-cs-eglot--editable-path-p)
                                       (lambda (_)
                                         {('(sleep-for 2)' if stage == 'scope' else '')} t))
                                      ((symbol-function 'find-file-noselect)
                                       (lambda (_)
                                         {('(sleep-for 2)' if stage == 'read' else '')} buffer))
                                      ((symbol-function 'eglot-current-server) (lambda () nil))
                                      ((symbol-function 'eglot--guess-contact) (lambda () nil))
                                      ((symbol-function 'eglot)
                                       (lambda (&rest _)
                                         {('(sleep-for 2)' if stage == 'connect' else '')})))
                              (copilot-cs-eglot-start path 'ruby-mode)
                              (while (and (eq (plist-get (gethash path copilot-cs-eglot--states)
                                                        :status) 'starting)
                                          (< (- (float-time) started) 3))
                                (accept-process-output nil 0.01))
                              (unless (< (- (float-time) started) 0.5)
                                (error "Eglot preparation blocked the daemon"))
                              (princ (copilot-cs-eglot-status path)))
                          (kill-buffer buffer))))"""
                )
                self.assertIn("state=error", output)
                self.assertIn("preparation timed out", output)

    def test_initialization_is_nonblocking_and_retains_the_starting_server(self):
        self.emacs(
            """(progn
              (require 'copilot-cs-eglot)
              (let ((buffer (generate-new-buffer "eglot-start-fixture"))
                    (path "/ghcs:fixture:/workspaces/project/file.rb")
                    (eglot-sync-connect t))
                (unwind-protect
                    (cl-letf (((symbol-function 'copilot-cs-eglot--editable-path-p)
                               (lambda (_) t))
                              ((symbol-function 'find-file-noselect)
                               (lambda (_)
                                 (when eglot-sync-connect
                                   (error "blocking mode-hook handshake"))
                                 buffer))
                              ((symbol-function 'eglot-current-server) (lambda () nil))
                              ((symbol-function 'eglot--guess-contact) (lambda () nil))
                              ((symbol-function 'eglot)
                               (lambda (&rest _)
                                 (when eglot-sync-connect (error "blocking LSP handshake"))
                                 (run-hook-with-args 'eglot-server-initialized-hook 'fixture-server)
                                 nil))
                              ((symbol-function 'copilot-cs-eglot--complete-start) #'ignore))
                      (copilot-cs-eglot--record path 'starting :mode 'ruby-mode)
                      (copilot-cs-eglot--start-now path 'ruby-mode)
                      (let ((state (gethash path copilot-cs-eglot--states)))
                        (unless (and (eq (plist-get state :status) 'starting)
                                     (eq (plist-get state :buffer) buffer)
                                     (eq (plist-get state :server) 'fixture-server))
                          (error "Lost pending server or startup failed: %S" state)))
                      (unless eglot-sync-connect
                        (error "Changed the caller's synchronous-connect policy")))
                  (kill-buffer buffer))))"""
        )

    def test_stopping_a_startup_prevents_queued_and_inflight_file_reads_from_connecting(self):
        for inflight in (False, True):
            with self.subTest(inflight=inflight):
                self.emacs(
                    f"""(progn
                      (require 'cl-lib)
                      (require 'copilot-cs-eglot)
                      (let ((path "/ghcs:fixture:/workspaces/project/file.rb")
                            (buffer (generate-new-buffer "eglot-cancel-fixture"))
                            (connections 0))
                        (unwind-protect
                            (cl-letf (((symbol-function 'copilot-cs-eglot--basic-path-p)
                                       (lambda (_) t))
                                      ((symbol-function 'copilot-cs-eglot--editable-path-p)
                                       (lambda (_) t))
                                      ((symbol-function 'find-file-noselect)
                                       (lambda (_)
                                         (copilot-cs-eglot-stop path)
                                         buffer))
                                      ((symbol-function 'eglot)
                                       (lambda (&rest _) (cl-incf connections))))
                              (copilot-cs-eglot-start path 'ruby-mode)
                              {('' if inflight else '(copilot-cs-eglot-stop path)')}
                              (sleep-for 0.1)
                              (unless (and (zerop connections)
                                           (eq (plist-get (gethash path copilot-cs-eglot--states)
                                                          :status) 'stopped))
                                (error "Cancelled startup opened another connection")))
                          (kill-buffer buffer))))"""
                )

    def test_repeated_start_does_not_queue_another_connection(self):
        self.emacs(
            """(progn
              (require 'copilot-cs-eglot)
              (let ((path "/ghcs:fixture:/workspaces/project/file.rb")
                    (timers 0))
                (cl-letf (((symbol-function 'copilot-cs-eglot--basic-path-p) (lambda (_) t))
                          ((symbol-function 'run-at-time)
                           (lambda (&rest _) (cl-incf timers))))
                  (copilot-cs-eglot-start path 'ruby-mode)
                  (copilot-cs-eglot-start path 'ruby-mode)
                  (let ((rejected nil))
                    (condition-case nil (copilot-cs-eglot-start path 'go-mode)
                      (error (setq rejected t)))
                    (unless (and (= timers 1) rejected)
                      (error "Repeated startup changed mode or queued a connection"))))))"""
        )

    def test_failed_server_is_reported_immediately_with_its_stderr(self):
        output = self.emacs(
            """(progn
              (require 'copilot-cs-eglot)
              (let ((path "/ghcs:fixture:/workspaces/project/file.rb")
                    (buffer (generate-new-buffer "eglot-start-fixture"))
                    (stderr (generate-new-buffer "eglot-stderr-fixture")))
                (unwind-protect
                    (cl-letf (((symbol-function 'eglot-current-server) (lambda () nil))
                              ((symbol-function 'eglot-managed-p) (lambda () nil))
                              ((symbol-function 'jsonrpc-running-p) (lambda (_) nil))
                              ((symbol-function 'jsonrpc-stderr-buffer) (lambda (_) stderr)))
                      (with-current-buffer stderr (insert "fixture language-server failure"))
                      (copilot-cs-eglot--record path 'starting :mode 'ruby-mode
                                               :buffer buffer :server 'fixture-server :owned t)
                      (copilot-cs-eglot--complete-start path 'ruby-mode buffer (+ (float-time) 60))
                      (princ (copilot-cs-eglot-status path)))
                  (kill-buffer buffer)
                  (kill-buffer stderr))))"""
        )
        self.assertIn("state=error", output)
        self.assertIn("exited during startup", output)
        self.assertIn("fixture language-server failure", output)

    def test_old_completion_cannot_overwrite_a_new_start(self):
        self.emacs(
            """(progn
              (require 'copilot-cs-eglot)
              (let* ((path "/ghcs:fixture:/workspaces/project/file.rb")
                     (token (make-symbol "new-start"))
                     (state (copilot-cs-eglot--record path 'starting :mode 'ruby-mode
                                                    :token token)))
                (copilot-cs-eglot--complete-start path 'ruby-mode nil 0 'old-start)
                (unless (eq state (gethash path copilot-cs-eglot--states))
                  (error "Old startup completion overwrote the new attempt"))))"""
        )

    def test_preparation_timeout_stops_only_the_server_created_by_that_attempt(self):
        self.emacs(
            """(progn
              (require 'copilot-cs-eglot)
              (let* ((path "/ghcs:fixture:/workspaces/project/file.rb")
                     (buffer (generate-new-buffer "eglot-start-fixture"))
                     (process (make-process :name "eglot-timeout-fixture" :command '("cat")
                                            :connection-type 'pipe :noquery t))
                     (server (make-instance 'eglot-lsp-server :name "eglot-timeout-fixture"
                                            :process process :on-shutdown #'ignore))
                     (copilot-cs-eglot-preparation-timeout 0.05))
                (unwind-protect
                    (cl-letf (((symbol-function 'copilot-cs-eglot--editable-path-p)
                               (lambda (_) t))
                              ((symbol-function 'find-file-noselect) (lambda (_) buffer))
                              ((symbol-function 'eglot-current-server) (lambda () nil))
                              ((symbol-function 'eglot--guess-contact) (lambda () nil))
                              ((symbol-function 'eglot)
                               (lambda (&rest _)
                                 (run-hook-with-args 'eglot-server-initialized-hook server)
                                 (sleep-for 2))))
                      (copilot-cs-eglot--record path 'starting :mode 'ruby-mode)
                      (copilot-cs-eglot--start-now path 'ruby-mode)
                      (unless (and (not (process-live-p process))
                                   (eglot--inhibit-autoreconnect server)
                                   (eq (plist-get (gethash path copilot-cs-eglot--states)
                                                  :status) 'error))
                        (error "Timed-out startup retained a live language server")))
                  (when (process-live-p process) (delete-process process))
                  (kill-buffer buffer))))"""
        )

    def test_remote_sorbet_prefers_repository_binstub(self):
        (self.root / "sorbet").mkdir()
        (self.root / "sorbet" / "config").touch()
        self.executable("srb", "import json, sys\nprint(json.dumps(sys.argv[1:]))")
        self.executable("bundle", "raise SystemExit('must use the repository binstub')")
        self.env["PATH"] = str(self.bin)
        command = self.emacs(
            """(progn
              (require 'copilot-cs-eglot)
              (cl-letf (((symbol-function 'copilot-cs-eglot--project-root)
                         (lambda (_) "/ghcs:fixture:/workspaces/project/"))
                        ((symbol-function 'copilot-cs-eglot--ghcs-root-p) (lambda (_) t))
                        ((symbol-function 'copilot-cs-eglot--remote-contact)
                         (lambda (_project command &optional _login) command)))
                (princ (copilot-cs-eglot-ruby-contact))))"""
        )
        result = self.run_command(["/bin/sh", "-c", command])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         ["typecheck", "--lsp", "--cache-dir", "tmp/sorbet", "--disable-watchman"])

        (self.bin / "srb").chmod(0o600)
        self.executable("bundle", "import json, sys\nprint(json.dumps(sys.argv[1:]))")
        self.executable("watchman", "raise SystemExit(0)")
        result = self.run_command(["/bin/sh", "-c", command])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         ["exec", "srb", "typecheck", "--lsp", "--cache-dir", "tmp/sorbet"])

        (self.root / "sorbet" / "config").unlink()
        self.executable("ruby-lsp", "print('ruby-lsp fixture')")
        result = self.run_command(["/bin/sh", "-c", command])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ruby-lsp fixture")

    def test_local_sorbet_also_prefers_the_repository_binstub(self):
        (self.root / "sorbet").mkdir()
        (self.root / "sorbet" / "config").touch()
        self.executable("srb", "raise SystemExit(0)")
        project = f"(cons 'transient {json.dumps(str(self.root) + '/')})"
        output = self.emacs(
            f"""(progn (require 'copilot-cs-eglot)
                 (prin1 (copilot-cs-eglot-ruby-contact nil {project})))"""
        )
        self.assertEqual(output, '("bin/srb" "typecheck" "--lsp" "--cache-dir" "tmp/sorbet")')


class EglotProjectQueryTests(HelperTestCase):
    def project_query(self, assertions, language="ruby"):
        extension = {"ruby": "rb", "go": "go"}[language]
        return self.emacs(
            f"""(progn
              (require 'copilot-cs-eglot)
              (require 'tramp)
              (let* ((tramp-methods (cons '("ghcs" (tramp-login-program "false"))
                                          tramp-methods))
                     (copilot-cs-eglot--states (make-hash-table :test #'equal))
                     (root "/ghcs:fixture:/workspaces/project/")
                     (anchor (concat root "anchor.{extension}"))
                     (target (concat root "sub/target.{extension}"))
                     (anchor-buffer (generate-new-buffer "eglot-project-anchor"))
                     (target-buffer (generate-new-buffer "eglot-project-target"))
                     (process (make-process :name "eglot-project-fixture"
                                            :command '("cat")
                                            :connection-type 'pipe :noquery t))
                     (server (make-instance 'eglot-lsp-server
                                            :name "eglot-project-fixture"
                                            :process process :on-shutdown #'ignore))
                     (scope-allowed t)
                     (same-project t)
                     (file-present t)
                     (cancel-during-read nil)
                     (slow-stage nil)
                     (file-reads 0)
                     (activations 0)
                     requests diagnostic-path)
                (unwind-protect
                    (progn
                      (setf (eglot--project server) (cons 'transient root)
                            (eglot--languages server)
                            '(({language}-mode . "{language}")
                              ({language}-ts-mode . "{language}")))
                      (with-current-buffer anchor-buffer
                        (setq-local buffer-file-name anchor
                                    major-mode '{language}-mode
                                    eglot--cached-server server
                                    eglot--managed-mode t))
                      (with-current-buffer target-buffer
                        (setq-local buffer-file-name target
                                    major-mode '{language}-mode
                                    eglot--cached-server server)
                        (insert "class Target\\n  def example; end\\nend\\n"))
                      (copilot-cs-eglot--record
                       anchor 'ready :mode '{language}-mode
                       :buffer anchor-buffer :server server)
                      (cl-letf
                          (((symbol-function 'copilot-cs-eglot--editable-path-p)
                            (lambda (_)
                              (when (eq slow-stage 'scope) (sleep-for 2))
                              scope-allowed))
                           ((symbol-function 'file-in-directory-p)
                            (lambda (file directory)
                              (unless (and (equal file target) (equal directory root))
                                (error "Unexpected project-boundary check"))
                              same-project))
                           ((symbol-function 'file-regular-p) (lambda (_) file-present))
                           ((symbol-function 'find-file-noselect)
                            (lambda (file &rest _)
                              (unless (equal file target)
                                (error "Opened the representative file instead of the target"))
                              (cl-incf file-reads)
                              (when cancel-during-read (copilot-cs-eglot-stop target))
                              (when (eq slow-stage 'read) (sleep-for 2))
                              target-buffer))
                           ((symbol-function 'eglot)
                            (lambda (&rest _) (error "Query attempted to launch a server")))
                           ((symbol-function 'eglot--maybe-activate-editing-mode)
                            (lambda ()
                              (when (and (eq (current-buffer) target-buffer)
                                         (not eglot--managed-mode))
                                (cl-incf activations)
                                (when (eq slow-stage 'activate) (sleep-for 2))
                                (setq eglot--managed-mode t))))
                           ((symbol-function 'eglot--TextDocumentIdentifier)
                            (lambda () (list :uri buffer-file-name)))
                           ((symbol-function 'eglot--TextDocumentPositionParams)
                            (lambda ()
                              (list :textDocument (list :uri buffer-file-name)
                                    :position (list :line (1- (line-number-at-pos))
                                                    :character (current-column)))))
                           ((symbol-function 'eglot--request)
                            (lambda (connection method params &rest _)
                              (push (list connection method params buffer-file-name)
                                    requests)
                              '(:fixture t)))
                           ((symbol-function 'flymake-diagnostics)
                            (lambda (&rest _)
                              (setq diagnostic-path buffer-file-name)
                              (list (flymake-make-diagnostic
                                     (current-buffer) 1 2 :warning "fixture")))))
                        {assertions}))
                  (when (process-live-p process) (delete-process process))
                  (dolist (buffer (list anchor-buffer target-buffer))
                    (when (buffer-live-p buffer)
                      (with-current-buffer buffer
                        (setq eglot--managed-mode nil)
                        (set-buffer-modified-p nil))
                      (kill-buffer buffer))))))"""
        )

    def test_semantic_queries_adopt_another_file_without_launching_a_server(self):
        self.project_query(
            """(unless (string-match-p "state=unknown" (copilot-cs-eglot-status target))
                 (error "Status should remain passive for an unopened file"))
               (unless (zerop file-reads) (error "Status opened a file"))
               (copilot-cs-eglot-document-symbols target)
               (copilot-cs-eglot-hover target 2 3)
               (copilot-cs-eglot-definition target 2 3)
               (copilot-cs-eglot-references target 2 3)
               (copilot-cs-eglot-diagnostics target)
               (unless (and (= file-reads 1) (= activations 1)
                            (= (length requests) 4) (equal diagnostic-path target)
                            (eq (plist-get (gethash target copilot-cs-eglot--states)
                                           :status) 'ready))
                 (error "Unexpected adoption: reads=%S activations=%S requests=%S diagnostic=%S state=%S"
                        file-reads activations (length requests) diagnostic-path
                        (plist-get (gethash target copilot-cs-eglot--states) :status)))
               (dolist (request requests)
                 (unless (and (eq (nth 0 request) server)
                              (equal (nth 3 request) target)
                              (equal (plist-get (plist-get (nth 2 request) :textDocument)
                                                :uri)
                                     target))
                   (error "Semantic request used the wrong file or server")))
               (unless (string-match-p "state=ready.*server=running"
                                       (copilot-cs-eglot-status target))
                 (error "The adopted file has no ready state"))"""
        )

    def test_tree_sitter_modes_share_the_existing_project_server(self):
        for language in ("ruby", "go"):
            with self.subTest(language=language):
                self.project_query(
                    f"""(with-current-buffer target-buffer
                          (setq major-mode '{language}-ts-mode))
                        (copilot-cs-eglot-document-symbols target)
                        (unless (and (= file-reads 1) (= (length requests) 1)
                                     (eq (caar requests) server)
                                     (eq (plist-get
                                          (gethash target copilot-cs-eglot--states) :mode)
                                         '{language}-ts-mode))
                          (error "A compatible tree-sitter mode did not reuse the server"))""",
                    language=language,
                )

    def test_go_queries_share_the_existing_project_server(self):
        self.project_query(
            """(copilot-cs-eglot-document-symbols target)
               (unless (and (= file-reads 1) (= (length requests) 1)
                            (eq (caar requests) server)
                            (eq (plist-get (gethash target copilot-cs-eglot--states)
                                           :mode) 'go-mode))
                 (error "Go query did not adopt its ready project server"))""",
            language="go",
        )

    def test_unmatched_queries_do_not_open_files_or_start_servers(self):
        for target in (
            "/ghcs:other:/workspaces/project/target.rb",
            "/ghcs:fixture:/workspaces/other/target.rb",
            "/ghcs:fixture:/workspaces/project/target.go",
            "/ghcs:fixture:/workspaces/project/../../etc/target.rb",
            "/tmp/target.rb",
        ):
            with self.subTest(target=target):
                self.project_query(
                    f"""(setq target {json.dumps(target)})
                        (let ((rejected nil))
                          (condition-case nil (copilot-cs-eglot-document-symbols target)
                            (error (setq rejected t)))
                          (unless (and rejected (zerop file-reads) (null requests))
                            (error "An unmatched query opened a file or sent a request")))"""
                )

    def test_missing_or_out_of_scope_files_are_rejected_before_opening(self):
        for flag in ("scope-allowed", "same-project", "file-present"):
            with self.subTest(flag=flag):
                self.project_query(
                    f"""(setq {flag} nil)
                        (let ((reason nil))
                          (condition-case err (copilot-cs-eglot-document-symbols target)
                            (error (setq reason (error-message-string err))))
                          (unless (and reason
                                       (or (string-match-p "Refusing Eglot" reason)
                                           (string-match-p "not a regular file" reason))
                                       (zerop file-reads) (null requests))
                            (error "Missing scope/file validation: %S" reason)))"""
                )

    def test_stopped_or_disconnected_project_servers_are_not_restarted(self):
        for setup in (
            "(copilot-cs-eglot--record anchor 'stopped :server server :buffer anchor-buffer)",
            "(setf (eglot--shutdown-requested server) t)",
            "(delete-process process)",
        ):
            with self.subTest(setup=setup):
                self.project_query(
                    f"""{setup}
                        (let ((rejected nil))
                          (condition-case nil (copilot-cs-eglot-document-symbols target)
                            (error (setq rejected t)))
                          (unless (and rejected (zerop file-reads) (null requests))
                            (error "An inactive project caused a reconnect")))"""
                )

    def test_stopping_during_file_preparation_is_not_overwritten(self):
        self.project_query(
            """(setq cancel-during-read t)
               (let ((rejected nil))
                 (condition-case nil (copilot-cs-eglot-document-symbols target)
                   (error (setq rejected t)))
                 (unless (and rejected (= file-reads 1) (zerop activations) (null requests)
                              (eq (plist-get (gethash target copilot-cs-eglot--states)
                                             :status) 'stopped)
                              (jsonrpc-running-p server))
                   (error "File preparation overwrote cancellation or stopped the shared server")))"""
        )

    def test_a_mismatched_buffer_cannot_use_the_ready_project_server(self):
        for setup in (
            "(setq eglot--cached-server 'other-server)",
            "(setq major-mode 'go-mode)",
        ):
            with self.subTest(setup=setup):
                self.project_query(
                    f"""(with-current-buffer target-buffer {setup})
                        (let ((rejected nil))
                          (condition-case nil (copilot-cs-eglot-document-symbols target)
                            (error (setq rejected t)))
                          (unless (and rejected (= file-reads 1) (zerop activations)
                                       (null requests)
                                       (null (gethash target copilot-cs-eglot--states))
                                       (jsonrpc-running-p server))
                            (error "A mismatched buffer was adopted or sent a request")))"""
                )

    def test_query_file_preparation_remains_bounded(self):
        for stage in ("scope", "read", "activate"):
            with self.subTest(stage=stage):
                self.project_query(
                    f"""(setq slow-stage '{stage})
                        (let ((copilot-cs-eglot-preparation-timeout 0.05)
                              (started (float-time)) reason)
                          (condition-case err (copilot-cs-eglot-document-symbols target)
                            (error (setq reason (error-message-string err))))
                          (unless (and reason (string-match-p "preparation timed out" reason)
                                       (< (- (float-time) started) 0.5)
                                       (null requests) (jsonrpc-running-p server))
                            (error "Query preparation was unbounded or stopped the server: %S"
                                   reason)))"""
                )


class EglotReconnectTests(HelperTestCase):
    def test_codespace_servers_cannot_autoreconnect_after_shutdown(self):
        for root in ("/ghcs:test-codespace:/workspaces/test/",
                     "/ssh:test-host:/workspaces/test/", str(self.root) + "/"):
            for existing in (False, True):
                for delayed in (False, True):
                    with self.subTest(root=root, existing=existing, delayed=delayed):
                        configure = (
                            "(puthash project (list server) eglot--servers-by-project)"
                            "(copilot-cs-eglot-configure)"
                            if existing else
                            "(run-hook-with-args 'eglot-connect-hook server)"
                        )
                        policy = (
                            """(run-at-time
                                 0.1 nil
                                 (lambda ()
                                   (setq timer-fired t)
                                   (setf (eglot--inhibit-autoreconnect server) nil)))"""
                            if delayed else "nil"
                        )
                        output = self.emacs(
                            f"""(progn
                              (require 'copilot-cs-eglot)
                              (require 'tramp)
                              (add-to-list 'tramp-methods
                                           '("ghcs" (tramp-login-program "false")))
                              (let* ((eglot--servers-by-project (make-hash-table :test #'equal))
                                     (project (cons 'transient {json.dumps(root)}))
                                     (process (make-process
                                               :name "eglot-fixture" :command '("cat")
                                               :connection-type 'pipe :noquery t))
                                     (server (make-instance
                                              'eglot-lsp-server :name "eglot-fixture"
                                              :process process
                                              :on-shutdown #'eglot--on-shutdown))
                                     (reconnects 0)
                                     (timer-fired nil)
                                     (timer {policy}))
                                (unwind-protect
                                    (cl-letf (((symbol-function 'eglot-reconnect)
                                               (lambda (&rest _) (cl-incf reconnects))))
                                      (setf (eglot--project server) project
                                            (eglot--inhibit-autoreconnect server) timer)
                                      {configure}
                                      (sleep-for 0.2)
                                      (delete-process process)
                                      (accept-process-output nil 0.05)
                                      (prin1 (list reconnects
                                                   (eq (eglot--inhibit-autoreconnect server) t)
                                                   timer-fired)))
                                  (when (timerp timer) (cancel-timer timer))
                                  (setf (eglot--shutdown-requested server) t)
                                  (when (process-live-p process) (delete-process process)))))"""
                        )
                        expected = "(0 t nil)" if root.startswith("/ghcs:") else (
                            "(1 nil t)" if delayed else "(1 nil nil)"
                        )
                        self.assertEqual(output, expected)


class DaemonLifecycleTests(HelperTestCase):
    def lifecycle(self, expression, setup=""):
        self.env["COPILOT_MCP_PARENT_PID"] = "101"
        init = (SETUP / "copilot-mcp-init.el").read_text()
        lifecycle = init.split(";;; Lifecycle\n", 1)[1].split(";;; MCP server\n", 1)[0]
        return self.emacs(
            f"""(progn
              (require 'cl-lib)
              (let ((attributes '((101 (ppid . 202) (comm . "bash") (start . 10))
                                  (202 (ppid . 303) (comm . "python3") (start . 20))
                                  (303 (ppid . 404) (comm . "/bin/copilot") (start . 30))
                                  (404 (ppid . 1) (comm . "zsh") (start . 40))))
                    (now 100)
                    (stops 0))
                (cl-letf (((symbol-function 'process-attributes)
                           (lambda (pid) (cdr (assq pid attributes))))
                          ((symbol-function 'float-time) (lambda (&rest _) now))
                          ((symbol-function 'kill-emacs) (lambda (&rest _) (cl-incf stops))))
                  {setup}
                  {lifecycle}
                  {expression})))"""
        )

    def test_live_cli_preserves_state_after_its_bridge_exits(self):
        output = self.lifecycle(
            """(setq attributes (assq-delete-all 101 (assq-delete-all 202 attributes))
                     copilot-cs-id "original-codespace"
                     copilot-cs-dir "/workspaces/original"
                     copilot-cs-configured t)
               (puthash "original-job" '(:id "original-job") copilot-cs--jobs)
               (copilot-mcp-watch-parent)
               (setq now 10000)
               (copilot-mcp-watch-parent)
               (prin1 (list copilot-mcp-parent-pid stops copilot-mcp--orphaned-since
                            copilot-cs-id copilot-cs-dir copilot-cs-configured
                            (gethash "original-job" copilot-cs--jobs)))"""
        )
        self.assertEqual(
            output,
            '(303 0 nil "original-codespace" "/workspaces/original" t (:id "original-job"))',
        )

    def test_grace_starts_when_cli_exits_not_when_bridge_exits(self):
        output = self.lifecycle(
            """(setq attributes (assq-delete-all 101 (assq-delete-all 202 attributes)))
               (copilot-mcp-watch-parent)
               (setq now 10000 attributes (assq-delete-all 303 attributes))
               (copilot-mcp-watch-parent)
               (setq now 13599)
               (copilot-mcp-watch-parent)
               (prin1 (list stops copilot-mcp--orphaned-since))
               (setq now 13600)
               (copilot-mcp-watch-parent)
               (prin1 (list stops))"""
        )
        self.assertEqual(output, "(0 10000)(1)")

    def test_reused_cli_pid_does_not_keep_an_orphan_alive(self):
        output = self.lifecycle(
            """(setf (alist-get 'start (cdr (assq 303 attributes))) 31)
               (copilot-mcp-watch-parent)
               (setq now 3700)
               (copilot-mcp-watch-parent)
               (prin1 (list stops))"""
        )
        self.assertEqual(output, "(1)")

    def test_zombie_cli_does_not_keep_an_orphan_alive(self):
        output = self.lifecycle(
            """(setf (alist-get 'state (cdr (assq 303 attributes))) "Z")
               (copilot-mcp-watch-parent)
               (setq now 3700)
               (copilot-mcp-watch-parent)
               (prin1 (list stops))"""
        )
        self.assertEqual(output, "(1)")

    def test_manual_client_retains_bridge_based_grace(self):
        output = self.lifecycle(
            """(setq attributes (assq-delete-all 101 attributes))
               (copilot-mcp-watch-parent)
               (setq now 3699)
               (copilot-mcp-watch-parent)
               (prin1 (list copilot-mcp-parent-pid stops))
               (setq now 3700)
               (copilot-mcp-watch-parent)
               (prin1 (list stops))""",
            setup='(setf (alist-get \'comm (cdr (assq 303 attributes))) "unrelated")',
        )
        self.assertEqual(output, "(101 0)(1)")

    def test_live_runner_outlives_the_owner_grace(self):
        output = self.lifecycle(
            """(setq attributes nil)
               (puthash "active-job" '(:process fixture) copilot-cs--jobs)
               (cl-letf (((symbol-function 'process-live-p) (lambda (p) (eq p 'fixture))))
                 (copilot-mcp-watch-parent)
                 (setq now 3700)
                 (copilot-mcp-watch-parent)
                 (prin1 (list stops))
                 (remhash "active-job" copilot-cs--jobs)
                 (copilot-mcp-watch-parent)
                 (prin1 (list stops)))"""
        )
        self.assertEqual(output, "(0)(1)")

    def test_reattachment_refreshes_owner_without_resetting_state(self):
        output = self.lifecycle(
            """(setq attributes nil)
               (copilot-mcp-watch-parent)
               (setq attributes '((111 (ppid . 999) (comm . "bash") (start . 50))
                                  (999 (ppid . 1) (comm . "copilot") (start . 60)))
                     copilot-cs-id "original-codespace")
               (copilot-mcp-attach-parent 111)
               (setq now 10000 attributes (assq-delete-all 111 attributes))
               (copilot-mcp-watch-parent)
               (prin1 (list copilot-mcp-parent-pid stops copilot-mcp--orphaned-since
                            copilot-cs-id))"""
        )
        self.assertEqual(output, '(999 0 nil "original-codespace")')

    def test_missing_bridge_identity_fails_without_replacing_owner(self):
        output = self.lifecycle(
            """(let ((owner copilot-mcp-parent-pid) (failed nil))
                 (condition-case err
                     (copilot-mcp-attach-parent 111)
                   (error (setq failed (error-message-string err))))
                 (prin1 (list failed (equal owner copilot-mcp-parent-pid))))"""
        )
        self.assertEqual(output, '("Cannot identify MCP bridge process 111" t)')


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
            """import json, os, sys, time
with open(os.environ["FAKE_CLIENT_CALLS"], "a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[-1] == "t":
    with open(os.environ["FAKE_CLIENT_CALLS"]) as calls:
        probes = sum(json.loads(line)[-1] == "t" for line in calls)
    if probes > int(os.environ.get("FAKE_PROBE_DELAY_AFTER", "0")):
        time.sleep(float(os.environ.get("FAKE_PROBE_DELAY", "0")))
    if os.environ.get("FAKE_PROBE_UNREACHABLE"):
        sys.exit(1)
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
              (require 'copilot-cs-eglot)
              (setq copilot-mcp-setup-dir {json.dumps(str(SETUP))}
                    copilot-cs-id "original-codespace"
                    copilot-cs-dir "/workspaces/original"
                    copilot-cs-configured t)
              (puthash "original-job" '(:id "original-job") copilot-cs--jobs)
              (puthash "original-server" '(:state ready) copilot-cs-eglot--states)
              (fset 'copilot-cs--login-shell-command (lambda (_) "stale"))
              (fset 'copilot-cs-eglot--login-command (lambda (_root _command) "stale"))
              (cl-letf (((symbol-function 'process-attributes)
                         (lambda (_pid) '((ppid . 1) (comm . "copilot") (start . 1)))))
                {refresh})
              (unless (advice-member-p #'copilot-mcp--check-dotted-form
                                       'mcp-server-security--check-form-safety)
                (error "Form-safety compatibility was not refreshed"))
              (unless (and (equal copilot-cs-id "original-codespace")
                           (equal copilot-cs-dir "/workspaces/original")
                           copilot-cs-configured
                           (gethash "original-job" copilot-cs--jobs)
                           (equal (gethash "original-server" copilot-cs-eglot--states)
                                  '(:state ready))
                           (string-prefix-p "bash -lc "
                             (copilot-cs-eglot--login-command "/workspaces/example" "server"))
                           (string-prefix-p "env COPILOT_CS_LOGIN_DIR="
                             (copilot-cs--login-shell-command "git push")))
                (error "Runner refresh lost state or retained stale code")))"""
        )

    def test_failed_refresh_does_not_serve_stale_runner(self):
        self.env["FAKE_FAIL_REFRESH"] = "1"
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not refresh runner", result.stderr)
        self.assertFalse(self.served.exists())

    def test_busy_daemon_survives_a_probe_timeout_and_is_reused_when_responsive(self):
        for probes_before_delay in (0, 1):
            with self.subTest(probes_before_delay=probes_before_delay):
                self.calls.unlink(missing_ok=True)
                self.served.unlink(missing_ok=True)
                daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
                pidfile = self.root / "sockets" / "emacs-mcp-server-copilot-test0001.pid"
                pidfile.write_text(str(daemon.pid))
                self.env.update(COPILOT_MCP_PROBE_TIMEOUT="1", FAKE_PROBE_DELAY="2",
                                FAKE_PROBE_DELAY_AFTER=str(probes_before_delay))
                try:
                    result = self.invoke()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("preserving its state", result.stderr)
                    self.assertIsNone(daemon.poll(), result.stderr)
                    self.assertEqual(pidfile.read_text(), str(daemon.pid))
                    self.assertTrue(pidfile.with_suffix(".sock").exists(), result.stderr)
                    self.assertEqual(self.served.exists(), bool(probes_before_delay))
                    self.assertNotIn("replacing", result.stderr)
                    self.env["FAKE_PROBE_DELAY"] = "0"
                    recovered = self.invoke()
                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    self.assertIn("reusing Emacs daemon", recovered.stderr)
                    self.assertIsNone(daemon.poll())
                finally:
                    if daemon.poll() is None:
                        daemon.terminate()
                    daemon.wait(timeout=5)

    def test_unreachable_server_does_not_replace_a_live_daemon(self):
        daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        pidfile = self.root / "sockets" / "emacs-mcp-server-copilot-test0001.pid"
        pidfile.write_text(str(daemon.pid))
        self.env["FAKE_PROBE_UNREACHABLE"] = "1"
        try:
            result = self.invoke()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("preserving its state", result.stderr)
            self.assertIsNone(daemon.poll(), result.stderr)
            self.assertEqual(pidfile.read_text(), str(daemon.pid))
            self.assertTrue(pidfile.with_suffix(".sock").exists(), result.stderr)
        finally:
            if daemon.poll() is None:
                daemon.terminate()
            daemon.wait(timeout=5)

    def test_missing_mcp_socket_does_not_replace_a_responding_daemon(self):
        daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        pidfile = self.root / "sockets" / "emacs-mcp-server-copilot-test0001.pid"
        pidfile.write_text(str(daemon.pid))
        pidfile.with_suffix(".sock").unlink()
        try:
            result = self.invoke()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MCP socket is missing", result.stderr)
            self.assertIn("preserving its state", result.stderr)
            self.assertIsNone(daemon.poll(), result.stderr)
            self.assertEqual(pidfile.read_text(), str(daemon.pid))
            self.assertFalse(self.served.exists())
        finally:
            if daemon.poll() is None:
                daemon.terminate()
            daemon.wait(timeout=5)

    def test_exited_daemon_rebuild_preserves_live_peers(self):
        self.executable(
            "emacsclient",
            """import os
from pathlib import Path
raise SystemExit(0 if (Path(os.environ["HOME"]) / "daemon-started").exists() else 1)
""",
        )
        self.executable(
            "emacs",
            """import os, socket
from pathlib import Path
root = Path(os.environ["HOME"])
(root / "daemon-started").touch()
server = socket.socket(socket.AF_UNIX)
server.bind(str(root / "sockets" / "emacs-mcp-server-copilot-test0001.sock"))
""",
        )
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=5)
        pidfile = self.root / "sockets" / "emacs-mcp-server-copilot-test0001.pid"
        pidfile.write_text(str(exited.pid))
        peer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        live_peer = self.root / "sockets" / "emacs-mcp-server-copilot-live0001.pid"
        dead_peer = self.root / "sockets" / "emacs-mcp-server-copilot-dead0001.pid"
        try:
            for path, pid in ((live_peer, peer.pid), (dead_peer, exited.pid)):
                path.write_text(str(pid))
                self.unix_socket(path.with_suffix(".sock"))
            result = self.invoke()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((self.root / "daemon-started").exists())
            self.assertTrue(self.served.exists())
            self.assertIsNone(peer.poll(), result.stderr)
            self.assertEqual(live_peer.read_text(), str(peer.pid))
            self.assertTrue(live_peer.with_suffix(".sock").exists())
            self.assertFalse(dead_peer.exists())
            self.assertFalse(dead_peer.with_suffix(".sock").exists())
        finally:
            if peer.poll() is None:
                peer.terminate()
            peer.wait(timeout=5)

    def test_reusing_a_healthy_daemon_does_not_probe_other_sessions(self):
        self.unix_socket(self.root / "sockets" / "emacs-mcp-server-copilot-other000.sock")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertTrue(calls)
        self.assertTrue(all(call[:2] == ["-s", "copilot-test0001"] for call in calls), calls)


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
              (cl-letf (((symbol-function 'copilot-cs-sh)
                         (lambda (_command _wait)
                           (list copilot-cs-id copilot-cs-dir))))
                (unless (equal (copilot-cs-stop "original")
                               '("old-codespace" "/workspaces/old"))
                  (error "Cancellation targeted the new Codespace"))))"""
        )


if __name__ == "__main__":
    unittest.main()
