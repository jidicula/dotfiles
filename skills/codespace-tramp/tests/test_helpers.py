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
        for status in (401, 403, 404, 410):
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

    def test_queued_connections_stop_after_three_refusals(self):
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

    def test_copies_and_commands_share_the_refusal_limit(self):
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
        self.assertFalse(self.sleeps.exists())

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
        for _ in range(4):
            self.assertEqual(self.invoke("ssh", "test-codespace", "remote-command").returncode, 1)
        self.assertEqual(self.gate_path.read_text(), "0")
        self.assertEqual(len(self.recorded_calls()), 5)

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
        for error in (
            "Agent returned SSH_AGENT_FAILURE",
            "HTTP 403: Must have admin rights to Repository",
            "WARNING: UNPROTECTED PRIVATE KEY FILE!",
        ):
            with self.subTest(error=error):
                self.env["FAKE_GH_ERROR"] = error
                for _ in range(4):
                    self.assertEqual(self.invoke("ssh", "test-codespace", "run-once").returncode, 1)
        self.assertEqual(len(self.recorded_calls()), 12)

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
respond(1, {"protocolVersion": "2024-11-05", "capabilities": {}})
assert json.loads(sys.stdin.readline())["method"] == "notifications/initialized"
call = json.loads(sys.stdin.readline())
assert call["method"] == "tools/call"
assert call["params"] == {"name": "eval-elisp", "arguments": {"expression": "(copilot-cs-status)"}}
respond(2, {"content": [{"type": "text", "text": "fixture jobs"}]})
sys.stdin.read()
""",
        )
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "fixture jobs")

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
        for value in ("NaN", "inf", "-inf", "0"):
            with self.subTest(value=value):
                self.env["COPILOT_MCP_CALL_TIMEOUT"] = value
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("finite and greater than zero", result.stderr)

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
              (require 'copilot-cs-eglot)
              (setq copilot-mcp-setup-dir {json.dumps(str(SETUP))}
                    copilot-cs-id "original-codespace"
                    copilot-cs-dir "/workspaces/original"
                    copilot-cs-configured t)
              (puthash "original-job" '(:id "original-job") copilot-cs--jobs)
              (puthash "original-server" '(:state ready) copilot-cs-eglot--states)
              (fset 'copilot-cs--login-shell-command (lambda (_) "stale"))
              (fset 'copilot-cs-eglot--login-command (lambda (_root _command) "stale"))
              {refresh}
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
