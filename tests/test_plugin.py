#!/usr/bin/env python3
"""Plugin packaging: the manifests, where the key and state come from, and the session-start setup check.

Nothing is sent anywhere. Run: python3 tests/test_plugin.py
"""
import json, os, shutil, subprocess, sys, tempfile, unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK = os.path.join(REPO, "hooks", "check-setup.sh")
KEY_VARS = ("CLAUDE_PLUGIN_OPTION_OPENROUTER_API_KEY", "OPENROUTER_API_KEY", "DECISION_GUARD_ENV_FILE")


def load(*path):
    with open(os.path.join(REPO, *path)) as f:
        return json.load(f)


def python(code, **env):
    """Run code in a fresh Python, so module-level settings are read from this env."""
    clean = {k: v for k, v in os.environ.items() if k not in KEY_VARS + ("DECISION_GUARD_STATE", "CLAUDE_PLUGIN_DATA")}
    r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {os.path.join(REPO, 'guards')!r}); {code}"],
                       env={**clean, **env}, capture_output=True, text=True, check=True)
    return r.stdout.strip()


class Manifests(unittest.TestCase):
    def test_marketplace_lists_this_plugin(self):
        plugin, market = load(".claude-plugin", "plugin.json"), load(".claude-plugin", "marketplace.json")
        self.assertEqual([p["name"] for p in market["plugins"]], [plugin["name"]])

    def test_key_is_masked(self):
        opt = load(".claude-plugin", "plugin.json")["userConfig"]["openrouter_api_key"]
        self.assertTrue(opt["sensitive"])

    def test_hooks_match_manual_install(self):
        """A plugin user gets the same protection as a manual install: same events, same tools, same time limit."""
        plugin, manual = load("hooks", "hooks.json")["hooks"], load("settings.example.json")["hooks"]
        for event, entries in manual.items():
            self.assertEqual(plugin[event][0]["matcher"], entries[0]["matcher"], event)
            self.assertEqual(plugin[event][0]["hooks"][0]["timeout"], entries[0]["hooks"][0]["timeout"], event)
            self.assertIn("${CLAUDE_PLUGIN_ROOT}/guards/decision_guard.py", plugin[event][0]["hooks"][0]["command"])

    def test_hook_scripts_exist(self):
        for name in ("guards/decision_guard.py", "guards/judge.py", "hooks/check-setup.sh"):
            self.assertTrue(os.path.exists(os.path.join(REPO, name)), name)


class KeyAndState(unittest.TestCase):
    KEY = "import judge; judge.ENV_FILES = ['/nonexistent']; print(judge.api_key())"
    STATE = "import decision_guard as d; print(d.STATE)"

    def test_plugin_key_wins(self):
        self.assertEqual(python(self.KEY, CLAUDE_PLUGIN_OPTION_OPENROUTER_API_KEY="plugin", OPENROUTER_API_KEY="env"), "plugin")

    def test_empty_plugin_key_falls_back(self):
        self.assertEqual(python(self.KEY, CLAUDE_PLUGIN_OPTION_OPENROUTER_API_KEY="", OPENROUTER_API_KEY="env"), "env")

    def test_env_file_still_works(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, ".env")
            with open(f, "w") as out:
                out.write("OPENROUTER_API_KEY=fromfile\n")
            self.assertEqual(python("import judge; print(judge.api_key())", DECISION_GUARD_ENV_FILE=f), "fromfile")

    def test_model_order(self):
        code = "import decision_guard as d; print(d.MODEL)"
        self.assertEqual(python(code, DECISION_GUARD_MODEL="a/env", CLAUDE_PLUGIN_OPTION_DECISION_MODEL="b/plugin"), "a/env")
        self.assertEqual(python(code, CLAUDE_PLUGIN_OPTION_DECISION_MODEL="b/plugin"), "b/plugin")
        default = python("import judge; print(judge.DEFAULT_MODEL)")
        self.assertEqual(python(code, CLAUDE_PLUGIN_OPTION_DECISION_MODEL=""), default)

    def test_wrong_model_name_gets_its_own_message(self):
        code = ("import decision_guard as d, judge; "
                "print(d.rejected_message(judge.ContentRejected('HTTP 400: Model a/b does not exist')))")
        msg = python(code, CLAUDE_PLUGIN_OPTION_DECISION_MODEL="a/b")
        self.assertIn('"a/b"', msg)
        self.assertIn("/plugin configure", msg)
        self.assertNotIn("smaller pieces", msg)
        self.assertIn("smaller pieces", python("import decision_guard as d, judge; "
                                               "print(d.rejected_message(judge.ContentRejected('HTTP 413: too big')))"))

    def test_state_order(self):
        self.assertEqual(python(self.STATE, DECISION_GUARD_STATE="/a", CLAUDE_PLUGIN_DATA="/b"), "/a")
        self.assertEqual(python(self.STATE, CLAUDE_PLUGIN_DATA="/b"), "/b")
        self.assertTrue(python(self.STATE).endswith("/.claude/hooks/.decision-guard"))


