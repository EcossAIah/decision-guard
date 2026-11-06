#!/usr/bin/env python3
"""Benchmark the installed hook end to end, the way a live Write reaches it.

For each case: write the conversation as a Claude Code transcript (.jsonl), send the hook a
PreToolUse/Write event whose content is the case's lines, and read which lines it denies.
Unlike bench.py (which calls judge.py directly), this exercises the real path: transcript
parsing, line filtering, document labels, batching, and the deny message.

Usage: python3 hook_bench.py [--runs 3] [--hook PATH] [--cases 'cases/*.json']
"""
import argparse, glob, json, os, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))


def transcript(conv, path):
    with open(path, "w") as f:
        for t in conv:
            content = t["text"] if t["role"] == "user" else [{"type": "text", "text": t["text"]}]
            f.write(json.dumps({"type": t["role"], "message": {"role": t["role"], "content": content}}) + "\n")


def run_case(hook, case, tmp):
    tpath = os.path.join(tmp, case["id"] + ".jsonl")
    transcript(case["conversation"], tpath)
    content = "\n".join(l["text"] for l in case["lines"])
    event = {"hook_event_name": "PreToolUse", "tool_name": "Write", "session_id": "bench",
             "transcript_path": tpath, "cwd": tmp,
             "tool_input": {"file_path": os.path.join(tmp, case["id"] + ".md"), "content": content}}
    t0 = time.time()
    r = subprocess.run([sys.executable, hook], input=json.dumps(event), capture_output=True, text=True, timeout=300)
    secs = time.time() - t0
    out = json.loads(r.stdout) if r.stdout.strip() else {}
    if "systemMessage" in out:
        raise RuntimeError(out["systemMessage"])
    reason = (out.get("hookSpecificOutput") or {}).get("permissionDecisionReason", "")
    shown = [ln[4:] for ln in reason.splitlines() if ln.startswith("  - ")]
    blocked = [any(l["text"].strip()[:160].startswith(s.split("  (")[0][:150]) for s in shown) for l in case["lines"]]
    return blocked, secs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--hook", default=os.path.join(HERE, "..", "guards", "decision_guard.py"))
    ap.add_argument("--cases", default="cases/*.json")
    a = ap.parse_args()
    cases = [c for f in sorted(glob.glob(os.path.join(HERE, a.cases))) for c in json.load(open(f))]
    n = sum(len(c["lines"]) for c in cases)
    runs = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(a.runs):
            tp = fp = fn = tn = 0
            misses, secs = [], 0.0
            for c in cases:
                blocked, s = run_case(a.hook, c, tmp)
                secs += s
                for l, b in zip(c["lines"], blocked):
                    rot = l["label"] == "BLOCK"
                    tp += rot and b; fn += rot and not b; fp += (not rot) and b; tn += (not rot) and not b
                    if rot != b:
                        misses.append(f'{c["id"]}: {"missed rot" if rot else "false block"}: {l["text"][:90]}')
            acc = (tp + tn) / n
            runs.append({"run": i + 1, "caught": f"{tp}/{tp+fn}", "false_blocks": f"{fp}/{fp+tn}",
                         "accuracy": round(acc, 3), "seconds": round(secs, 1), "misses": misses})
            print(f"run {i+1}: rot caught {tp}/{tp+fn}, false blocks {fp}/{fp+tn}, accuracy {acc:.0%}, {secs:.0f}s")
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    out = os.path.join(HERE, "results", time.strftime("%Y%m%d-%H%M%S") + "-hook.json")
    json.dump({"cases": len(cases), "lines": n, "hook": os.path.relpath(os.path.abspath(a.hook), os.path.join(HERE, "..")), "runs": runs}, open(out, "w"), indent=1)
    print("saved", out)
    for m in sorted(set(m for r in runs for m in r["misses"])):
        print("  ", sum(m in r["misses"] for r in runs), "/", a.runs, m)


if __name__ == "__main__":
    main()
