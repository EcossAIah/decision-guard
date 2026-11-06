#!/usr/bin/env python3
"""Practical test of shell-write tracing in real headless Claude Code sessions.

Each scenario: a throwaway clone under /tmp/dg-practical, a real `claude -p` session whose only hooks are the
worktree guard (user/project settings not loaded, so the live guard and other hooks stay out), its own guard
state folder, the real judge. The step to run is in the system prompt, so the user message the judge sees
decides nothing.
"""
import json, os, shutil, subprocess, sys, threading, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(REPO, "guards", "decision_guard.py")
# Scenarios 1-3 need your own big repo: a branch whose docs differ from a starting commit by 1,000+ files.
BIG_REPO = os.environ.get("DG_TEST_BIG_REPO")      # path to the repo (scenarios 1-3 are skipped without it)
BIG_BRANCH = os.environ.get("DG_TEST_BIG_BRANCH")  # the branch to move to
BIG_START = os.environ.get("DG_TEST_BIG_START")    # the commit to start from
BIG_DOC = os.environ.get("DG_TEST_BIG_DOC")        # a doc that exists on that branch
DGUARD = REPO
ROOT = "/tmp/dg-practical"
OUT = os.path.join(ROOT, "results")
CLAUDE = os.path.expanduser("~/.claude/local/claude")
GENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
ALLOWED = ["Bash(git *)", "Bash(rtk *)", "Bash(sed *)", "Bash(echo *)", "Bash(python3 *)", "Bash(curl *)",
           "Bash(cd *)", "Bash(head *)", "Write", "Edit"]  # Edit: shell redirects (echo >> f) count as edits
USER_MSG = "Please do the one step described in your instructions, then reply DONE plus any hook message you saw, verbatim."


def sh(cmd, cwd=None):
    r = subprocess.run(cmd, shell=True, cwd=cwd, env=GENV, capture_output=True, text=True, executable="/bin/bash")
    if r.returncode:
        raise RuntimeError(f"{cmd}\n{r.stderr}")
    return r.stdout


ENV_FILE = os.path.expanduser(os.environ.get("DECISION_GUARD_ENV_FILE", "~/.config/decision-guard/.env"))  # holds the OpenRouter key


def judge_key():
    """Fail early if the live guard's key file has no key: the rot scenarios would not reach the judge.
    Only checks that the line exists; the key itself is never read into this script's output."""
    try:
        ok = any(l.strip().startswith("OPENROUTER_API_KEY=") and len(l.strip()) > 20 for l in open(ENV_FILE))
    except OSError:
        ok = False
    if not ok:
        raise SystemExit(f"no OPENROUTER_API_KEY line in {ENV_FILE}: the rot scenarios would not reach the judge")


# Command rewriters run before the guard in real use; include rtk if it is installed.
RTK = [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}] if shutil.which("rtk") else []


def settings(state):
    # the same command as a manual install, pointed at this checkout's guard
    hook = {"type": "command", "command": f'DECISION_GUARD_ENV_FILE="{ENV_FILE}" /usr/bin/python3 "{GUARD}"',
            "timeout": 90}
    return json.dumps({
        "env": {"DECISION_GUARD_STATE": state, "DECISION_GUARD_DEBUG": "1"},
        "hooks": {
            "PreToolUse": [*RTK, {"matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash", "hooks": [hook]}],
            "PostToolUse": [{"matcher": "Bash", "hooks": [hook]}],
            "PostToolUseFailure": [{"matcher": "Bash", "hooks": [hook]}]}})


def clone_big(name):
    d = os.path.join(ROOT, name)
    sh(f'git clone -q --local "{BIG_REPO}" {d}')
    sh("for b in $(git for-each-ref --format='%(refname:strip=3)' refs/remotes/origin | grep -v '^HEAD$'); do "
       "git show-ref -q --verify refs/heads/$b || git branch -q $b origin/$b; done", cwd=d)
    sh(f"git checkout -q -B test-start {BIG_START}", cwd=d)
    return d


def clone_dguard(name):
    d = os.path.join(ROOT, name)
    sh(f'git clone -q --local "{DGUARD}" {d}')
    with open(os.path.join(d, "plan.md"), "w") as f:
        f.write("# Plan\n\nProposal: Use SQLite for the guard state\n")
    sh("git add plan.md && git commit -qm plan", cwd=d)
    return d