class SetupCheck(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)

    def run_check(self, path=None, **env):
        clean = {k: v for k, v in os.environ.items() if k not in KEY_VARS}
        r = subprocess.run(["/bin/sh", CHECK], env={**clean, "HOME": self.home, "PATH": path or os.environ["PATH"], **env},
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_silent_when_set_up(self):
        self.assertEqual(self.run_check(CLAUDE_PLUGIN_OPTION_OPENROUTER_API_KEY="k"), "")

    def test_env_file_counts_as_a_key(self):
        os.makedirs(os.path.join(self.home, ".config", "decision-guard"))
        with open(os.path.join(self.home, ".config", "decision-guard", ".env"), "w") as f:
            f.write("OPENROUTER_API_KEY=k\n")
        self.assertEqual(self.run_check(), "")

    def test_empty_env_file_line_is_no_key(self):
        os.makedirs(os.path.join(self.home, ".config", "decision-guard"))
        for empty in ("OPENROUTER_API_KEY=\n", 'OPENROUTER_API_KEY=""\n', "OPENROUTER_API_KEY=''\n", "OPENROUTER_API_KEY=   \n"):
            with open(os.path.join(self.home, ".config", "decision-guard", ".env"), "w") as f:
                f.write(empty)
            self.assertIn("No OpenRouter API key", self.run_check(), empty)

    def test_quoted_env_file_key_counts(self):
        os.makedirs(os.path.join(self.home, ".config", "decision-guard"))
        with open(os.path.join(self.home, ".config", "decision-guard", ".env"), "w") as f:
            f.write('OPENROUTER_API_KEY="k"\n')
        self.assertEqual(self.run_check(), "")

    def test_missing_key_warns_in_valid_json(self):
        out = json.loads(self.run_check())
        self.assertIn("No OpenRouter API key", out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("No OpenRouter API key", out["hookSpecificOutput"]["additionalContext"])

    def test_missing_python_warns(self):
        with tempfile.TemporaryDirectory() as bin_dir:  # only the tools the script needs, no python3
            for tool in ("grep",):
                os.symlink(shutil.which(tool), os.path.join(bin_dir, tool))
            out = json.loads(self.run_check(path=bin_dir, CLAUDE_PLUGIN_OPTION_OPENROUTER_API_KEY="k"))
        self.assertIn("python3 was not found", out["systemMessage"])

    def test_old_python_warns(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            fake = os.path.join(bin_dir, "python3")
            with open(fake, "w") as f:
                f.write("#!/bin/sh\nexit 1\n")
            os.chmod(fake, 0o755)
            os.symlink(shutil.which("grep"), os.path.join(bin_dir, "grep"))
            out = json.loads(self.run_check(path=bin_dir, CLAUDE_PLUGIN_OPTION_OPENROUTER_API_KEY="k"))
        self.assertIn("older than 3.9", out["systemMessage"])

    def test_manual_install_warns(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as f:
            f.write('{"command": "python3 ~/.claude/hooks/decision-guard/decision_guard.py"}')
        out = json.loads(self.run_check(CLAUDE_PLUGIN_OPTION_OPENROUTER_API_KEY="k"))
        self.assertIn("installed by hand", out["systemMessage"])

    def test_several_problems_stay_valid_json(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as f:
            f.write("hooks/decision-guard/decision_guard.py")
        msg = json.loads(self.run_check())["systemMessage"]
        self.assertIn("No OpenRouter API key", msg)
        self.assertIn("installed by hand", msg)

    def test_key_never_printed(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as f:
            f.write("hooks/decision-guard/decision_guard.py")
        self.assertNotIn("SECRETVALUE", self.run_check(CLAUDE_PLUGIN_OPTION_OPENROUTER_API_KEY="SECRETVALUE"))


if __name__ == "__main__":
    unittest.main()
