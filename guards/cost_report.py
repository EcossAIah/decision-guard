#!/usr/bin/env python3
"""Where the decision guard's judge money goes, from its log.

Run: python3 guards/cost_report.py [--days N] [--log PATH]
Every paid judge run is one log line with "cost" (see find_rot in decision_guard.py).
"""
import argparse, collections, json, os, time

p = argparse.ArgumentParser()
p.add_argument("--days", type=float, default=7, help="only the last N days (0 = all)")

PLUGIN_STATE = os.path.expanduser("~/.claude/plugins/data/decision-guard-decision-guard")  # plugin install
state = os.environ.get("DECISION_GUARD_STATE") or (PLUGIN_STATE if os.path.isdir(PLUGIN_STATE) else "~/.claude/hooks/.decision-guard")
p.add_argument("--log", default=os.path.join(os.path.expanduser(state), "log.jsonl"))
p.add_argument("--top", type=int, default=10)
a = p.parse_args()

since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - a.days * 86400)) if a.days else ""
runs = []
if not os.path.exists(a.log):
    raise SystemExit(f"no log at {a.log} (nothing judged yet?)")
for line in open(a.log, encoding="utf-8", errors="ignore"):
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if isinstance(r, dict) and isinstance(r.get("cost"), (int, float)) and r.get("t", "") >= since:
        r.setdefault("t", "?")
        runs.append(r)


def kind(r):
    w = r.get("where") or "?"
    if w.startswith("bash"):
        return "shell write"
    if w.startswith("mcp__"):
        return "Claude Docs"
    return w


def files(r):
    if r.get("files"):
        return r["files"]
    w = r.get("where") or ""  # older entries: file names only, inside "where"
    return w[6:].split(", ") if w.startswith("bash: ") else ["(no file recorded)"]


def table(title, groups):
    tot = sum(v[1] for v in groups.values()) or 1
    print(f"\n{title}")
    for k, (n, c, ln) in sorted(groups.items(), key=lambda x: -x[1][1])[:a.top]:
        print(f"  ${c:7.3f}  {100 * c / tot:3.0f}%  {n:5d} runs  {ln:7d} lines  {k}")


total = sum(r["cost"] for r in runs)
print(f"Decision guard spend, {'last %g days' % a.days if a.days else 'all time'}: "
      f"${total:.3f} over {len(runs)} judged runs ({sum(r.get('calls', 0) for r in runs)} logged API calls)")
by = {name: collections.defaultdict(lambda: [0, 0.0, 0]) for name in ("day", "kind", "file", "session")}
for r in runs:
    c, ln = r["cost"], r.get("lines", 0)
    fs = files(r)
    for name, key in (("day", r["t"][:10]), ("kind", kind(r)), ("session", r.get("session") or "?")):
        by[name][key][0] += 1; by[name][key][1] += c; by[name][key][2] += ln
    for f in fs:  # a run's cost is shared evenly among its files
        by["file"][f][0] += 1; by["file"][f][1] += c / len(fs); by["file"][f][2] += ln // len(fs)
table("By day", by["day"])
table("By what triggered it", by["kind"])
table("By file (run cost split evenly across its files)", by["file"])
table("By session", by["session"])
partial = [r for r in runs if r.get("failed") and r["cost"]]
if partial:
    print(f"\nPaid, then stopped partway: {len(partial)} runs, ${sum(r['cost'] for r in partial):.3f}")
big = [r for r in runs if r.get("lines", 0) >= 300]
if big:
    print(f"Big runs (300+ lines): {len(big)} runs, ${sum(r['cost'] for r in big):.3f} "
          "- check these first: bulk text the session may not have written")
