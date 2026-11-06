#!/usr/bin/env python3
"""Red-team cases for shell-write tracing. Every test here states the behaviour the guard SHOULD have;
a failing test is a proven finding. Reuses the helpers in test_trace.py (judge mocked: judged = traced
to this session).

Run: python3 -W ignore tests/test_trace_redteam.py
"""
import io, json, os, sys, time, unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_trace as tt  # noqa: E402
from test_trace import dg, BASE_ENV  # noqa: E402


class RedTeam(tt.Trace):
    # ================= MISSED ROT: this session's text that is not judged =================

    def test_rot_space_in_filename_crashes_trace(self):
        """`git cat-file --batch` answers '<spec> missing'; with a space in the path that splits into 3 parts
        and int(b'missing') raises. In main() that is caught as an outage: nothing judged, line lost."""
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        got, _ = self.run_cmd('git switch -q -c work && echo "- Decision: use Postgres" >> "My Notes.md"')
        self.assertEqual(got, {"- Decision: use Postgres"})

    def test_rot_space_in_filename_fails_open_in_main(self):
        """Same, through main(): the hook prints 'check skipped' and does not block; the line is not queued."""
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        cmd = 'git switch -q -c work && echo "- Decision: use Postgres" >> "My Notes.md"'
        tid = self.typed("me", {"command": cmd}, name="Bash")
        data = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "me", "tool_use_id": tid,
                "cwd": self.repo, "transcript_path": self.log_path("me"), "tool_input": {"command": cmd}}
        dg.log = lambda rec: None
        dg.bash_pre(data)
        self.sh(cmd, env=BASE_ENV)
        buf = io.StringIO()
        sys.stdin = io.StringIO(json.dumps({**data, "hook_event_name": "PostToolUse"}))
        try:
            with redirect_stdout(buf):
                dg.main()
        finally:
            sys.stdin = sys.__stdin__
        out = buf.getvalue()
        pending = dg.load(dg.session_dir("me"), "pending.json", [])
        self.assertTrue('"block"' in out or pending, f"not blocked and not queued: {out}")

    def test_rot_script_written_by_heredoc(self):
        """Agents often create scripts with `cat > x.py <<EOF`. The script path is not in the word index
        (only whole lines are), so running it later is not 'own code' and computed text is skipped."""
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        self.run_cmd("cat > gen.py <<'EOF'\nparts = ['Deci', 'sion: use Re', 'dis']\nprint(''.join(parts))\nEOF")
        got, _ = self.run_cmd("python3 gen.py >> notes.md")
        self.assertEqual(got, {"Decision: use Redis"})

    def test_rot_python_module_written_by_session(self):
        """`python3 -m pkg.gen`: the module the session wrote is not matched (token 'pkg.gen' is not a path)."""
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        script = "db = 'Postgres'\nprint(f'Decision: we use {db} for storage')\n"
        p = os.path.join(self.repo, "pkg", "gen.py")
        self.typed("me", {"file_path": p, "content": script})
        self.write("pkg/gen.py", script)
        self.write("pkg/__init__.py", "")
        got, _ = self.run_cmd("python3 -m pkg.gen >> notes.md")
        self.assertEqual(got, {"Decision: we use Postgres for storage"})

    def test_rot_subshell_cd_script(self):
        """`(cd tools && python3 gen.py)`: '(cd' and 'gen.py)' tokens defeat cd_dirs and the path match."""
        self.git_repo({"notes.md": "# Notes\n", "tools/keep.txt": "x\n"})
        self.prime()
        script = "db = 'Postgres'\nprint(f'Decision: we use {db} for storage')\n"
        p = os.path.join(self.repo, "tools", "gen.py")
        self.typed("me", {"file_path": p, "content": script})
        self.write("tools/gen.py", script)
        got, _ = self.run_cmd("(cd tools && python3 gen.py >> ../notes.md)")
        self.assertEqual(got, {"Decision: we use Postgres for storage"})

    def test_rot_backdated_commit_in_same_command(self):
        """Evasion: write + commit with an old GIT_COMMITTER_DATE in one command. The new commit is HEAD
        after the command, its date says 'before', so its text counts as git history and is skipped."""
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        got, _ = self.run_cmd('echo "- Decision: use Postgres" >> notes.md && GIT_COMMITTER_DATE=2020-01-01T00:00:00 '
                              'GIT_AUTHOR_DATE=2020-01-01T00:00:00 git commit -qam x')
        self.assertEqual(got, {"- Decision: use Postgres"})

    def test_rot_proposal_copied_into_decisions_doc(self):
        """Label removal across docs: a line from a doc labelled 'Status: Proposal - not approved' is copied
        into DECISIONS.md. 'Another doc on disk had it' skips it, though the label did not come along."""
        self.git_repo({"PROPOSALS.md": "Status: Proposal - not approved\n\nUse Postgres for storage\n",
                       "DECISIONS.md": "# Decisions\n"})
        self.prime()
        got, _ = self.run_cmd("sed -n '3p' PROPOSALS.md >> DECISIONS.md")
        self.assertEqual(got, {"Use Postgres for storage"})

    def test_rot_existing_generator_driven_with_args(self):
        """A project tool (adr-tools style) the session drives with arguments writes 'Status: Accepted'.
        Not typed, not own code -> skipped, though the session chose to create an accepted decision."""
        gen = ("import sys\nt = ' '.join(sys.argv[2:])\n"
               "open('docs/adr-0002.md', 'w').write(f'# 2. {t}\\n\\n## Status\\n\\nStatus: Accepted\\n')\n")
        self.git_repo({"notes.md": "# Notes\n", "tools/adr.py": gen, "docs/adr-0001.md": "# 1. Record\n"})
        self.prime()
        got, _ = self.run_cmd("python3 tools/adr.py new Use Postgres")
        self.assertIn("Status: Accepted", got)

    def test_rot_ai_cli_not_in_list(self):
        """An AI CLI missing from OWN_CODE (here a stand-in named `goose`) writes new text: skipped."""
        cli = "#!/bin/sh\nprintf 'Decision: we adopt %s\\n' Kafka >> DESIGN.md\n"
        self.git_repo({"DESIGN.md": "# Design\n", "bin/goose": cli})
        self.sh("chmod +x bin/goose")
        self.age()
        self.prime()
        got, _ = self.run_cmd('PATH="$PWD/bin:$PATH" goose run -t "record the queue choice in DESIGN.md"')
        self.assertEqual(got, {"Decision: we adopt Kafka"})

    # ================= FALSE BLAME: text not from this session that is judged =================

    def test_blame_download_with_echo(self):
        """`&& echo saved` makes the whole command 'own code'; the downloaded vendor text is judged
        as kind 'own' (file named in command) and the agent is told to 'fix them now'."""
        outside = os.path.join(self.tmp, "outside")
        self.write("spec.md", "We decided to deprecate v1 (vendor note)\n", root=outside)
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        got, out = self.run_cmd(f"curl -s -o spec.md file://{outside}/spec.md && echo saved")
        self.assertEqual(got, set(), out and out.get("reason", "")[-300:])

    def test_blame_jq_pipe_from_download(self):
        """`gh issue view --json body | jq -r .body > issue.md` pattern: jq is own code, text is outside."""
        outside = os.path.join(self.tmp, "outside")
        self.write("issue.json", json.dumps({"body": "Maintainers decided: drop Python 3.8"}), root=outside)
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        got, _ = self.run_cmd(f"curl -s file://{outside}/issue.json | jq -r .body > issue.md")
        self.assertEqual(got, set())

    def test_blame_git_show_redirect_with_echo(self):
        """`git show feat:notes.md > notes.md`: HEAD/refs unchanged and `show` is not a tree op, so git
        history is never searched; with any echo in the command the branch text is judged."""
        self.git_repo({"notes.md": "# Notes\n"}, {"feat": {"notes.md": "# Notes\nBranch decision: ship v2 Friday\n"}})
        self.prime()
        got, _ = self.run_cmd("git show feat:notes.md > notes.md && echo restored")
        self.assertEqual(got, set())

    def test_blame_git_alias_with_echo(self):
        """`git co feat -- notes.md` (alias for checkout) is not matched by GIT_TREE_OPS."""
        self.git_repo({"notes.md": "# Notes\n"}, {"feat": {"notes.md": "# Notes\nBranch decision: ship v2 Friday\n"}})
        self.sh("git config alias.co checkout")
        self.prime()
        got, _ = self.run_cmd("git co feat -- notes.md && echo ok")
        self.assertEqual(got, set())

    def test_blame_merge_in_repo_with_many_branches(self):
        """Only 64 commits are searched: post HEAD (new merge, excluded), pre HEAD, ..., then ref tips in
        sha order. With >64 branches the merged branch's tip is often not searched -> its text is judged."""
        self.git_repo({"notes.md": "# Notes\n", "x.txt": "0\n"})
        self.sh("git switch -q -c target && printf '# Notes\\nBranch decision: ship v2 Friday\\n' > notes.md "
                "&& git commit -qam f && git switch -q main && echo 1 > x.txt && git commit -qam m")
        tip = self.sh("git rev-parse target").strip()
        n = 0
        while sum(1 for l in self.sh("git for-each-ref --format=%\(objectname\)").split() if l < tip) < 80:
            n += 1  # more branches, each its own commit, until 80 tips sort before the target's
            self.sh(f"git update-ref refs/heads/b{n} $(git commit-tree -p main -m b{n} 'main^{{tree}}')")
        br = "target"
        self.age()
        self.prime()
        got, _ = self.run_cmd(f"git merge -q --no-edit {br} && echo merged")
        self.assertEqual(got, set())

    def test_blame_typed_in_search_then_pulled(self):
        """The session once searched for a boilerplate line; a teammate later commits a new doc with that
        line; `git pull` brings it in. 'Typed first' wins over the later commit -> judged, 'fix them now'."""
        self.git_repo({"docs/adr-0001.md": "# 1. Record\n"})
        clone = os.path.join(self.tmp, "clone")
        self.sh(f"git clone -q {self.repo} {clone}")
        self.age(clone)
        self.prime(cwd=clone)
        self.run_cmd('grep -rn "Status: Accepted" docs || true', cwd=clone)
        self.write("docs/adr-0002.md", "# 2. Teammate ADR\n\nStatus: Accepted\n")
        self.sh("git add -A && git commit -qm adr2", env=BASE_ENV)  # real time: after the search
        time.sleep(2.1)
        got, out = self.run_cmd("git pull -q --no-rebase", cwd=clone)
        self.assertEqual(got, set(), out and out.get("reason", "")[-200:])

    # ================= PERFORMANCE =================

    def test_perf_large_words_file(self):
        """A long session (24h kept, up to 16 MB): own_index re-parses all of it on every shell command
        that changed a doc, outside the 4 s trace budget."""
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        body = "\n".join(f"- Line {i}: some *typed* doc text with \"quoted bits\" and more words here"
                         for i in range(4000))
        for i in range(40):  # ~12 MB of log, 40 large Write calls
            self.typed("me", {"file_path": os.path.join(self.repo, f"w{i}.md"), "content": body + str(i)})
        t0 = time.time()
        self.run_cmd('echo "- Decision: use Postgres" >> notes.md')
        took = time.time() - t0
        print(f"\n  bash_pre+cmd+bash_post with 12 MB words: {took:.2f}s", file=sys.stderr)
        self.assertLess(took, 2.0)

    def test_perf_bash_pre_git_overhead(self):
        """bash_pre now runs git rev-parse + for-each-ref per repo on EVERY shell command."""
        self.git_repo({"notes.md": "# Notes\n"})
        self.sh("for i in $(seq 1 3000); do echo \"$(git rev-parse HEAD) refs/tags/t$i\"; done >> .git/packed-refs")
        t0 = time.time()
        for _ in range(20):
            dg.git_pre([self.repo])
        per = (time.time() - t0) / 20
        print(f"\n  git_pre per command (3000 refs): {per * 1000:.0f} ms", file=sys.stderr)
        self.assertLess(per, 0.05)


for _n in dir(tt.Trace):  # only this file's cases (the base class's tests run in test_trace.py)
    if _n.startswith("test_"):
        setattr(RedTeam, _n, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
