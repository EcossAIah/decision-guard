#!/usr/bin/env python3
"""Shell-write tracing: only text that comes from this session is judged.

Each test runs real shell commands (git, curl, sed, python) between the guard's before/after hooks, with
the judge replaced by a recorder that flags every line it is given. So "judged" = "traced to this session".

Run: python3 tests/test_trace.py
"""
import datetime, json, os, shutil, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("DG_GUARDS") or os.path.join(HERE, "..", "guards"))
import decision_guard as dg  # noqa: E402

OLD = "2026-01-01T00:00:00"
BASE_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
SETUP_ENV = {**BASE_ENV, "GIT_AUTHOR_DATE": OLD, "GIT_COMMITTER_DATE": OLD}  # history made long before
os.environ.update({k: BASE_ENV[k] for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")})


class Trace(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="dg-test-", dir="/tmp"))  # not a Claude temp folder
        dg.STATE = os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(dg.STATE, "sessions"))
        dg.CLAUDE_PROJECTS = os.path.join(self.tmp, "projects")  # session logs, as Claude Code keeps them
        self.proj = os.path.join(dg.CLAUDE_PROJECTS, "-tmp-repo")
        os.makedirs(self.proj)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.judged, self.n = [], 0
        def fake_find_rot(tp, lines, where="", leftover=None, files=None):
            self.judged += lines
            return [(l, 0.9) for l in lines], 0.0
        dg.find_rot = fake_find_rot

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def sh(self, cmd, cwd=None, env=SETUP_ENV):
        r = subprocess.run(cmd, shell=True, cwd=cwd or self.repo, env=env, capture_output=True, text=True,
                           executable="/bin/bash")
        self.assertEqual(r.returncode, 0, f"{cmd}\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def write(self, rel, text, root=None):
        p = os.path.join(root or self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def age(self, root=None):
        """Make every file look old, as if it was there long before this session's commands."""
        t = time.time() - 300
        for d, dirs, files in os.walk(root or self.repo):
            dirs[:] = [x for x in dirs if x != ".git"]
            for f in files:
                os.utime(os.path.join(d, f), (t, t))

    def git_repo(self, files, branches=None):
        self.sh("git init -q -b main")
        for rel, text in files.items():
            self.write(rel, text)
        self.sh("git add -A && git commit -qm init")
        for b, bfiles in (branches or {}).items():
            self.sh(f"git switch -q -c {b}")
            for rel, text in bfiles.items():
                self.write(rel, text)
            self.sh(f"git add -A && git commit -qm {b}")
            self.sh("git switch -q main")
        self.age()

    def log_path(self, sid):
        return os.path.join(self.proj, sid + ".jsonl")

    def typed(self, sid, tool_input, name="Write", tid=None):
        """A tool call written into the session's log, the way Claude Code logs it before the tool runs."""
        self.n += 1
        tid = tid or f"x{self.n}"
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        rec = {"type": "assistant", "timestamp": ts, "sessionId": sid,
               "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name,
                                                             "input": tool_input}]}}
        with open(self.log_path(sid), "a") as f:
            f.write(json.dumps(rec) + "\n")
        return tid

    def run_cmd(self, cmd, cwd=None, during=None, sid="me"):
        """One shell command the way Claude Code runs it: guard before, the command, guard after."""
        cwd = cwd or self.repo
        tid = self.typed(sid, {"command": cmd}, name="Bash")
        data = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": sid,
                "tool_use_id": tid, "cwd": cwd, "transcript_path": self.log_path(sid), "tool_input": {"command": cmd}}
        dg.bash_pre(data)
        self.sh(cmd, cwd=cwd, env=BASE_ENV)
        if during:
            during()
        self.judged = []
        out = dg.bash_post({**data, "hook_event_name": "PostToolUse"})
        return {dg.STATUS_SUFFIX.sub("", l) for l in self.judged}, out

    def prime(self, cwd=None):
        """The session's first command: the guard takes its first copy of every doc."""
        return self.run_cmd("true", cwd=cwd)

    # ---- this session's own text: judged ----
    def test_echo_is_judged(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        got, out = self.run_cmd('echo "- Decision: use Postgres" >> notes.md')
        self.assertEqual(got, {"- Decision: use Postgres"})
        self.assertIn("fix them now", out["reason"])

    def test_own_script_computed_text_is_judged(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        script = 'parts = ["Deci", "sion: use Re", "dis"]\nprint("".join(parts))\n'
        p = os.path.join(self.repo, "gen.py")
        self.typed("me", {"file_path": p, "content": script})  # this session's Write of the script
        self.write("gen.py", script)
        got, _ = self.run_cmd("python3 gen.py >> notes.md")
        self.assertEqual(got, {"Decision: use Redis"})

    def test_sed_stripping_label_is_judged(self):
        self.git_repo({"a.md": "Proposal: Postgres for the database\n"})
        self.prime()
        got, out = self.run_cmd("sed -i '' 's/^Proposal: //' a.md")
        self.assertEqual(got, {"Postgres for the database"})
        self.assertIn("fix them now", out["reason"])

    def test_sed_then_commit_in_same_command_is_judged(self):
        self.git_repo({"a.md": "Proposal: Use Kafka for events\n"})
        self.prime()
        got, _ = self.run_cmd("sed -i '' 's/^Proposal: //' a.md && git commit -qam x")
        self.assertEqual(got, {"Use Kafka for events"})

    def test_git_switch_plus_echo_judges_only_echo(self):
        self.git_repo({"notes.md": "# Notes\n"}, {"feat": {"notes.md": "# Notes\nBranch decision: ship v2 Friday\n"}})
        self.prime()
        got, _ = self.run_cmd('git switch -q feat && echo "- Agent line: we picked Fly.io" >> notes.md')
        self.assertEqual(got, {"- Agent line: we picked Fly.io"})

    def test_write_commit_switch_back_judged_again(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        got, _ = self.run_cmd('git switch -q -c work && echo "- Mine: deploy on Tuesdays" >> notes.md '
                              '&& git commit -qam w')
        self.assertEqual(got, {"- Mine: deploy on Tuesdays"})
        self.run_cmd("git switch -q main")
        got, _ = self.run_cmd("git switch -q work")
        # the commit was made by this session, so it is not "history from before": its text is still the session's
        self.assertEqual(got, {"- Mine: deploy on Tuesdays"})

    def test_unchecked_line_follows_a_move(self):
        self.git_repo({"a.md": "# A\nPending: rollout plan agreed\n"})
        self.prime()
        d = dg.session_dir("me")
        dg.add_pending(d, [{"line": "Pending: rollout plan agreed", "path": os.path.join(self.repo, "a.md"),
                            "t": time.time(), "tries": 1}])
        got, _ = self.run_cmd("mv a.md moved.md")
        self.assertEqual(got, {"Pending: rollout plan agreed"})

    # ---- text from elsewhere: skipped ----
    def test_git_switch_skipped(self):
        self.git_repo({"notes.md": "# Notes\n"}, {"feat": {"notes.md": "# Notes\nBranch decision: ship v2 Friday\n",
                                                          "DESIGN.md": "We chose Fly.io\n"}})
        self.prime()
        self.assertEqual(self.run_cmd("git switch -q feat")[0], set())

    def test_git_checkout_reset_merge_stash_skipped(self):
        self.git_repo({"notes.md": "# Notes\n", "b.md": "base\n"},
                      {"f1": {"notes.md": "# Notes\nF1 line: queue is SQS\n"},
                       "f2": {"b.md": "base\nF2 line: cache is Redis\n"}})
        self.prime()
        self.assertEqual(self.run_cmd("git checkout -q f1 -- notes.md")[0], set())
        self.sh("git checkout -q main -- notes.md")
        self.age()
        self.assertEqual(self.run_cmd("git reset -q --hard f1")[0], set())
        self.assertEqual(self.run_cmd("git merge -q --no-edit f2")[0], set())
        self.write("notes.md", "# Notes\nF1 line: queue is SQS\nStashed: someone's draft line\n")
        self.sh("git stash -q")
        self.age()
        self.assertEqual(self.run_cmd("git stash pop -q")[0], set())

    def test_git_pull_skipped(self):
        self.git_repo({"notes.md": "# Notes\n"})
        clone = os.path.join(self.tmp, "clone")
        self.sh(f"git clone -q {self.repo} {clone}")
        self.write("notes.md", "# Notes\nUpstream: we moved to Postgres 17\n")
        self.sh("git commit -qam up")
        self.age(clone)
        self.prime(cwd=clone)
        self.assertEqual(self.run_cmd("git pull -q --no-rebase", cwd=clone)[0], set())

    def test_git_worktree_add_skipped(self):  # the warning seen live while building this
        self.git_repo({"CHANGELOG.md": "- **Change:** per-session file lock\n"})
        self.prime()
        got, _ = self.run_cmd("git worktree add -q ../wt -b wtb && cd ../wt && git log --oneline -1")
        self.assertEqual(got, set())

    def test_quoting_branch_text_is_not_writing_it(self):
        self.git_repo({"notes.md": "# Notes\n"}, {"feat": {"notes.md": "# Notes\nBranch decision: ship v2 Friday\n"}})
        self.prime()
        self.run_cmd('git show feat:notes.md | grep -c "Branch decision: ship v2 Friday"')
        got, _ = self.run_cmd('git switch -q feat && echo "- Agent line here now" >> notes.md')
        self.assertEqual(got, {"- Agent line here now"})

    def test_copy_move_skipped(self):
        self.git_repo({"a.md": "# A\nExisting: the API is REST\n"})
        self.prime()
        self.assertEqual(self.run_cmd("cp a.md b.md")[0], set())
        self.assertEqual(self.run_cmd("python3 -c \"import shutil; shutil.copy('a.md', 'c.md')\"")[0], set())
        self.assertEqual(self.run_cmd("mv b.md d.md")[0], set())

    def test_reformat_and_rewrap_skipped(self):
        self.git_repo({"a.md": "* Item: Postgres for the DB\n\n| a | b |\n|---|---|\n| x  |  y |\n\n"
                               "This paragraph is long and\nwrapped over two lines.\n"})
        self.prime()
        self.assertEqual(self.run_cmd("sed -i '' 's/^\\* /- /' a.md")[0], set())
        self.assertEqual(self.run_cmd("sed -i '' 's/| x  |  y |/| x | y |/' a.md")[0], set())
        cmd = ("python3 -c \"p='a.md'; t=open(p).read().replace('long and\\nwrapped', 'long and wrapped'); "
               "open(p,'w').write(t)\"")
        self.assertEqual(self.run_cmd(cmd)[0], set())

    def test_download_skipped(self):
        outside = os.path.join(self.tmp, "outside")
        self.write("spec.md", "We decided to deprecate v1 (vendor note)\n", root=outside)
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        self.assertEqual(self.run_cmd(f"curl -s -o spec.md file://{outside}/spec.md")[0], set())
        self.assertEqual(self.run_cmd(f"curl -s file://{outside}/spec.md > spec2.md")[0], set())

    def test_project_script_not_written_by_session_skipped(self):
        self.git_repo({"notes.md": "# Notes\n", "tools/gen.py": "print('Feed: vendor chose gRPC')\n"})
        self.prime()
        self.assertEqual(self.run_cmd("python3 tools/gen.py >> notes.md")[0], set())

    def test_other_writer_during_command_skipped(self):
        self.git_repo({"notes.md": "# Notes\n", "other.md": "# Other\n"})
        self.prime()
        def editor():
            with open(os.path.join(self.repo, "other.md"), "a") as f:
                f.write("Editor: user typed this decision\n")
        self.assertEqual(self.run_cmd("sleep 0.1", during=editor)[0], set())

    def test_other_session_during_own_code_skipped(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        line = "Other session: we will use Kafka"
        dg.session_dir("other")  # that session runs the guard too: its guard judges its own writes
        def other():
            self.typed("other", {"file_path": os.path.join(self.repo, "notes.md"), "content": line})
            with open(os.path.join(self.repo, "notes.md"), "a") as f:
                f.write(line + "\n")
        self.assertEqual(self.run_cmd("python3 -c 'pass'", during=other)[0], set())

    # ---- the one true collision: judged, but the agent is told not to edit ----
    def test_unknown_writer_while_own_code_runs_is_shared(self):
        self.git_repo({"notes.md": "# Notes\n", "other.md": "# Other\n"})
        self.prime()
        def unknown():
            with open(os.path.join(self.repo, "other.md"), "a") as f:
                f.write("Unknown: someone wrote this meanwhile\n")
        got, out = self.run_cmd("python3 -c 'pass'", during=unknown)
        self.assertEqual(got, {"Unknown: someone wrote this meanwhile"})
        self.assertIn("do NOT edit", out["reason"])
        self.assertNotIn("fix them now", out["reason"])

    # ---- review fixes (checker pass, 2026-11-04) ----
    def test_plumbing_commit_then_checkout_is_judged(self):
        self.git_repo({"DECISIONS.md": "# Decisions\n"})
        self.prime()
        # command 1: own text into a commit on another branch, no working-tree doc touched
        self.run_cmd("b=$(printf '# Decisions\\n- Decision: move to Mongo\\n' | git hash-object -w --stdin) && "
                     "git update-index --add --cacheinfo 100644,$b,DECISIONS.md && "
                     "t=$(git write-tree) && c=$(git commit-tree $t -p HEAD -m x) && git branch side $c && "
                     "git reset -q")
        got, _ = self.run_cmd("git checkout side -- DECISIONS.md")
        self.assertEqual(got, {"- Decision: move to Mongo"})

    def test_backdated_own_commit_is_judged(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        self.run_cmd("git switch -q -c side && printf '# Notes\\n- Decision: drop tests\\n' > notes.md && "
                     "GIT_AUTHOR_DATE=2020-01-01T00:00:00 GIT_COMMITTER_DATE=2020-01-01T00:00:00 git commit -qam x "
                     "&& git switch -q main")
        got, _ = self.run_cmd("git merge -q side")
        self.assertEqual(got, {"- Decision: drop tests"})

    def test_decoded_write_is_judged(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        import base64
        b = base64.b64encode(b"- Decision: ship on Fridays\n").decode()
        got, _ = self.run_cmd(f"echo {b} | base64 -d >> notes.md")
        self.assertEqual(got, {"- Decision: ship on Fridays"})

    def test_session_without_guard_started_during_command_not_trusted(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        line = "Child session: we will use Kafka"
        def child():  # e.g. a `claude -p` this command started with hooks off: no guard state
            self.typed("child", {"file_path": os.path.join(self.repo, "notes.md"), "content": line})
            with open(os.path.join(self.repo, "notes.md"), "a") as f:
                f.write(line + "\n")
        self.assertEqual(self.run_cmd("python3 -c 'pass'", during=child)[0], {line})

    def test_short_line_not_credited_to_other_session(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        dg.session_dir("other")
        self.typed("other", {"command": "echo 'Decision: yes, the team agreed on that one' >> log.md"}, name="Bash")
        def gen():
            with open(os.path.join(self.repo, "notes.md"), "a") as f:
                f.write("Decision: yes\n")
        self.assertEqual(self.run_cmd("python3 -c 'pass'", during=gen)[0], {"Decision: yes"})

    def test_hook_denied_call_in_other_session_not_counted(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        dg.session_dir("other")
        line = "- Decision: the API moves to SOAP"
        tid = self.typed("other", {"file_path": os.path.join(self.repo, "notes.md"), "content": line})
        with open(self.log_path("other"), "a") as f:  # Claude Code logs a hook's denial as a tool error
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "is_error": True,
                 "content": "PreToolUse:Write hook error: DECISION GUARD: ..."}]}}) + "\n")
        def gen():
            with open(os.path.join(self.repo, "notes.md"), "a") as f:
                f.write(line + "\n")
        self.assertEqual(self.run_cmd("python3 -c 'pass'", during=gen)[0], {line})

    def test_lines_kept_if_hook_killed_while_judging(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        def killed(*a, **k):
            raise KeyboardInterrupt  # stands in for Claude Code killing the hook mid-judge
        dg.find_rot = killed
        with self.assertRaises(KeyboardInterrupt):
            self.run_cmd('echo "- Decision: use Redis" >> notes.md')
        pend = json.load(open(os.path.join(dg.session_dir("me"), "pending.json")))
        self.assertEqual([e["line"] for e in pend], ["- Decision: use Redis"])

    # ---- commit security review (2026-11-04) ----
    def test_sed_combined_flags_judged(self):
        self.git_repo({"plan.md": "# Plan\nProposal: Use SQLite\nProposal: Use Redis\n"})
        self.prime()
        got, _ = self.run_cmd("sed -Ei '' 's/^Proposal: (Use S)/\\1/' plan.md")  # BSD sed: -E, -i ''
        self.assertEqual(got, {"Use SQLite"})
        got, _ = self.run_cmd("sed -i.bak 's/^Proposal: //' plan.md")
        self.assertEqual(got, {"Use Redis"})

    def test_local_clone_of_old_repo_trusted(self):
        self.git_repo({"notes.md": "# Notes\n- Decision: old text\n"})
        self.prime()
        got, _ = self.run_cmd(f"git clone -q {self.repo} copy")
        self.assertEqual(got, set())

    def test_perl_grouped_inplace_judged(self):
        self.git_repo({"plan.md": "# Plan\nProposal: Use SQLite\n"})
        self.prime()
        got, _ = self.run_cmd("perl -0777pi -e 's/Proposal: //' plan.md")
        self.assertEqual(got, {"Use SQLite"})

    def test_inplace_sed_that_runs_a_command_is_own_code(self):
        cw = dg.command_writers(["sed -i '' '1e python3 gen.py' a.md"], self.repo, set())
        self.assertTrue(cw["any"])
        cw = dg.command_writers(["sed -i '' 's/a/b/' x.md y.md"], self.repo, set())
        self.assertFalse(cw["any"])
        self.assertTrue(any("y.md" in ws for ws, _ in cw["named"]))

    def test_find_exec_sed_judged(self):
        self.git_repo({"docs/plan.md": "# Plan\nProposal: Use SQLite\n"})
        self.prime()
        got, _ = self.run_cmd("find docs -name '*.md' -exec sed -i '' 's/^Proposal: //' {} +")
        self.assertEqual(got, {"Use SQLite"})

    def test_encoded_plumbing_commit_then_checkout_judged(self):
        import base64
        self.git_repo({"DECISIONS.md": "# Decisions\n"})
        self.prime()
        b = base64.b64encode(b"# Decisions\n- Decision: move to Mongo\n").decode()
        self.run_cmd(f"h=$(echo {b} | base64 -d | git hash-object -w --stdin) && "
                     "git update-index --add --cacheinfo 100644,$h,DECISIONS.md && t=$(git write-tree) && "
                     "c=$(git commit-tree $t -p HEAD -m x) && git branch side $c && git reset -q")
        got, _ = self.run_cmd("git checkout side -- DECISIONS.md")
        self.assertEqual(got, {"- Decision: move to Mongo"})

    def test_repo_made_by_session_not_history(self):
        import base64
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        b = base64.b64encode(b"- Decision: drop the tests\n").decode()
        self.run_cmd("mkdir sub && cd sub && git init -q && "
                     f"h=$(echo {b} | base64 -d | git hash-object -w --stdin) && "
                     "git update-index --add --cacheinfo 100644,$h,d.md && t=$(git write-tree) && "
                     "c=$(GIT_COMMITTER_DATE=2020-01-01T00:00:00 GIT_AUTHOR_DATE=2020-01-01T00:00:00 "
                     "git commit-tree $t -m x) && git update-ref refs/heads/main $c")
        got, _ = self.run_cmd("cd sub && git checkout -- d.md")
        self.assertEqual(got, {"- Decision: drop the tests"})

    def test_session_opened_during_command_trusted(self):
        """Another guarded session opened while this command ran: its text is its own guard's to judge."""
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        line = "New session: we will use Kafka"
        def opened():
            dg.session_dir("newer")
            self.typed("newer", {"file_path": os.path.join(self.repo, "notes.md"), "content": line})
            with open(os.path.join(self.repo, "notes.md"), "a") as f:
                f.write(line + "\n")
        self.assertEqual(self.run_cmd("python3 -c 'pass'", during=opened)[0], set())

    def test_read_only_detection(self):
        ro, rw = dg.is_read_only, lambda c: not dg.is_read_only(c)
        self.assertTrue(ro("sed -n '1,5p' a.md") and ro("git branch") and ro("find . -name '*.md'")
                        and ro("sed -n '/emoji/p' a.md") and ro("sed 's/foo/emoji/' a.md")
                        and ro("sed -E 's/(we|e)/x/g' a.md") and ro("sed 's|/usr|/opt|g' a.md")
                        and ro("sed -n -e '1,5p' -e '/week/p' a.md") and ro("sed '/^#/d;s/ *$//' a.md")
                        and ro("sed -n '/start/,/end/{/x/p}' a.md") and ro("sed ':a;N;$!ba;s/\\n/ /g' a.md")
                        and ro("sed 'y/abc/xyz/' a.md") and ro("sed -n '$=' a.md") and ro("sed 10q a.md")
                        and ro("sed --quiet --expr='1,3p' a.md") and ro("sed -nE 's/(a)/b/p' a.md")
                        and ro("sed -n ':top;/x/p;n;btop' a.md") and ro("sed -- 's/a/b/' a.md"))
        for c in ("sed -Ei 's/a/b/' a.md", "sed 's/a/b/w out.md' a.md", "awk '{print > \"o.md\"}' a",
                  "find . -delete", "sort -o out.md a", "git branch side abc123", "git tag v1",
                  "gsed '1e python3 gen.py' a.md", "sed 's/x/date/e' a.md", "sort --compress-program=sh a",
                  "sort -ro out.md a", "perl -0777pi -e 's/a/b/' a.md", "git diff --output=out.md",
                  "git log --output out.md", "git reflog expire --all",
                  "sed 's|x|y|e' a.md", "sed 's#a#b#w out.md' a.md", "sed '1!e date' a.md", "sed '/x/{e date\n}' a.md",
                  "sed --expression='1e date' a.md", "sed -e '1e date' a.md", "sed -e1e a.md", "sed -f s.sed a.md",
                  "sed --file=s.sed a.md", "sed -n -e 'w out.md' a.md", "sed '1edate' a.md", "sed '1s/a/b/e' a.md",
                  "sed '/re/s/a/b/w f' a.md", "sed 's/a/b' a.md", "sed '\\%x%e date' a.md", "sed '$e date' a.md",
                  "sed '1,5wout.md' a.md", "sed '/a/,/b/Wx' a.md", "sed 'y/ab/cd/;e' a.md",
                  "sed -n -e p -e p -e 'w o.md' a.md", "sed ':a;e date' a.md", "sed ':a;w o.md' a.md",
                  "sed --expr='1e date' a.md", "sed --ex '1e date' a.md", "sed --fi=s.sed a.md", "sed --in-pl 's/a/b/' a.md",
                  "sed --bogus 's/a/b/' a.md", "sed -nl 5 'e date' a.md", "sed -l 'w o.md' a.md", "sed -ne'1e date' a.md",
                  "sed -x 's/a/b/' a.md", "sed -nf s.sed a.md", "sed -l 5 'e date' a.md", "sed --line-length=5 'w o.md' a.md",
                  "sed -l x 's/a/b/' a.md", "sed 'w o.md' -e p a.md", "sed -n 'w o.md' -e p a.md"):
            self.assertTrue(rw(c), c)

    # ---- tools that give the hook no session log: the guard's own saved log ----
    def run_no_log(self, cmd, sid, prompt=None):
        """One command from a tool with no session log: the hook gets no transcript_path."""
        self.n += 1
        if prompt:
            p = {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": prompt}
            dg.use_saved_log(p)
        data = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": sid, "tool_use_id": f"n{self.n}",
                "cwd": self.repo, "transcript_path": None, "tool_input": {"command": cmd}}
        dg.use_saved_log(data)
        dg.bash_pre(data)
        self.sh(cmd, env=BASE_ENV)
        self.judged = []
        post = {**data, "hook_event_name": "PostToolUse", "transcript_path": None}
        dg.use_saved_log(post)
        out = dg.bash_post(post)
        return {dg.STATUS_SUFFIX.sub("", l) for l in self.judged}, out, data["transcript_path"]

    def test_no_log_own_echo_judged_and_prompt_saved(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.run_no_log("true", "cli1")
        got, out, saved = self.run_no_log('echo "- Decision: use Postgres" >> notes.md', "cli1",
                                          prompt="please add a note about the database")
        self.assertEqual(got, {"- Decision: use Postgres"})
        self.assertIn("fix them now", out["reason"])
        self.assertTrue(saved.endswith(dg.SAVED_CALLS))
        self.assertIn("please add a note about the database", dg.conversation(saved))  # the judge sees the user

    def test_no_log_git_switch_skipped(self):
        self.git_repo({"plan.md": "# Plan\n"}, {"other": {"plan.md": "# Plan\nDecision: use Kafka\n"}})
        self.run_no_log("true", "cli2")
        got, _, _ = self.run_no_log("git switch -q other", "cli2")
        self.assertEqual(got, set())

    def test_no_log_other_session_text_skipped(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.run_no_log("true", "cli3")
        dg.use_saved_log({"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "cli-b",  # also no log
                          "tool_use_id": "b1", "transcript_path": None,
                          "tool_input": {"command": 'echo "- Decision: ship Friday" >> notes.md'}})
        def other():
            with open(os.path.join(self.repo, "notes.md"), "a") as f:
                f.write("- Decision: ship Friday\n")
        self.n += 1
        data = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "cli3", "tool_use_id": "slow",
                "cwd": self.repo, "transcript_path": None, "tool_input": {"command": "python3 -c 'pass'"}}
        dg.use_saved_log(data)
        dg.bash_pre(data)
        other()
        self.judged = []
        post = {**data, "hook_event_name": "PostToolUse", "transcript_path": None}
        dg.use_saved_log(post)
        dg.bash_post(post)
        self.assertEqual(set(self.judged), set())

    def test_claude_code_writes_no_saved_log(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        self.run_cmd('echo "- x" >> notes.md')
        self.assertFalse(os.path.exists(os.path.join(dg.session_dir("me"), dg.SAVED_CALLS)))

    # ---- other features unchanged ----
    def test_write_tool_still_strict(self):
        p = self.write("x.md", "* Item: Postgres\n")
        _, lines = dg.lines_for_tool("Write", {"file_path": p, "content": "- Item: Redis\n"})
        self.assertEqual(lines, ["- Item: Redis"])


    # ---- text written in Claude's temp folder, then brought into a real doc ----
    def _temp_doc(self, text):
        root = f"/private/tmp/claude-{os.getuid()}"  # the real Claude temp folder
        os.makedirs(root, exist_ok=True)
        d = tempfile.mkdtemp(prefix="dgtest-", dir=root)
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "plan.md")
        self.typed("me", {"file_path": p, "content": text})  # this session's Write
        with open(p, "w") as f:
            f.write(text)
        t = time.time() - 600  # written well before the next command
        os.utime(p, (t, t))
        return p

    def test_temp_doc_moved_into_repo_is_judged(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        src = self._temp_doc("- Decision: the user picked Postgres\n")
        got, _ = self.run_cmd(f"mv {src} plan.md")
        self.assertEqual(got, {"- Decision: the user picked Postgres"})

    def test_temp_doc_moved_over_existing_doc_is_judged(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        src = self._temp_doc("# Notes\n- Decision: the user picked Postgres\n")
        got, _ = self.run_cmd(f"mv {src} notes.md")
        self.assertEqual(got, {"- Decision: the user picked Postgres"})

    def test_temp_doc_copied_keeping_dates_is_judged(self):
        self.git_repo({"notes.md": "# Notes\n"})
        self.prime()
        src = self._temp_doc("- Decision: the user picked Postgres\n")
        got, _ = self.run_cmd(f"cp -p {src} plan.md")
        self.assertEqual(got, {"- Decision: the user picked Postgres"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