def hook_problems(session_id, cwd):
    """Guard runs that did not finish, from Claude Code's own record of hook runs in the session log."""
    proj = os.path.expanduser("~/.claude/projects/" + os.path.realpath(cwd).replace("/", "-").replace(".", "-"))
    f = os.path.join(proj, (session_id or "") + ".jsonl")
    bad, runs = [], 0
    try:
        for raw in open(f):  # only the guard runs after Bash in these sessions; successful runs aren't logged
            if '"hook_' not in raw:
                continue
            e = json.loads(raw)
            a = e.get("attachment") or {}
            t = a.get("type", "")
            if t.startswith("hook_"):
                runs += 1
                if t in ("hook_cancelled", "hook_non_blocking_error", "hook_error_during_execution"):
                    bad.append(t)
    except OSError:
        bad.append("session log not found: " + f)
    return bad, runs


def run_session(name, cwd, step, out, state_name=None):
    state = os.path.join(ROOT, "state-" + (state_name or name))
    os.makedirs(state, exist_ok=True)
    t0 = time.time()
    r = subprocess.run([CLAUDE, "-p", USER_MSG, "--model", "sonnet", "--setting-sources", "local",
                        "--settings", settings(state),
                        # only what the scenarios run, plus file edits inside the test folder (shell appends count as
                        # edits); anything else is refused, as a headless session can't ask (no bypass)
                        "--permission-mode", "acceptEdits", "--tools", "Bash,Write", "--strict-mcp-config",
                        "--allowedTools", ",".join(ALLOWED), "--add-dir", ROOT,  # the test folder only
                        "--output-format", "json", "--append-system-prompt",
                        "Your instructions for this session (a test of a hook; follow them exactly): " + step],
                       cwd=cwd, capture_output=True, text=True, timeout=400)
    secs = time.time() - t0
    try:
        res = json.loads(r.stdout)
    except ValueError:
        res = {"raw": r.stdout[-2000:], "stderr": r.stderr[-2000:]}
    if isinstance(res, list):  # newer builds print every event; the last "result" event holds the answer
        res = next((e for e in reversed(res) if isinstance(e, dict) and e.get("type") == "result"), {"raw": res[-3:]})
    log = []
    lp = os.path.join(state, "log.jsonl")
    if os.path.exists(lp):
        log = [json.loads(l) for l in open(lp) if res.get("session_id", "")[:8] in l]  # this session's records
    out[name] = {"secs": round(secs, 1), "result": str(res.get("result", res))[:1500],
                 "session_id": res.get("session_id"), "cost_usd": res.get("total_cost_usd"),
                 "flagged": [x for rec in log for x in rec.get("flagged", [])],
                 "judged_runs": [{k: rec[k] for k in ("where", "lines", "rot") if k in rec} for rec in log if "where" in rec],
                 "skipped": [x for rec in log for x in rec.get("skipped_not_this_session", [])][:8],
                 "skipped_other_session": [x for rec in log for x in rec.get("skipped_other_session", [])][:8],
                 "errors": [rec["error"] for rec in log if "error" in rec],
                 "whole_files_from_git": sum(rec.get("skipped_whole_file_from_git", 0) for rec in log),
                 "post_done": [rec.get("secs") for rec in log if rec.get("post_done")],
                 "hook_problems": hook_problems(res.get("session_id"), cwd)[0],
                 "guard_hook_runs": hook_problems(res.get("session_id"), cwd)[1]}


