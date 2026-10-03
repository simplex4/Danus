"""Offline integration checks: python3 -m unittest discover -s scripts/tests -v."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bootstrap test ", dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "deployment"
        for folder in ("scripts", "bin", "config", "runtime/venv/bin"):
            (self.root / folder).mkdir(parents=True)
        for name in ("bootstrap.sh", "detect-toolchain.sh", "env.sh"):
            shutil.copyfile(ROOT / "scripts" / name, self.root / "scripts" / name)
        shutil.copyfile(ROOT / "bin/codex", self.root / "bin/codex")
        (self.root / "bin/codex").chmod(0o755)
        self.host = Path(self.tmp.name) / "host tools"
        self.host.mkdir()
        self.tool(self.root / "runtime/venv/bin/python", "exit 0")
        self.tool(self.host / "node", '[ "$1" = --version ] && echo v26.9.0; exit 0')
        self.tool(self.host / "npm", '[ "$1" = --version ] && { echo 11; exit 0; }; exit 91')
        self.tool(self.host / "codex", 'printf "%s\\n" "$@"')
        # No test may download or install dependencies.
        self.tool(self.host / "curl", "exit 92")
        # Keep system app installations out of these offline fixtures.
        self.tool(self.host / "uname", '[ "$1" = -s ] && { echo Linux; exit 0; }; echo arm64')
        self.env = {"PATH": f"{self.host}:/usr/bin:/bin", "HOME": str(self.root)}

    def desktop(self, body='printf "%s\\n" "$@"',
                relative="Contents/Resources/codex-cli/bin/codex"):
        self.tool(self.host / "uname", '[ "$1" = -s ] && { echo Darwin; exit 0; }; echo arm64')
        detector = self.root / "scripts/detect-toolchain.sh"
        # Redirect only the system application root; HOME already belongs to
        # this fixture. Never run the real machine's bundled CLI in a test.
        detector.write_text(detector.read_text().replace(
            'set -- /Applications ', 'set -- "$HOME/system-applications" '))
        cli = self.root / "Applications/ChatGPT.app" / relative
        self.tool(cli, body)
        return cli

    def tool(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(0o755)

    def run_shell(self, command):
        return subprocess.run(["/bin/bash", "-c", command], cwd=self.root,
                              env=self.env, text=True, capture_output=True, check=True).stdout

    def bootstrap(self):
        return self.run_shell("bash scripts/bootstrap.sh")

    def selected(self):
        return self.run_shell('. runtime/runtime.env; printf "%s\\n" "$DANUS_NODE" "$DANUS_CODEX_BIN" "$DANUS_CODEX_JS"').splitlines()

    def test_host_tools_and_paths_with_spaces(self):
        self.bootstrap()
        self.assertEqual(self.selected(), [str(self.host / "node"), str(self.host / "codex"), ""])
        self.assertFalse((self.root / "runtime/node22").exists())
        self.assertFalse((self.root / "runtime/codex-npm").exists())
        self.assertEqual(self.run_shell('bin/codex exec "two words"'), "exec\ntwo words\n")

    def test_sourced_environment_prefers_host_over_old_runtime(self):
        node = self.root / "runtime/node22/bin/node"
        self.tool(node, "exit 93")
        self.bootstrap()
        self.run_shell('source scripts/env.sh; export PATH="$DANUS_ROOT/runtime/node22/bin:$PATH"; bash scripts/bootstrap.sh')
        self.assertEqual(self.selected()[0], str(self.host / "node"))

    def test_symlink_to_wrapper_is_skipped(self):
        aliases = self.root / "aliases"
        aliases.mkdir()
        (aliases / "codex").symlink_to(self.root / "bin/codex")
        self.env["PATH"] = f"{aliases}:{self.env['PATH']}"
        self.bootstrap()
        self.assertEqual(self.selected()[1], str(self.host / "codex"))

    def test_broken_host_tools_use_existing_local_installation(self):
        self.desktop("exit 1")
        for name in ("node", "codex"):
            self.tool(self.host / name, "exit 1")
        node = self.root / "runtime/node22/bin/node"
        self.tool(node, 'printf "%s\\n" "$@"')
        self.tool(node.parent / "npm", "exit 91")
        js = self.root / "runtime/codex-npm/lib/node_modules/@openai/codex/bin/codex.js"
        js.parent.mkdir(parents=True)
        js.touch()
        self.bootstrap()
        self.assertEqual(self.selected(), [str(node), "", str(js)])
        self.assertEqual(self.run_shell('bin/codex exec "two words"'), f"{js}\nexec\ntwo words\n")

    def test_desktop_cli_without_path_cli(self):
        cli = self.desktop()
        (self.host / "codex").unlink()
        output = self.bootstrap()
        self.assertEqual(self.selected()[1:], [str(cli), ""])
        self.assertIn("Desktop updates may change its location", output)
        self.assertFalse((self.root / "runtime/codex-npm").exists())
        self.assertEqual(self.run_shell('bin/codex exec "two words"'), "exec\ntwo words\n")

    def test_path_cli_precedes_desktop(self):
        self.desktop()
        output = self.bootstrap()
        self.assertEqual(self.selected()[1], str(self.host / "codex"))
        self.assertNotIn("Desktop updates may change its location", output)

    def test_desktop_internal_layout_is_discovered(self):
        (self.host / "codex").unlink()
        for relative in ["Contents/Resources/codex",
                         "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
                         "Contents/Future Layout/tools/bin/codex"]:
            with self.subTest(relative=relative):
                cli = self.desktop(relative=relative)
                self.assertIn("Desktop updates may change its location", self.bootstrap())
                self.assertEqual(self.selected()[1], str(cli))
                cli.unlink()

    def test_desktop_search_skips_invalid_candidates(self):
        bad = self.desktop("exit 1", relative="Contents/Resources/codex")
        good = self.desktop(relative="Contents/Other Tools/codex")
        nonexec = bad.parent / "disabled/codex"
        self.tool(nonexec, "exit 0")
        nonexec.chmod(0o644)
        (bad.parent / "directory/codex").mkdir(parents=True)
        (self.host / "codex").unlink()
        self.bootstrap()
        self.assertEqual(self.selected()[1], str(good))

    def test_desktop_cli_on_path_still_warns(self):
        cli = self.desktop()
        (self.host / "codex").unlink()
        self.env["PATH"] = f"{cli.parent}:{self.env['PATH']}"
        self.assertIn("Desktop updates may change its location", self.bootstrap())
        self.assertEqual(self.selected()[1], str(cli))

    def test_system_bundle_precedes_user_bundle(self):
        self.desktop()
        cli = self.root / "system-applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
        self.tool(cli, "exit 0")
        (self.host / "codex").unlink()
        self.bootstrap()
        self.assertEqual(self.selected()[1], str(cli))

    def test_desktop_wrapper_alias_is_skipped(self):
        cli = self.desktop()
        cli.unlink()
        cli.symlink_to(self.root / "bin/codex")
        self.assertEqual(self.run_shell(
            'DANUS_ROOT="$PWD"; . scripts/detect-toolchain.sh; '
            'danus_find_desktop_codex && exit 90; exit 0'), "")

    def test_desktop_discovery_is_macos_only(self):
        self.desktop()
        self.tool(self.host / "uname", "echo Linux")
        self.assertEqual(self.run_shell(
            'DANUS_ROOT="$PWD"; . scripts/detect-toolchain.sh; '
            'danus_find_desktop_codex && exit 90; exit 0'), "")

    def test_model_and_codex_home_are_preserved(self):
        self.tool(self.host / "codex", 'printf "%s\\n" "$CODEX_HOME" "$@"')
        (self.root / "config/danus.env").write_text("DANUS_MAIN_MODEL=test-model\n")
        self.bootstrap()
        self.assertEqual(self.run_shell('bin/codex --help'),
                         f"{self.root}/runtime/codex-home\n--model\ntest-model\n--help\n")


if __name__ == "__main__":
    unittest.main()
