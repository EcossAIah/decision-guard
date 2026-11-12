#!/usr/bin/env python3
"""Question-box answers reach the judge, whatever wording Claude Code uses for them.

Run: python3 tests/test_answers.py
"""
import json, os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("DG_GUARDS") or os.path.join(HERE, "..", "guards"))
import decision_guard as dg  # noqa: E402


def ask(tid, q):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": "AskUserQuestion",
         "input": {"questions": [{"question": q, "header": "Q", "multiSelect": False,
                                  "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}]}]}}]}}


def result(tid, text, error=False):
    b = {"type": "tool_result", "tool_use_id": tid, "content": text}
    if error:
        b["is_error"] = True
    return {"type": "user", "message": {"role": "user", "content": [b]}}


class Answers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dg-ans-")
        self.tp = os.path.join(self.tmp, "s.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def convo(self, recs):
        with open(self.tp, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        return dg.conversation(self.tp)

    def test_the_user_answered_wording(self):  # the wording the guard missed (2026-11-12)
        c = self.convo([ask("a1", "Q4 OG patient numbers: how should counting work?"),
                        result("a1", 'The user answered: "Q4 OG patient numbers: how should counting work?"='
                                     '"OG1, never resets (Recommended)". Read the answers carefully.')])
        self.assertIn("USER (answered a question)", c)
        self.assertIn("OG1, never resets", c)

    def test_your_questions_have_been_answered_wording(self):
        c = self.convo([ask("a1", "Record page layout?"),
                        result("a1", 'Your questions have been answered: "Record page layout?"="Visits in Present".')])
        self.assertIn("Visits in Present", c)

    def test_any_wording_counts_when_it_answers_a_question_box(self):
        c = self.convo([ask("a1", "Lock timeout?"), result("a1", '"Lock timeout?"="15 min, changeable in Settings"')])
        self.assertIn("USER (answered a question)", c)
        self.assertIn("15 min, changeable in Settings", c)

    def test_long_typed_answers_kept_whole(self):
        tail = "LAST-ANSWER: store neither gender nor code"
        text = 'The user answered: "Q5"="' + "show them by trimester. " * 150 + '", "Q7"="' + tail + '"'
        self.assertGreater(len(text), 3000)
        c = self.convo([ask("a1", "Q5"), result("a1", text)])
        self.assertIn(tail, c)

    def test_rejected_question_box_is_not_an_answer(self):
        c = self.convo([ask("a1", "Consent?"),
                        result("a1", "The user doesn't want to proceed with this tool use.", error=True)])
        self.assertNotIn("USER (answered a question)", c)

    def test_other_tool_results_are_not_answers(self):
        c = self.convo([{"type": "assistant", "message": {"role": "assistant", "content": [
                            {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "cat notes.md"}}]}},
                        result("b1", "- Decision: the owner chose Postgres")])
        self.assertNotIn("USER (answered a question)", c)
        self.assertNotIn("Postgres", c)

    def test_never_checked_log_is_capped(self):
        recs = []
        dg.log = lambda r: recs.append(r)
        dg.never_checked("s", [{"path": "/x.md", "line": "x" * 500}] * 100000, "test")
        self.assertEqual(len(recs[0]["never_checked"]), 50)
        self.assertEqual(recs[0]["count"], 100000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