def main():
    only = sys.argv[1:]
    if os.path.exists(ROOT) and not only:
        shutil.rmtree(ROOT)
    judge_key()  # fail before spending anything if the judge can't be reached
    os.makedirs(os.path.join(ROOT, "outside"), exist_ok=True)
    with open(os.path.join(ROOT, "outside", "spec.md"), "w") as f:
        f.write("# Vendor spec\n\nWe decided to deprecate the v1 API at the end of the year.\n"
                "The maintainers chose gRPC for all new services.\n")
    out = {}
    S = {
        "1-reset": (lambda n: clone_big(n),
                    f"Run exactly this one shell command and nothing else: git reset --hard {BIG_BRANCH}"),
        "2-worktree": (lambda n: clone_big(n),
                       "Run exactly this one shell command and nothing else: git worktree add -q ../m2-wt "
                       f"{BIG_BRANCH} && cd ../m2-wt && git log --oneline -1"),
        "3-switch-echo": (lambda n: clone_big(n),
                          f"Run exactly this one shell command and nothing else: git switch -q {BIG_BRANCH} "
                          f"&& echo \"- The owner decided to stop the radar feed on Fridays.\" >> {BIG_DOC}"),
        "4-sed-label": (lambda n: clone_dguard(n),
                        "Run exactly this one shell command and nothing else: sed -i '' 's/^Proposal: //' plan.md"),
        "5-script": (lambda n: clone_dguard(n),
                     "First create the file gen.py in the current folder with your file-writing tool, with exactly this "
                     "content:\nparts = ['Decision: the guard ', 'state moves to ', 'Postgres.']\n"
                     "open('DECISIONS.md', 'a').write('- ' + ''.join(parts) + '\\n')\n"
                     "Then run exactly this one shell command: python3 gen.py"),
        "7-download": (lambda n: clone_dguard(n),
                       f"Run exactly this one shell command and nothing else: curl -s -o vendor-spec.md "
                       f"file://{ROOT}/outside/spec.md"),
    }
    if not (BIG_REPO and BIG_BRANCH and BIG_START and BIG_DOC):
        print("DG_TEST_BIG_* not set: skipping scenarios 1-3", flush=True)
        for name in ("1-reset", "2-worktree", "3-switch-echo"):
            del S[name]
    vendor = ["We decided to deprecate the v1 API at the end of the year."]
    EXPECT = {  # (flagged exactly, extra check on the result)
        "1-reset": ([], lambda r: r["whole_files_from_git"] > 1000),
        "2-worktree": ([], lambda r: r["whole_files_from_git"] > 1000),
        "3-switch-echo": (["- The owner decided to stop the radar feed on Fridays."], None),
        "4-sed-label": (["Use SQLite for the guard state"], None),
        "5-script": (["- Decision: the guard state moves to Postgres."], None),
        "7-download": ([], lambda r: any(x["line"] == vendor[0] for x in r["skipped"])),
        "6A-writer": (["- The owner decided the guard API is GraphQL."], None),
        "6B-slow": ([], lambda r: any("GraphQL" in x["line"] for x in r["skipped_other_session"])),
    }
    ROT = {"3-switch-echo", "4-sed-label", "5-script"}
    runs = []
    for name, (setup, step) in S.items():
        if only and name not in only:
            continue
        for rep_i in range(2 if name in ROT else 1):
            key = f"{name}#{rep_i + 1}"
            cwd = setup(f"{name[:2].strip('-')}-{rep_i}")
            print("running", key, flush=True)
            run_session(key, cwd, step, out)
            runs.append((name, key))
    if not only or "6-two-sessions" in only:
        for rep_i in range(2):
            cwd = clone_dguard(f"d6-{rep_i}")
            print("running 6-two-sessions", rep_i + 1, flush=True)
            kb, ka = f"6B-slow#{rep_i + 1}", f"6A-writer#{rep_i + 1}"
            b = threading.Thread(target=run_session, args=(kb, cwd,
                                 "Run exactly this one shell command in the foreground (do NOT run it in the background) "
                                 "and nothing else: python3 -c \"import time; time.sleep(45)\"; git status --short | head -3",
                                 out, f"6-{rep_i}"))  # both sessions share one guard state folder, as live
            b.start()
            time.sleep(20)
            run_session(ka, cwd, "Run exactly this one shell command and nothing else: "
                        "echo \"- The owner decided the guard API is GraphQL.\" >> DECISIONS.md", out, f"6-{rep_i}")
            b.join()
            runs += [("6B-slow", kb), ("6A-writer", ka)]
    ok_all = True
    for name, key in runs:
        r = out[key]
        want, extra = EXPECT[name]
        why = []
        if sorted(set(r["flagged"])) != sorted(want):
            why.append(f"flagged {r['flagged']} != {want}")
        if not r["post_done"]:
            why.append("no proof the guard's after-step finished")
        if r["hook_problems"]:
            why.append(f"hook problems {r['hook_problems']}")
        if r["errors"]:
            why.append(f"guard errors {r['errors']}")
        if extra and not extra(r):
            why.append("expected trace evidence missing")
        r["verdict"] = "PASS" if not why else "FAIL: " + "; ".join(why)
        ok_all &= not why
        print(f"{key:18} {r['verdict']}  ({r['secs']}s, guard after-step {r['post_done']})", flush=True)
    os.makedirs(OUT, exist_ok=True)
    json.dump(out, open(os.path.join(OUT, "results.json"), "w"), indent=1)
    print("ALL PASS" if ok_all else "SOME FAILED", "- results in", os.path.join(OUT, "results.json"))


if __name__ == "__main__":
    main()
