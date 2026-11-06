#!/usr/bin/env python3
"""Cost: judge calls that are not needed are skipped, and every paid call is logged.

The judge is replaced by a fake that charges a fixed cost per call, so nothing is sent anywhere.
Run: python3 tests/test_cost.py
"""
import json, os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "guards"))
import decision_guard as dg  # noqa: E402
import judge  # noqa: E402

CLAUDE_TMP = f"/private/tmp/claude-{os.getuid()}"
os.makedirs(CLAUDE_TMP, exist_ok=True)


class Skips(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="dg-cost-", dir="/tmp"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_docs_read_only_tools_judge_nothing(self):
        for t in ("guide", "read", "query", "export", "delete"):
            _, lines = dg.lines_for_tool("mcp__claude_ai_Claude_Docs__" + t, {"items": ["topic.index"], "query": "find the pricing section"})
            self.assertEqual(lines, [], t)

    def test_docs_write_tools_still_judged(self):
        for t in ("batch", "update", "create", "some_new_tool"):
            _, lines = dg.lines_for_tool("mcp__claude_ai_Claude_Docs__" + t, {"payload": "Database: Postgres, as the user decided"})
            self.assertTrue(lines, t)

    def test_notebook_code_cell_skipped_markdown_judged(self):
        code = {"notebook_path": "/x/a.ipynb", "cell_type": "code", "new_source": "import pandas as pd\ndf.head()"}
        md = {"notebook_path": "/x/a.ipynb", "cell_type": "markdown", "new_source": "We chose Postgres."}
        self.assertEqual(dg.lines_for_tool("NotebookEdit", code)[1], [])
        self.assertEqual(dg.lines_for_tool("NotebookEdit", md)[1], ["We chose Postgres."])

    def test_notebook_type_read_from_file_when_not_given(self):
        nb = os.path.join(self.tmp, "a.ipynb")
        json.dump({"cells": [{"id": "c1", "cell_type": "code", "source": []},
                             {"id": "m1", "cell_type": "markdown", "source": []}]}, open(nb, "w"))
        self.assertEqual(dg.lines_for_tool("NotebookEdit", {"notebook_path": nb, "cell_id": "c1", "new_source": "x = 1234"})[1], [])
        self.assertEqual(dg.lines_for_tool("NotebookEdit", {"notebook_path": nb, "cell_id": "m1", "new_source": "We chose Postgres."})[1],
                         ["We chose Postgres."])
        # unknown cell: judged, never silently skipped
        self.assertTrue(dg.lines_for_tool("NotebookEdit", {"notebook_path": nb, "cell_id": "zz", "new_source": "We chose Postgres."})[1])
        # insert after a code cell, no type given: the neighbour's type must not be used
        self.assertTrue(dg.lines_for_tool("NotebookEdit", {"notebook_path": nb, "cell_id": "c1", "edit_mode": "insert",
                                                           "new_source": "We chose Postgres."})[1])

    def test_notebook_without_cell_id_is_judged(self):
        nb = os.path.join(self.tmp, "old.ipynb")  # nbformat < 4.5: cells have no id
        json.dump({"cells": [{"cell_type": "code", "source": []}]}, open(nb, "w"))
        self.assertTrue(dg.lines_for_tool("NotebookEdit", {"notebook_path": nb, "new_source": "We chose Postgres."})[1])

    def test_symlink_from_temp_folder_to_real_doc_is_judged(self):
        real = os.path.join(self.tmp, "plan.md")
        open(real, "w").close()
        link_dir = tempfile.mkdtemp(prefix="dgtest-", dir=CLAUDE_TMP)
        try:
            link = os.path.join(link_dir, "plan.md")
            os.symlink(real, link)
            self.assertTrue(dg.lines_for_tool("Write", {"file_path": link, "content": "We will use Postgres."})[1])
            # and the other way: a doc in the repo that links into the temp folder
            target = os.path.join(link_dir, "t.md")
            open(target, "w").close()
            repo_link = os.path.join(self.tmp, "decisions.md")
            os.symlink(target, repo_link)
            self.assertTrue(dg.lines_for_tool("Write", {"file_path": repo_link, "content": "We will use Postgres."})[1])
        finally:
            shutil.rmtree(link_dir, ignore_errors=True)

    def test_claude_temp_folder_is_judged_cache_is_not(self):
        ti = {"content": "We will use Postgres for storage."}
        self.assertTrue(dg.lines_for_tool("Write", {**ti, "file_path": CLAUDE_TMP + "/p/s/scratchpad/n.md"})[1])
        self.assertTrue(dg.lines_for_tool("Write", {**ti, "file_path": "/tmp/claude-lookalike/n.md"})[1])
        cache = os.path.expanduser("~/.claude/projects/-x/session/tool-results/n.md")  # Claude's own storage
        self.assertEqual(dg.lines_for_tool("Write", {**ti, "file_path": cache})[1], [])
        self.assertTrue(dg.lines_for_tool("Write", {**ti, "file_path": os.path.join(self.tmp, "plan.md")})[1])
        mem = os.path.expanduser("~/.claude/projects/-x/memory/note.md")  # auto-memory is a real doc
        self.assertTrue(dg.lines_for_tool("Write", {**ti, "file_path": mem})[1])


class CostLog(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="dg-cost-", dir="/tmp"))
        dg.STATE = self.tmp
        self.tp = os.path.join(self.tmp, "sess1234.jsonl")
        with open(self.tp, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "use postgres"}}) + "\n")
        self.real = judge.judge
        self.calls = 0

    def tearDown(self):
        judge.judge = self.real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def logged(self):
        return [json.loads(l) for l in open(os.path.join(self.tmp, "log.jsonl"))]

    def test_paid_calls_logged_with_files(self):
        def fake(convo, lines, **k):
            self.calls += 1
            return [("OK", 0.1)] * len(lines), 0.01
        judge.judge = fake
        lines = [f"line number {i} here" for i in range(45)]  # 2 calls of up to 30
        dg.find_rot(self.tp, lines, "Write", files=["/a/plan.md"])
        r = self.logged()[-1]
        self.assertEqual((r["calls"], round(r["cost"], 4), r["files"], r["lines"]), (2, 0.02, ["/a/plan.md"], 45))

    def test_cost_logged_even_when_judging_fails_partway(self):
        def fake(convo, lines, **k):
            self.calls += 1
            if self.calls == 2:
                raise judge.JudgeUnavailable("network down")
            return [("OK", 0.1)] * len(lines), 0.01
        judge.judge = fake
        with self.assertRaises(judge.ContentRejected):
            dg.find_rot(self.tp, [f"line number {i} here" for i in range(45)], "Write", files=["/a/plan.md"])
        r = self.logged()[-1]
        self.assertEqual((r["calls"], round(r["cost"], 4), r["lines"]), (1, 0.01, 0))
        self.assertIn("failed", r)


if __name__ == "__main__":
    unittest.main()
