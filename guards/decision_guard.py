#!/usr/bin/env python3
"""Decision guard hook: stops agents writing their own proposals into docs as the user's decisions.

No regex word lists: every new doc line is judged by a decision model (guards/judge.py)
against the real conversation (the session transcript).

Hook events (see settings.json):
  PreToolUse  Write|Edit|MultiEdit|NotebookEdit|mcp docs  -> judge new lines, deny if rot
  PreToolUse  Artifact (publish)                           -> strip HTML, judge visible text that is new
                                                              since the last approved publish, deny if rot
  PreToolUse  Bash                                         -> snapshot doc files
  PostToolUse Bash                                         -> diff doc files vs snapshot, judge
                                                              new lines (catches python/perl/sed/...)
  PostToolUseFailure Bash                                  -> same as PostToolUse (failed commands can write too)
  Shell writes are traced: each new line is judged only if its text comes from this session (see "tracing").
  What a session typed is read from its own Claude Code log (and its helper agents' logs); no copy is kept,
  only fingerprints (hashes) for fast lookup.
Fails open (with a visible warning) if the model can't be reached.
"""
import fcntl, hashlib, json, os, re, shutil, sys, time
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import judge  # noqa: E402

DOC_EXT = (".md", ".mdx", ".txt", ".rst", ".org")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".cache",
             "anaconda3", "miniconda3", "site-packages", "Library", "target", "vendor"}
SCAN_SECONDS = 1.5         # never slow down a shell command more than this
STATE = os.path.expanduser(os.environ.get("DECISION_GUARD_STATE") or os.environ.get("CLAUDE_PLUGIN_DATA")
                           or "~/.claude/hooks/.decision-guard")  # plugin data survives plugin updates
MAX_FILES = 5000
PRUNE_DAYS = 2          # snapshot folders of sessions idle this long are deleted
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")


def is_cache(path):
    """Claude Code's own storage under ~/.claude/projects (saved web pages, tool output, subagent logs).
    Only Claude Code's auto-memory folders (memory/) there hold real docs."""
    rel = os.path.relpath(os.path.abspath(path), CLAUDE_PROJECTS)
    if rel.startswith(".."):
        return False
    parts = rel.split(os.sep)
    return len(parts) >= 2 and parts[1] != "memory" and (len(parts) > 2 or os.path.isdir(path))
CONTEXT_CHARS = 40000      # ~10K tokens of conversation
LINES_PER_CALL = 30        # 4 questions per line; API max is 128 questions
MAX_LINES = 600           # more pieces than this in one write: denied ("write it in smaller pieces"), never skipped
LINE_CHARS = 1000         # longer lines are split into pieces of about this size; every piece is judged
HEAD_CHARS = 150          # later pieces of a long line carry its opening words (where "Proposal:" labels sit)
JUDGE_SECONDS = 70        # stop before Claude Code's 90-second hook timeout kills the hook silently
HOOK_SECONDS = 82         # the whole hook (tracing + judging) ends by then
HOOK_T0 = time.time()     # when this hook run started
PENDING_HOURS = 24        # lines that could not be judged are retried at the next shell command this long...
PENDING_TRIES = 3         # ...or this many failed tries, then reported as never checked
MODEL = (os.environ.get("DECISION_GUARD_MODEL") or os.environ.get("CLAUDE_PLUGIN_OPTION_DECISION_MODEL")
         or judge.DEFAULT_MODEL)


# ---------- conversation from the transcript ----------

def _text(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text", ""))
    return "\n".join(out)


def _strip_reminders(t):
    while "<system-reminder>" in t and "</system-reminder>" in t:
        a = t.index("<system-reminder>")
        b = t.index("</system-reminder>", a) + len("</system-reminder>")
        t = t[:a] + t[b:]
    return t.strip()


def conversation(transcript_path):
    """USER/ASSISTANT turns, newest kept, within CONTEXT_CHARS. Question-dialog answers count as USER."""
    turns = []
    try:
        for raw in open(transcript_path, encoding="utf-8", errors="ignore"):
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if e.get("isMeta") or e.get("isSidechain"):
                continue
            msg = e.get("message") or {}
            role, content = msg.get("role"), msg.get("content")
            if e.get("type") == "user" and role == "user":
                if isinstance(content, list):
                    for b in content:  # answers to AskUserQuestion arrive as tool results
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            r = _text(b.get("content")) if not isinstance(b.get("content"), str) else b["content"]
                            if "answered" in r and "question" in r.lower():
                                turns.append(("USER (answered a question)", r[:2000]))
                t = _strip_reminders(_text(content))
                if t and not t.startswith("<local-command") and not t.startswith("<command-"):
                    turns.append(("USER", t[:3000]))
            elif e.get("type") == "assistant":
                t = _text(content).strip()
                if t:
                    turns.append(("ASSISTANT", t[:1500]))
    except OSError:
        return ""
    return pack(turns)


PAIR_CHARS = 600  # the agent message just before a user message, shortened: what a bare "Yes." answered


def pack(turns, budget=CONTEXT_CHARS):
    """Like _pack, but never over budget: gap markers add up when many short messages are kept."""
    limit = budget
    for _ in range(4):
        out = _pack(turns, limit)
        if len(out) <= budget:
            return out
        limit -= len(out) - budget + 500
    return out[-budget:]


def _pack(turns, budget):
    """Fit the conversation into the budget, most decision-relevant first:
    1. every USER message (newest first) - short, and where decisions live;
    2. a shortened copy of the agent message each one answered (what a bare "Yes." referred to);
    3. the most recent messages in full.
    Output keeps the original order and marks left-out stretches, so the judge knows there is a gap."""
    budget -= 1000  # room for gap markers
    keep, used = {}, 0  # index -> text to show

    def take(i, text):
        nonlocal used
        old = keep.get(i)
        c = len(text) - (len(old) if old is not None else -(len(turns[i][0]) + 3))
        if used + c > budget:
            return False
        keep[i], used = text, used + c
        return True

    users = [i for i in reversed(range(len(turns))) if turns[i][0].startswith("USER")]
    for i in users:
        if not take(i, turns[i][1]):
            break
    for i in users:
        j = i - 1
        if i in keep and j >= 0 and turns[j][0] == "ASSISTANT" and j not in keep:
            t = turns[j][1]
            if not take(j, t[:PAIR_CHARS] + (" [...]" if len(t) > PAIR_CHARS else "")):
                break
    for i in reversed(range(len(turns))):  # recent context, in full
        if keep.get(i) != turns[i][1] and not take(i, turns[i][1]):
            break
    out, gap = [], 0
    for i, (role, t) in enumerate(turns):
        if i in keep:
            if gap:
                out.append(f"[... {gap} message{'s' if gap > 1 else ''} left out ...]")
                gap = 0
            out.append(f"{role}: {keep[i]}")
        else:
            gap += 1
    if gap:
        out.append(f"[... {gap} message{'s' if gap > 1 else ''} left out ...]")
    return "\n".join(out)


# ---------- which lines are new ----------

def new_lines(text, old=""):
    old_set = {l.strip() for l in old.splitlines()}
    out = []
    for l in text.splitlines():
        s = l.strip()
        if len(s) < 4 or s in old_set or s.startswith("```") or set(s) <= set("|-: "):
            continue
        out.append(s)
    return out


STATUS_RE = re.compile(r"^[\s>*_#-]*status[\s*_]*:[\s*_]*(.+?)[\s*_]*$", re.I)
PROPOSAL_WORDS = ("proposal", "proposed", "draft", "not approved", "not decided", "unapproved")


def doc_status(text):
    """A whole-document label near the top, e.g. 'Status: Agent proposal - not approved by the user'.
    Only labels that say the doc is NOT decided count; 'Status: approved' gives no context."""
    for l in text.splitlines()[:15]:
        m = STATUS_RE.match(l)
        if m and any(w in m.group(1).lower() for w in PROPOSAL_WORDS):
            return m.group(1).strip()[:120]
    return None


def with_status(lines, text):
    st = doc_status(text)
    return [f'{l}  (document status: "{st}")' for l in lines] if st else lines


# Claude Docs tools that only look things up: nothing new is written, so nothing to judge.
# A denylist, not an allowlist: a new or renamed tool that writes is still judged.
DOCS_READ_ONLY = ("guide", "read", "query", "export", "delete")


def notebook_cell_type(ti):
    """'code' or 'markdown' for the cell this NotebookEdit writes; None if unknown (then it is judged)."""
    if ti.get("cell_type"):
        return ti["cell_type"]
    if ti.get("edit_mode", "replace") != "replace" or ti.get("cell_id") is None:
        return None  # insert: cell_id is the neighbour, not this cell; no id: can't tell
    try:  # replace without a type keeps the cell's current type
        nb = json.load(open(ti.get("notebook_path", ""), encoding="utf-8"))
        for i, c in enumerate(nb.get("cells") or []):
            if ti.get("cell_id") in (c.get("id"), str(i), f"cell-{i}"):
                return c.get("cell_type")
    except (OSError, ValueError, AttributeError):
        pass
    return None


def in_temp(path):
    """Write/Edit target inside Claude's cache area, both as named and after following links:
    a link either way (doc -> cache, cache -> doc) is judged. Temp folders aren't exempt: text could escape from them."""
    return bool(path) and is_cache(os.path.abspath(path)) and is_cache(os.path.realpath(path))


def lines_for_tool(tool, ti):
    if tool in ("Write", "Edit", "MultiEdit"):
        path = ti.get("file_path", "")
        if not path.lower().endswith(DOC_EXT) or in_temp(path):
            return path, []  # code files, and Claude's own cache folders (shell writes skip them too)
        disk = open(path, encoding="utf-8", errors="ignore").read() if os.path.isfile(path) else ""
        if tool == "Write":
            return path, with_status(new_lines(ti.get("content", ""), disk), ti.get("content", ""))
        out, after = [], disk
        for e in ti.get("edits") or [ti]:
            out += new_lines(e.get("new_string", ""), e.get("old_string", ""))
            after = after.replace(e.get("old_string", ""), e.get("new_string", ""), 1)
        return path, with_status(out, after)  # status as it will be after the edit
    if tool == "NotebookEdit":
        path = ti.get("notebook_path", "")
        if ti.get("edit_mode") == "delete" or in_temp(path) or notebook_cell_type(ti) == "code":
            return path, []  # code cells hold code, not decisions; only text (markdown) cells are judged
        return path, new_lines(ti.get("new_source", ""))
    if tool.startswith("mcp__") and tool.rsplit("__", 1)[-1] in DOCS_READ_ONLY:
        return tool, []
    # MCP doc tools: every string in the payload
    strings = []
    def walk(v):
        if isinstance(v, str):
            strings.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(ti)
    text = "\n".join(strings)
    return tool, with_status(new_lines(text), text)


# ---------- Artifact: HTML pages published to claude.ai ----------

HTML_EXT = (".html", ".htm")
SKIP_TAGS = {"script", "style", "noscript", "template"}
BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "li", "ul", "ol",
              "tr", "td", "th", "table", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr", "blockquote", "pre",
              "figcaption", "summary", "details", "dt", "dd", "title", "caption", "label", "button", "option"}


HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6", "legend", "summary"}
ITEMS = {"li", "td", "option", "button", "dd"}  # list entries never act as labels for what follows


class _Text(HTMLParser):
    """Visible text only (no tags, scripts, styles), one line per block, each with its page context:
    the nearest heading and the nearest short label under it (e.g. "Options" above a list)."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines, self.buf, self.skip, self.tag = [], [], 0, None
        self.heading = self.label = None

    def flush(self):
        t = " ".join("".join(self.buf).split())
        self.buf = []
        if not t:
            return
        self.lines.append((t, self.heading, self.label))
        if self.tag in HEADINGS:
            self.heading, self.label = t, None
        elif self.tag not in ITEMS and len(t.split()) <= 3 and not t.endswith((".", "!", "?")):
            self.label = t

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag in BLOCK_TAGS or tag in HEADINGS:
            self.flush()
            self.tag = tag

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK_TAGS or tag in HEADINGS:
            self.flush()

    def handle_data(self, d):
        if not self.skip:
            self.buf.append(d)


def html_lines(html):
    """[(line, line with its page context for the judge)]."""
    p = _Text()
    p.feed(html)
    p.close()
    p.flush()
    out = []
    for t, h, lab in p.lines:
        ctx = " > ".join(f'"{x}"' for x in (h, lab) if x)
        out.append((t, f"{t}  (page context: under {ctx})" if ctx else t))
    return out


def html_text(html):
    return "\n".join(t for t, _ in html_lines(html))


def artifact_pages(ti, cwd):
    """Local HTML files a publish sends: the page itself plus any HTML supporting files."""
    if ti.get("action", "publish") != "publish" or ti.get("asset"):
        return []  # read/list/open/delete/asset uploads publish no page text
    paths = [ti.get("file_path")] if ti.get("file_path") else []
    files = ti.get("files") or {}
    srcs = files.values() if isinstance(files, dict) else [f.get("path") for f in files if isinstance(f, dict)]
    for v in srcs:
        v = v.get("from") if isinstance(v, dict) else v
        if isinstance(v, str):
            paths.append(os.path.join(ti.get("root") or "", v) if ti.get("root") and not os.path.isabs(v) else v)
    out = []
    for p in paths:
        p = os.path.normpath(os.path.join(cwd, os.path.expanduser(p)))
        if p.lower().endswith(HTML_EXT) and os.path.isfile(p):
            out.append(p)
    return list(dict.fromkeys(out))


def artifact_base(p):
    d = os.path.join(STATE, "artifacts")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, hashlib.sha1(p.encode()).hexdigest() + ".txt")


def artifact_pre(data):
    """Judge only lines new since this page's last approved publish; remember the text once it passes."""
    pages = artifact_pages(data.get("tool_input") or {}, data.get("cwd") or os.getcwd())
    texts, lines = {}, []
    for p in pages:
        pairs = html_lines(open(p, encoding="utf-8", errors="ignore").read())
        texts[p] = "\n".join(t for t, _ in pairs)  # baseline compares plain text
        with_ctx = {}
        for t, c in pairs:
            with_ctx.setdefault(t, c)
        b = artifact_base(p)
        old = open(b, encoding="utf-8").read() if os.path.exists(b) else ""
        st = doc_status(texts[p])
        lines += [with_ctx.get(ln, ln) + (f'  (document status: "{st}")' if st else "")
                  for ln in new_lines(texts[p], old)]  # judge sees the context
    rot, _ = find_rot(data.get("transcript_path", ""), lines, "artifact", files=pages)
    if rot:
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": explain("artifact " + ", ".join(os.path.basename(p) for p in pages), rot)
            + "\nFix those lines in the HTML file, then publish again."}}
    for p, t in texts.items():  # passed: new baseline, so republishes only judge what changed
        with open(artifact_base(p), "w", encoding="utf-8") as f:
            f.write(t)
    return None


# ---------- judging ----------

def pieces(ln):
    """A line longer than LINE_CHARS is split into pieces, at sentence ends where possible, so every
    character is judged. Each piece keeps the document label, and later pieces keep the line's opening
    words, where labels like 'Proposal:' usually sit."""
    m = STATUS_SUFFIX.search(ln)
    body, suffix = (ln[:m.start()], m.group(0)) if m else (ln, "")
    if len(body) <= LINE_CHARS:
        return [ln]
    head = body[:HEAD_CHARS].rsplit(" ", 1)[0]
    size = LINE_CHARS - len(head) - 7
    parts, cur = [], ""
    for s in re.split(r"(?<=[.!?;])\s+", body):
        while len(s) > size:  # one huge sentence: hard cuts that overlap, so no phrase falls in a gap
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(s[:size])
            s = s[size - 100:]
        if cur and len(cur) + 1 + len(s) > size:
            parts.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        parts.append(cur)
    return [(p if p.startswith(head) else head + " [...] " + p) + suffix for p in parts]


def find_rot(transcript_path, lines, where="", leftover=None, files=None):
    """Judge lines; a line is rot if any of its pieces is judged BLOCK. Returns (rot, cost).
    leftover=None (writes not made yet): more than MAX_LINES pieces is refused; running out of time fails open.
    leftover=a list (shell writes, already on disk): lines past the limit or the deadline are added to it unjudged.
    files: the doc paths these lines come from (cost log only)."""
    lines = list(dict.fromkeys(lines))
    if not lines:
        return [], 0.0
    # scrub secrets from the whole line BEFORE splitting: a cut through a key would leave unscrubbable fragments
    items = [(i, pc) for i, ln in enumerate(lines) for pc in pieces(judge.redact(ln))]
    done = len(lines)  # lines before this index get fully judged
    if len(items) > MAX_LINES:
        done = items[MAX_LINES][0]  # first line that does not fit whole
        if leftover is None or done == 0:
            raise judge.ContentRejected(f"{len(items)} pieces to judge (a long line counts once per "
                                        f"{LINE_CHARS:,} characters); the limit is {MAX_LINES}. "
                                        "Write the document in smaller pieces.")
        items = [x for x in items if x[0] < done]
    convo = conversation(transcript_path)
    if not convo:  # no conversation = no evidence either way; don't guess
        raise RuntimeError("transcript not readable, cannot judge")
    start, flagged, cost, calls, failed = time.time(), {}, 0.0, 0, None
    try:
        for b in range(0, len(items), LINES_PER_CALL):
            left = min(JUDGE_SECONDS - (time.time() - start), HOOK_T0 + HOOK_SECONDS - time.time())
            if left < 5:
                if leftover is None:  # the judge works (earlier calls answered): the write is too big to check in time
                    raise judge.ContentRejected(f"only part of this write could be judged in {JUDGE_SECONDS}s; "
                                                "write it in smaller pieces")
                done = items[b][0]  # this line and later ones are not (fully) judged
                break
            batch = items[b:b + LINES_PER_CALL]
            try:
                res, c = judge.judge(convo, [pc for _, pc in batch], model=MODEL, batched=True, timeout=left)
            except judge.JudgeUnavailable:
                if b and leftover is None:  # earlier calls answered, so size, not an outage, ran out the clock
                    raise judge.ContentRejected("the judge stopped answering partway through this write; "
                                                "write it in smaller pieces")
                raise
            cost, calls = cost + c, calls + 1
            for (i, _), (v, p) in zip(batch, res):
                if v == "BLOCK":
                    flagged[i] = max(flagged.get(i, 0), p)
    except BaseException as e:  # incl. the hook being killed: the log must not claim lines were judged
        failed = f"{type(e).__name__}: {e}"[:200]
        raise
    finally:  # every paid call reaches the log, even when judging stops partway
        judged = done if failed is None else 0
        rot = [(lines[i], p) for i, p in sorted(flagged.items()) if i < judged]
        rec = {"session": os.path.basename(transcript_path)[:8], "where": where, "lines": judged,
               "pieces": len(items), "rot": len(rot), "cost": cost, "calls": calls,
               "secs": round(time.time() - start, 1),
               "files": list(dict.fromkeys(files or []))[:10],
               "flagged": [ln[:240] for ln, _ in rot],
               **({"unjudged": len(lines) - judged} if judged < len(lines) else {}),
               **({"failed": failed} if failed else {})}
        if failed is None or calls:  # nothing paid and nothing judged: the error log line covers it
            log(rec)
    if leftover is not None:
        leftover += lines[done:]
    return rot, cost


def explain(where, rot):
    shown = "\n".join(f"  - {ln[:160]}" for ln, _ in rot[:12])
    more = f"\n  (+{len(rot) - 12} more)" if len(rot) > 12 else ""
    return (f"DECISION GUARD ({where}): these lines read as decided/settled, but the user did not "
            f"decide them in this conversation:\n{shown}{more}\n\n"
            "Only the user makes decisions. For each line, either:\n"
            "1. rewrite it honestly — 'Proposal:', 'Open:', 'Option:', or 'Agent recommendation (not decided):', or\n"
            "2. ask the user about that specific item (AskUserQuestion, one item per question — never 'approve all'). "
            "Once the user's answer is in the conversation, retry the write.\n"
            "If the WHOLE document is your own proposal (a plan, spec or draft), put this line near the top instead: "
            "'Status: Agent proposal - not approved by the user'. Lines that say the user decided something are still checked.\n"
            "Do not reword the line to dodge this check.")


def log(rec):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(os.path.join(STATE, "log.jsonl"), "a") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")
    except OSError:
        pass


# ---------- Bash: snapshot before, diff after ----------

def git_root(path):
    d = os.path.abspath(path)
    while d != "/":
        if os.path.exists(os.path.join(d, ".git")):
            return d
        d = os.path.dirname(d)
    return None


def cd_dirs(command, cwd):
    """Folders the command cd's into (e.g. `cd notes && python edit.py`)."""
    out, toks = [], re.sub(r"&&|\|\||[;|()]", " ", command).split()
    for i, tok in enumerate(toks[:-1]):
        if tok == "cd":
            p = os.path.normpath(os.path.join(cwd, os.path.expanduser(toks[i + 1].strip("'\""))))
            if os.path.isdir(p):
                out.append(p)
    return out


def named_docs(command, dirs):
    """Doc files mentioned by name in the command, resolved against each candidate folder."""
    out = []
    for tok in command.replace("'", " ").replace('"', " ").replace("=", " ").replace("(", " ").replace(")", " ").split():
        if tok.lower().endswith(DOC_EXT):
            for d in dirs:
                p = os.path.normpath(os.path.join(d, os.path.expanduser(tok)))
                if os.path.isfile(p) and not is_cache(p):
                    out.append(p)
    return out


def roots_for(cwd, command):
    return list(dict.fromkeys([cwd] + cd_dirs(command, cwd)))


def scan(roots, command=""):
    """Doc files to watch under each root (its git repo if any, else 2 levels deep), plus files named in the command."""
    found, t0 = {}, time.time()
    for start in roots:
        root = git_root(start)
        max_depth = 8 if root else 2
        root = root or start
        base_depth = root.rstrip("/").count("/")
        for d, dirs, files in os.walk(root):
            if time.time() - t0 > SCAN_SECONDS or len(found) >= MAX_FILES:
                break
            if d.count("/") - base_depth >= max_depth:
                dirs[:] = []
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS and (not x.startswith(".") or x == ".claude")
                       and not is_cache(os.path.join(d, x))]
            for f in files:
                if f.lower().endswith(DOC_EXT) and not is_cache(os.path.join(d, f)):
                    p = os.path.join(d, f)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    found[p] = [st.st_mtime, st.st_size]
    for p in named_docs(command, roots):
        try:
            st = os.stat(p)
        except OSError:
            continue
        found[p] = [st.st_mtime, st.st_size]
    return found


# State layout (v2):
#   sessions/<session>/index.json   path -> [mtime, size, content hash]  (the session's "before" copy list)
#   sessions/<session>/inflight.json  tool_use_id -> {t, roots}  (commands still running; ~50 bytes each)
#   sessions/<session>/own_index.db   fingerprints (hashes, not text) of what this session typed, read from its log
#   blobs/<ab>/<hash>.gz            file contents, gzip, stored once for all sessions
INFLIGHT_MAX_AGE = 15 * 60   # a command whose end never fired (cancelled) stops counting as running
FINISHED_CHECK_AGE = 10      # older running entries are checked against the transcript for a result
WORDS_ENTRY_CHARS = 300000   # cap per tool call
STATUS_SUFFIX = re.compile(r'\s+\(document status: ".*"\)$')
UESC = re.compile(r"\\u([0-9a-fA-F]{4})")


# ---------- "whose words": what each session typed into its tools, read from its own log ----------
#
# Claude Code writes every tool call into the session's log (transcript) before the tool runs, and gives the
# hook that log's path. So the guard keeps no copy of anyone's typing: it reads the logs. Another session's
# log is found among Claude Code's session logs; a session's helper agents (subagents) count as the session.

def norm(s):
    return " ".join(s.split())


def norm_lines(s):
    """Whitespace collapsed inside each line; line breaks kept, so each typed line can be found again."""
    return "\n".join(" ".join(l.split()) for l in s.splitlines() if l.strip())


def word_variants(s):
    """Raw text plus a copy with common script escapes undone (\\n, \\", \\uXXXX), whitespace collapsed."""
    un = UESC.sub(lambda m: chr(int(m.group(1), 16)), s)
    un = un.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
    return [norm_lines(s), norm_lines(un)] if un != s else [norm_lines(s)]


def tool_words(ti):
    out = []
    def walk(v):
        if isinstance(v, str):
            out.extend(word_variants(v))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(ti)
    return "\n".join(out)[:WORDS_ENTRY_CHARS]


def _iso(ts):
    import datetime
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


_HOOK_DENIED = re.compile(r"^(?:Error: )?PreToolUse:\S+ hook error:")


def log_events(f, start=0):
    """Tool calls in a Claude Code session log from byte `start`: (('call', time, id, text, read_only) or
    ('cancel', id), end offset). A call the user or a hook denied is cancelled: it never ran."""
    for e, end in _iter_recs(f, start):
        if not e:
            continue
        kind = e.get("type")
        content = (e.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        if kind == "assistant":
            t = _iso(e.get("timestamp")) or time.time()
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    ti = b.get("input") or {}
                    ro = b.get("name") == "Bash" and is_read_only(ti.get("command", "") if isinstance(ti, dict) else "")
                    yield ("call", t, b.get("id", "?"), tool_words(ti), ro), end
        elif kind == "user":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and (
                        e.get("toolDenialKind") or (b.get("is_error") and _HOOK_DENIED.match(_text(b.get("content"))))):
                    yield ("cancel", b.get("tool_use_id")), end


# For tools that give the hook no session log (see README, "Other coding tools"), the guard keeps one itself:
# sessions/<session>/saved-calls.jsonl, in Claude Code's log format, so the same code reads it. Each tool call
# is saved when its before-hook runs, each user message when the prompt hook runs. Claude Code never needs it.
SAVED_CALLS = "saved-calls.jsonl"


def has_log(tp):
    return bool(tp) and os.path.isfile(tp)


def use_saved_log(data):
    """No session log from the tool: save this event into the guard's own log and point the hook at it."""
    if data.get("transcript_path") or not data.get("session_id"):
        return  # a path was given (Claude Code): that log is the record, even if not written yet
    path = os.path.join(session_dir(data["session_id"]), SAVED_CALLS)
    data["transcript_path"] = path
    event = data.get("hook_event_name")
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
    if event == "UserPromptSubmit" and isinstance(data.get("prompt"), str):
        rec = {"type": "user", "timestamp": now, "message": {"role": "user", "content": data["prompt"]}}
    elif event == "PreToolUse":
        rec = {"type": "assistant", "timestamp": now, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": data.get("tool_use_id") or "?", "name": data.get("tool_name", ""),
             "input": data.get("tool_input") or {}}]}}
    else:
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def saved_cancel(data):
    """This guard denied a call in a tool with no log: mark it cancelled in the saved log, it never ran."""
    tp = data.get("transcript_path") or ""
    if not tp.endswith(SAVED_CALLS):
        return
    rec = {"type": "user", "toolDenialKind": "hook", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": data.get("tool_use_id") or "?", "is_error": True}]}}
    try:
        with open(tp, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def session_logs(transcript_path):
    """A session's own log plus its helper agents' logs."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return []
    out = [transcript_path]
    sub = os.path.join(transcript_path[:-6], "subagents") if transcript_path.endswith(".jsonl") else ""
    if sub and os.path.isdir(sub):
        out += sorted(os.path.join(sub, f) for f in os.listdir(sub) if f.endswith(".jsonl"))
    return out


def other_logs(own, since, born_before=None):
    """{session: [log files]} for other sessions whose logs changed since `since`. With born_before, a log made
    after that time counts only if that session runs this guard (it has guard state): a session this command
    started with hooks off, or a log this command wrote, is not "another session's guard judges it"."""
    out = _other_logs(own, since)
    if born_before is None:
        return out
    guarded = set()
    try:
        guarded = set(os.listdir(os.path.join(STATE, "sessions")))
    except OSError:
        pass
    keep = {}
    for name, files in out.items():
        # a guard folder made during the command is trusted too: a session opened while this command ran is
        # common. Forging one is possible either way (in an earlier command), see README "Deliberate forgery".
        ok = [f for f in files if _born(f) < born_before or name in guarded]
        if ok:
            keep[name] = ok
    return keep


def _born(f):
    try:
        st = os.stat(f)
    except OSError:
        return float("inf")
    return getattr(st, "st_birthtime", None) or st.st_ctime


def _other_logs(own, since):
    out, own = {}, set(own)
    try:
        projects = os.listdir(CLAUDE_PROJECTS)
    except OSError:
        return out
    for proj in projects:
        pd = os.path.join(CLAUDE_PROJECTS, proj)
        try:
            entries = list(os.scandir(pd))
        except OSError:
            continue
        for e in entries:
            try:
                if e.name.endswith(".jsonl") and e.is_file() and e.stat().st_mtime >= since and e.path not in own:
                    out.setdefault(e.name[:-6], []).append(e.path)
                elif e.is_dir():
                    sub = os.path.join(e.path, "subagents")
                    if os.path.isdir(sub) and os.stat(sub).st_mtime >= since - 86400:
                        for s in os.scandir(sub):
                            if s.name.endswith(".jsonl") and s.stat().st_mtime >= since and s.path not in own:
                                out.setdefault(e.name, []).append(s.path)
            except OSError:
                continue
    try:  # sessions of other tools, whose calls the guard saved itself
        for name in os.listdir(os.path.join(STATE, "sessions")):
            f = os.path.join(STATE, "sessions", name, SAVED_CALLS)
            if f not in own and os.path.isfile(f) and os.path.getmtime(f) >= since:  # birth checked by caller
                out.setdefault(name, []).append(f)
    except OSError:
        pass
    return out


# ---------- tracing: where did each new line's text come from? ----------
#
# A file changing while this session's command ran does not mean this session wrote it: git brings in
# committed text, downloads bring in outside text, other programs write at the same time. So each new line
# is traced, and only text that comes from this session is judged here:
#   own   this session typed it (in a tool call that writes, not a search) before any other source had it,
#         or this session's own code edited a file it names -> judged, "fix them now"
#   code  new text no source has, written while this session's own code ran (or by a tool it fed its own
#         words to) -> judged, but the agent is told not to edit what it did not write
#   skip  the text existed before the command (git history that existed before it, another doc with the
#         same document label, another session's typing), or nothing this session wrote could have written it

TRACE_SECONDS = 4           # time for git and stored-copy lookups after a shell command
OTHER_MIN_CHARS = 16        # a line shorter than this is never credited to another session
TRACE_TOTAL = 25            # all tracing stops by then (hook limit is 90 s); lines left are judged, never skipped
GIT_MAX_REFS = 20000        # refs remembered before a command
GIT_MAX_CANDS = 64          # commits searched for a file's earlier text
FIRST_SLACK = 2             # seconds: git dates are whole seconds, so "earlier" must be earlier by more than this
TAIL_BYTES = 4 << 20        # recent tool calls are read from the end of a words file

_LEAD = re.compile(r"^(?:[#>]+\s*|[*+-]\s+|\d+[.)]\s+)")
_EMPH_OPEN = re.compile(r"(?<!\w)(?:\*\*|__|\*|_)(?=\S)")
_EMPH_CLOSE = re.compile(r"(?<=\S)(?:\*\*|__|\*|_)(?!\w)")
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'')
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2(?=\s|$)", re.S)
_REDIRECT = re.compile(r"(?:\d?>>?|\btee\s+(?:-a\s+)?)\s*['\"]?([^\s'\";|&()<>]+)")
_SEP = re.compile(r"\|\||&&|[;\n()]|\|")


class _Sep:
    def __init__(self, a, b, t):
        self._a, self._b, self._t = a, b, t

    def start(self):
        return self._a

    def end(self):
        return self._b

    def group(self, _=0):
        return self._t


def _seps(body):
    """Command separators outside quotes: `sed -E 's/(a|b)/x/'` is one command, not three."""
    out, i, q, n = [], 0, None, len(body)
    while i < n:
        c = body[i]
        if q:
            if c == "\\" and q == '"':
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            q = c
            i += 1
            continue
        m = _SEP.match(body, i)
        if m:
            out.append(_Sep(m.start(), m.end(), m.group(0)))
            i = m.end()
            continue
        i += 1
    return out
_ENV = re.compile(r"^\w+=\S*$")

INTERP = re.compile(r"^(?:python[\d.]*|pypy3?|node|nodejs|ruby|perl|php|bun|deno|osascript|Rscript|lua|bash|sh|zsh|dash|fish)$")
OUTSIDE_TOOLS = {"curl", "wget", "gh", "glab", "http", "https", "xh", "aws", "gcloud", "az", "kubectl", "docker",
                 "ssh", "nc", "lynx", "w3m", "pandoc", "pbpaste"}
DECODERS = {"base64", "basenc", "base32", "xxd", "od", "uudecode", "openssl", "gunzip", "gzip", "zcat", "bzip2", "bunzip2",
            "xz", "unxz", "zstd", "iconv", "rev", "tac", "recode", "gpg", "age", "rot13", "uniconv"}
TEXT_TOOLS = {"sed", "gsed", "awk", "gawk", "mawk", "nawk", "perl", "jq", "yq", "tr", "echo", "printf", "cat", "tee",
              "cut", "paste", "sort", "uniq", "column", "fmt", "fold", "envsubst", "m4"}
AI_CLIS = {"codex", "claude", "gemini", "aider", "llm", "ollama", "opencode", "cursor-agent", "goose", "amp", "crush",
           "qwen", "kimi", "droid", "copilot", "cline", "plandex", "mentat", "q", "chatgpt", "sgpt", "mods", "fabric"}
WRAPPERS = {"sudo", "env", "time", "command", "exec", "nohup", "nice", "timeout", "caffeinate", "xargs", "uv", "uvx",
            "poetry", "pipenv", "npx", "pnpx", "bunx", "conda", "mamba", "pixi"}
WRAPPER_SUB = {"run", "exec"}
RO_PROGS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "cat", "less", "more", "head", "tail", "wc", "find", "ls",
            "file", "stat", "du", "diff", "cmp", "true", "false", "pwd", "which", "type", "echo", "printf", "test",
            "[", "sleep", "date", "tree", "fd", "bat", "jq", "sort", "uniq", "cut", "awk", "sed"}
RO_GIT = {"log", "show", "diff", "grep", "status", "blame", "rev-parse", "ls-files", "ls-tree", "cat-file",
          "describe", "shortlog", "reflog", "branch", "tag", "remote", "config"}


def fkey(s):
    """A line with formatting removed (list and heading marks, bold/italic, table padding).
    Two lines with the same key say the same words."""
    s = STATUS_SUFFIX.sub("", s).strip()
    prev = None
    while s != prev:
        prev, s = s, _LEAD.sub("", s, count=1)
    s = _EMPH_CLOSE.sub("", _EMPH_OPEN.sub("", s))
    return " ".join(re.sub(r"\s*\|\s*", "|", s).split())


def paragraphs(text):
    """[(key, lines)] for each run of non-blank lines; the key ignores formatting and line wrapping."""
    out, cur = [], []
    for l in text.splitlines() + [""]:
        if l.strip():
            cur.append(l)
        elif cur:
            out.append((" ".join(fkey(x) for x in cur), cur))
            cur = []
    return out


def new_lines_shell(new, old):
    """New lines after a shell command: a line is new only if its WORDS are new. Reformatting (bullet style,
    bold, heading level, table padding) and re-wrapping a paragraph don't count; any changed word does."""
    old_keys = {fkey(l) for l in old.splitlines()}
    old_paras = {k for k, _ in paragraphs(old)}
    out = []
    for k, ls in paragraphs(new):
        if k in old_paras:
            continue
        for l in ls:
            s = l.strip()
            if new_lines(s) and fkey(s) not in old_keys:  # new_lines(s): the usual filters (short, ```, |--|)
                out.append(s)
    return out


def line_keys(ln):
    body = STATUS_SUFFIX.sub("", ln)
    return [k for k in dict.fromkeys([norm(body), fkey(body)]) if k]


# ---- reading a shell command ----

def segments(cmd):
    """[(words, piped_in)] for each simple command in a shell line. Heredoc bodies are cut out (they are text,
    not commands); `piped_in` = the segment reads another command's output."""
    body = _HEREDOC.sub("<<HEREDOC", cmd)
    out, pos = [], 0
    piped = False
    for m in _seps(body) + [None]:
        part = body[pos:m.start()] if m else body[pos:]
        import shlex
        try:
            toks = shlex.split(part, comments=False)
        except ValueError:
            toks = part.replace("'", " ").replace('"', " ").split()
        if toks:
            out.append((toks, piped, part))
        piped = bool(m) and m.group(0) == "|"
        pos = m.end() if m else len(body)
    return out


def program(toks):
    """(program, args) after env assignments and wrappers like `sudo`, `uv run`, `npx`."""
    i = 0
    while i < len(toks):
        t = toks[i]
        if _ENV.match(t) or t in ("!", "{", "}"):
            i += 1
        elif os.path.basename(t) in WRAPPERS:
            i += 1
            while i < len(toks) and (toks[i].startswith("-") or toks[i] in WRAPPER_SUB or _ENV.match(toks[i])):
                i += 1
        else:
            return os.path.basename(t), toks[i + 1:], t
    return "", [], ""


_INPLACE = re.compile(r"^(?:-[A-Za-z0-9]*i|--in-place)")          # sed -i, -Ei, -i.bak, --in-place; perl -pi
SED_LONG = {"expression": "arg", "file": "arg", "line-length": "arg", "in-place": "opt", "quiet": "", "silent": "",
            "regexp-extended": "", "separate": "", "unbuffered": "", "null-data": "", "zero-terminated": "",
            "posix": "", "sandbox": "", "debug": "", "follow-symlinks": "", "binary": "", "help": "", "version": ""}


def sed_options(args):
    """Read sed's arguments both ways seds do: GNU (options anywhere) and BSD (options stop at the first plain
    word). (scripts, unsafe, in_place, files). Every word either reading could take as a script is in scripts.
    unsafe: a script file, or an option it can't read (fails closed)."""
    out, unsafe, in_place, plain, i = [], False, False, [], 0
    explicit, opt_after_plain, bare_i = False, False, False
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            plain += args[i:]
            break
        is_opt = a.startswith("-") and len(a) > 1
        if is_opt and plain:
            opt_after_plain = True  # BSD stops reading options here: the first plain word is its script
        if a.startswith("--"):
            name, eq, val = a[2:].partition("=")
            hits = [k for k in SED_LONG if k == name] or [k for k in SED_LONG if k.startswith(name)]
            if len(hits) != 1:
                unsafe = True  # unknown or ambiguous: can't tell what it does
                continue
            k = hits[0]
            if k == "in-place":
                in_place = True
            elif SED_LONG[k] == "arg":
                if not eq:
                    val = args[i] if i < len(args) else ""
                    i += 1
                if k == "expression":
                    out.append(val)
                    explicit = True
                elif k == "file":
                    unsafe = explicit = True
                elif not val.isdigit():
                    unsafe = True  # a line length that isn't a number: some other reading is meant
            continue
        if is_opt:
            for j, ch in enumerate(a[1:], 1):
                rest = a[j + 1:]
                if ch in "nsuzErab":
                    continue
                if ch in "iI":  # in place; the rest (or, on BSD, the next word) is a backup suffix
                    in_place = True
                    bare_i = bare_i or not rest
                    break
                if ch in "efl":
                    val = rest if rest else (args[i] if i < len(args) else "")
                    if not rest:
                        i += 1
                    if ch == "f":
                        unsafe = explicit = True
                    elif ch == "e":
                        out.append(val)
                        explicit = True
                    else:  # -l: GNU takes a number; BSD -l takes nothing, so the word is its script
                        out.append(val)
                        if not val.isdigit():
                            unsafe = True
                        if not rest:
                            plain.append(val)
                    break
                unsafe = True
                break
            continue
        plain.append(a)
    if plain and (not explicit or opt_after_plain):
        out.append(plain[0])
        if bare_i and len(plain) > 1:
            out.append(plain[1])  # BSD: -i '' 'script' file (the first word was the backup suffix)
    files = plain[1:] if not explicit else plain
    return out, unsafe, in_place, files


def sed_scripts(args):
    """(script texts, uses a script file or an unreadable option)."""
    out, unsafe, _, _ = sed_options(args)
    return out, unsafe


_SED_SAFE = set("pPnNdDgGhHxlz=qQ")


def sed_runs_or_writes(script):
    """Does a sed script write a file or run a command? It is read command by command (addresses, !, blocks,
    s/y with their own delimiter, text and label arguments). Anything it can't read counts as yes."""
    n, i = len(script), 0

    def delimited(i, d):  # index after the closing delimiter, or None
        while i < n:
            if script[i] == "\\":
                i += 2
                continue
            if script[i] == d:
                return i + 1
            if script[i] == "\n":
                return None
            i += 1
        return None

    def address(i):
        if i < n and script[i].isdigit():
            while i < n and script[i].isdigit():
                i += 1
            if i < n and script[i] == "~":
                i += 1
                while i < n and script[i].isdigit():
                    i += 1
        elif i < n and script[i] == "$":
            i += 1
        elif i < n and script[i] == "/":
            i = delimited(i + 1, "/")
        elif i < n and script[i] == "\\" and i + 1 < n:
            i = delimited(i + 2, script[i + 1])
        else:
            return i, False
        if i is None:
            return None, True
        while i < n and script[i] in "IM":
            i += 1
        return i, True

    def to_eol(i):
        while i < n and script[i] != "\n":
            i += 1
        return i

    while True:
        while i < n and script[i] in " \t\n;":
            i += 1
        if i >= n:
            return False
        i, had = address(i)
        if i is None:
            return True
        if had and i < n and script[i] == ",":
            i += 1
            if i < n and script[i] in "+~":
                i += 1
                while i < n and script[i].isdigit():
                    i += 1
            else:
                i, ok = address(i)
                if i is None or not ok:
                    return True
        while i < n and script[i] in " \t!":
            i += 1
        if i >= n:
            return True
        c = script[i]
        i += 1
        if c in "{}":
            continue
        if c in "s":
            if i >= n or script[i] in "\\\n":
                return True
            d = script[i]
            j = delimited(i + 1, d)
            j = delimited(j, d) if j is not None else None
            if j is None:
                return True  # unterminated: can't tell what follows
            k = j
            while k < n and (script[k].isalnum()):
                k += 1
            if re.search(r"[ewW]", script[j:k]):
                return True
            i = k
            continue
        if c == "y":
            if i >= n:
                return True
            d = script[i]
            j = delimited(i + 1, d)
            j = delimited(j, d) if j is not None else None
            if j is None:
                return True
            i = j
            continue
        if c in "aic":  # text to the end of the line, continued by a trailing backslash
            while True:
                e = to_eol(i)
                if e > i and script[e - 1] == "\\" and e < n:
                    i = e + 1
                    continue
                i = e
                break
            continue
        if c in "rR#":  # file name or comment: the rest of the line
            i = to_eol(i)
            continue
        if c in ":btTvFL":  # label or nothing: ends at ; newline or } (stricter than seds that read to the line end)
            while i < n and script[i] not in ";\n}":
                i += 1
            continue
        if c in _SED_SAFE:
            while i < n and script[i].isdigit():  # q5, l70
                i += 1
            continue
        return True  # e, w, W, or something unknown


FIND_WRITE = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}


def writes_files(prog, args):
    """Can this read-type program write files with these arguments?"""
    if prog in ("sed", "gsed", "perl"):
        if prog == "perl":
            return any(_INPLACE.match(a) for a in args)
        scripts, unsafe, in_place, _ = sed_options(args)
        return in_place or unsafe or any(sed_runs_or_writes(x) for x in scripts)
    if prog in ("awk", "gawk", "mawk", "nawk"):
        return any(re.search(r">|\||system|close\(", a) for a in args)
    if prog in ("find", "fd"):
        return any(a in FIND_WRITE or a in ("-x", "-X", "--exec", "--exec-batch") for a in args)
    if prog == "sort":
        return any(re.match(r"^-[A-Za-z]*o", a) or a.startswith(("--output", "--compress-program")) for a in args)
    return False


def is_read_only(cmd):
    """A command that only reads and prints (search, list, show): text typed in it is not writing."""
    if re.search(r"(?<![0-9&])>(?!\s*/dev/null|&)|\btee\b|<<", cmd):
        return False
    for toks, _, _ in segments(cmd):
        prog, args, _ = program(toks)
        if prog == "git":
            sub = next((a for a in args if not a.startswith("-")), "")
            if sub not in RO_GIT:
                return False
            rest = [a for a in args[args.index(sub) + 1:] if not a.startswith("-")] if sub in args else []
            if sub in ("branch", "tag", "remote", "config", "reflog") and rest:
                return False  # git branch x <commit>, git tag, git config k v, git reflog expire: these write
            if any(a.startswith("--output") for a in args):
                return False  # git diff/log --output=file writes a file
        elif writes_files(prog, args):
            return False
        elif prog not in RO_PROGS and prog != "cd":
            return False
    return True


# ---- what this session typed ----

def _iter_recs(f, start=0):
    """Complete JSON records from byte `start` of a words file: (record, end offset)."""
    try:
        with open(f, "rb") as fh:
            fh.seek(start)
            data = fh.read()
    except OSError:
        return
    pos = 0
    while True:
        j = data.find(b"\n", pos)
        if j < 0:
            return  # a half-written last line: read it next time
        line = data[pos:j]
        pos = j + 1
        try:
            yield json.loads(line.decode("utf-8", "ignore")), start + pos
        except ValueError:
            yield None, start + pos


def read_calls(files, since, tail=TAIL_BYTES):
    """[(time, text)] of the tool calls in these logs since `since`, oldest first. Denied calls and
    read-only commands (searches) left out. Only the last `tail` bytes of each log are read."""
    calls, cancelled = [], set()
    for f in files:
        try:
            start = max(0, os.path.getsize(f) - tail) if tail else 0
        except OSError:
            continue
        first = start > 0
        for ev, _ in log_events(f, start):
            if first:  # the first record may be cut off; log_events already skips unparsable ones
                first = False
            if ev[0] == "cancel":
                cancelled.add(ev[1])
            elif ev[1] >= since and not ev[4]:
                calls.append((ev[1], ev[2], ev[3]))
    return sorted((t, w) for t, i, w in calls if i not in cancelled)


INDEX_SECONDS = 1.5          # time per command for adding new tool calls to the session's word index


def _units(w):
    """(typed lines and quoted strings, as typed and formatting-free; names of files written) in a tool call."""
    keys, names = set(), set()
    for l in w.split("\n"):
        l = l.strip()
        if len(l) < 4:
            continue
        if l.startswith("/") and " " not in l and "." in os.path.basename(l):
            names.add(os.path.basename(l))  # a Write/Edit file_path: a file this session wrote
        for m in _REDIRECT.finditer(l):
            names.add(os.path.basename(m.group(1)))  # cat > gen.py <<EOF, echo ... > x.sh, tee x
        for part in [l] + [a or b for a, b in _QUOTED.findall(l)]:
            for x in part.replace("\\n", "\n").split("\n"):
                x = x.strip()
                keys.update((norm(x), fkey(x)))
                if x[:1] in "+-":  # a line of a patch
                    keys.add(fkey(x[1:]))
    return {k for k in keys if len(k) >= 4}, names


def _fp(k):
    return hashlib.blake2b(k.encode("utf-8", "ignore"), digest_size=10).hexdigest()


class OwnIndex:
    """Fingerprints (hashes, not text) of everything this session typed into a writing tool call (lines and
    quoted strings) -> earliest time, and of the names of files it wrote. Built from the session's own logs and
    extended with new log records only, so a long session costs the same per command as a short one."""
    def __init__(self, d, logs):
        import sqlite3
        self.db = None
        try:
            self.db = sqlite3.connect(os.path.join(d, "own_index.db"), timeout=10, isolation_level=None)
            c = self.db
            c.execute("CREATE TABLE IF NOT EXISTS units (k TEXT, t REAL, id TEXT)")
            c.execute("CREATE INDEX IF NOT EXISTS units_k ON units (k)")
            c.execute("CREATE TABLE IF NOT EXISTS wrote (name TEXT, id TEXT)")
            c.execute("CREATE INDEX IF NOT EXISTS wrote_n ON wrote (name)")
            c.execute("CREATE TABLE IF NOT EXISTS cancelled (id TEXT PRIMARY KEY)")
            c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v)")
            c.execute("BEGIN IMMEDIATE")  # one hook at a time adds records
            stop = time.time() + INDEX_SECONDS
            for f in logs:
                row = c.execute("SELECT v FROM meta WHERE k=?", ("off:" + f,)).fetchone()
                off = row[0] if row else 0
                try:
                    if off > os.path.getsize(f):
                        off = 0  # log replaced: read it again (duplicate fingerprints are harmless)
                except OSError:
                    continue
                for ev, end in log_events(f, off):
                    off = end
                    if ev[0] == "cancel":
                        c.execute("INSERT OR IGNORE INTO cancelled VALUES (?)", (ev[1],))
                    elif not ev[4]:
                        _, t, rid, w, _ = ev
                        keys, names = _units(w)
                        c.executemany("INSERT INTO units VALUES (?,?,?)", [(_fp(k), t, rid) for k in keys])
                        c.executemany("INSERT INTO wrote VALUES (?,?)", [(_fp(n), rid) for n in names])
                    if time.time() > stop:
                        break  # the rest is added at the next command
                c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", ("off:" + f, off))
                if time.time() > stop:
                    break
            c.execute("COMMIT")
        except Exception as e:  # no index: lines are traced without "typed earlier" (judged more, never less)
            log({"error": f"own index: {e}"[:200]})
            self.db = None

    def first(self, keys):
        if not self.db or not keys:
            return None
        fps = [_fp(k) for k in keys]
        q = ",".join("?" * len(fps))
        row = self.db.execute(f"SELECT min(t) FROM units WHERE k IN ({q}) "
                              "AND id NOT IN (SELECT id FROM cancelled)", fps).fetchone()
        return row[0] if row else None

    def __contains__(self, name):
        if not self.db:
            return False
        return self.db.execute("SELECT 1 FROM wrote WHERE name=? AND id NOT IN (SELECT id FROM cancelled) LIMIT 1",
                               (_fp(name),)).fetchone() is not None


def first_typed(keys, idx, recent):
    """Earliest time a session typed this text: whole lines from its index, or inside any recent tool call."""
    ts = []
    t = idx.first(keys) if idx else None
    if t is not None:
        ts.append(t)
    for t, w in recent:  # oldest first
        if any(k[:200] in w for k in keys):
            ts.append(t)
            break
    return min(ts) if ts else None


# ---- git: which commits existed before the command ----

def git_out(root, args, inp=None, timeout=5.0):
    import subprocess
    try:
        r = subprocess.run(["git", "-C", root] + args, input=inp, capture_output=True, timeout=max(0.5, timeout))
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def git_dirs(root):
    """(git dir, common git dir) of a work tree; linked worktrees share the common one."""
    g = os.path.join(root, ".git")
    if os.path.isdir(g):
        return g, g
    try:
        txt = open(g).read().strip()
    except OSError:
        return None, None
    if not txt.startswith("gitdir:"):
        return None, None
    gd = os.path.normpath(os.path.join(root, txt[7:].strip()))
    try:
        common = os.path.normpath(os.path.join(gd, open(os.path.join(gd, "commondir")).read().strip()))
    except OSError:
        common = gd
    return gd, common


def git_state(root):
    """{'key': common git dir, 'head': sha, 'refs': [sha, ...]} read straight from the ref files (fast)."""
    gd, cd = git_dirs(root)
    if not cd:
        return None
    if os.path.exists(os.path.join(cd, "reftable")):  # newer ref storage: ask git
        head = git_out(root, ["rev-parse", "-q", "--verify", "HEAD"], timeout=3)
        refs = git_out(root, ["for-each-ref", "--format=%(objectname)"], timeout=3)
        return {"key": cd, "head": head.decode().strip() if head else None,
                "refs": sorted(set(refs.decode().split()))[:GIT_MAX_REFS] if refs else []}
    hexre = re.compile(r"^[0-9a-f]{40,64}$")
    refs, packed = set(), {}
    try:
        for l in open(os.path.join(cd, "packed-refs"), encoding="utf-8", errors="ignore"):
            p = l.strip().split()
            if p and hexre.match(p[0].lstrip("^")):
                refs.add(p[0].lstrip("^"))
                if len(p) > 1:
                    packed[p[1]] = p[0]
    except OSError:
        pass
    loose = {}
    for base, dirs, files in os.walk(os.path.join(cd, "refs")):
        for f in files:
            try:
                v = open(os.path.join(base, f), encoding="utf-8", errors="ignore").read().strip()
            except OSError:
                continue
            if hexre.match(v):
                refs.add(v)
                loose[os.path.relpath(os.path.join(base, f), cd)] = v
        if len(refs) > GIT_MAX_REFS:
            break
    head = None
    try:
        h = open(os.path.join(gd, "HEAD"), encoding="utf-8").read().strip()
        if h.startswith("ref:"):
            name = h[4:].strip()
            head = loose.get(name) or packed.get(name)
        elif hexre.match(h):
            head = h
    except OSError:
        pass
    if head:
        refs.add(head)
    return {"key": cd, "head": head, "refs": sorted(refs)[:GIT_MAX_REFS]}


def git_pre(roots):
    """Before a command: where each repo's branches point, so its end knows which commits already existed."""
    out = {}
    for r in roots:
        g = git_root(r)
        st = git_state(g) if g else None
        if st and st["key"] not in out:
            out[st["key"]] = {**st, "root": g}
    return out


def _cat_batch(root, specs, timeout):
    """git cat-file --batch for `specs`: {spec: bytes} for the ones that exist."""
    raw = git_out(root, ["cat-file", "--batch"], ("\n".join(specs) + "\n").encode(), timeout)
    out, i = {}, 0
    if not raw:
        return out
    for spec in specs:
        j = raw.find(b"\n", i)
        if j < 0:
            break
        head = raw[i:j]
        i = j + 1
        if head.endswith(b" missing") or head.endswith(b" ambiguous"):
            continue
        parts = head.rsplit(None, 2)  # "<sha> <type> <size>"; a missing spec may contain spaces
        if len(parts) != 3 or not parts[2].isdigit():
            break  # unexpected reply: stop reading rather than guess
        size = int(parts[2])
        out[spec] = raw[i:i + size]
        i += size + 1
    return out


def _cmd_revs(cmds):
    """Words in the command that may name commits: `git merge feat`, `git show feat:x.md`, `git checkout abc1`."""
    out = []
    for c in cmds:
        for t in re.split(r"[\s'\"=]+", c):
            t = t.split(":", 1)[0]
            for x in t.split(".."):
                if x and not x.startswith("-") and re.match(r"^[\w./@{}~^-]{2,200}$", x):
                    out.append(x)
    return out


_OLD_MEMO = {}


def git_old_commits(root, pre, since, cmds, deadline, made=()):
    key = (root, since, tuple(cmds), len(made))
    if key not in _OLD_MEMO:
        _OLD_MEMO[key] = _git_old_commits(root, pre, since, cmds, deadline, set(made))
    return _OLD_MEMO[key]


SESSION_STARTED = [None]   # when the guard first saw this session (set per hook run)


def repo_untrusted(root, pre=None):
    """A repo whose git folder was made during this session (git init + plumbing, or a copy of such a repo):
    its commits are the session's own, whatever dates they carry. Clones of older repos (network, or a local
    repo older than the session) are fine. Linked worktrees share the main repo's folder."""
    if SESSION_STARTED[0] is None:
        return False
    gd, cd = git_dirs(root)
    if not cd or _born(cd) <= SESSION_STARTED[0]:
        return False
    try:
        first = open(os.path.join(cd, "logs", "HEAD"), encoding="utf-8", errors="ignore").readline()
    except OSError:
        first = ""
    m = re.search(r"\tclone: from (\S+)", first)
    if not m:
        return True
    src = m.group(1)
    if re.match(r"^(?:https?|ssh|git)://|^[\w.-]+@[\w.-]+:", src):
        return False  # fetched from elsewhere
    src = src[7:] if src.startswith("file://") else src
    sg = git_root(src) if os.path.isdir(src) else None
    return not sg or repo_untrusted(sg)


def _git_old_commits(root, pre, since, cmds, deadline, made):
    if repo_untrusted(root, pre):
        return []
    """[(commit, time)] most likely sources first: commits that existed before this command (any branch, stash,
    the commit before a reset/merge/rebase), or that the command fetched. Commits made during the command don't
    count, whatever date they carry. [] if git did not move and was not run."""
    post = git_state(root)
    if not post:
        return []
    pre_refs = (pre or {}).get("refs") or []
    if pre and post["head"] == pre.get("head") and post["refs"] == pre_refs \
            and not any(re.search(r"\b(?:git|gh)\b", c) for c in cmds):
        return []  # git did not move and was not run: git history is not where new text came from
    left = lambda: deadline - time.time()
    stash = git_out(root, ["stash", "list", "--format=%H"], timeout=left())
    parents = git_out(root, ["log", "-1", "--format=%P", "HEAD"], timeout=left())
    changed = sorted(set(post["refs"]) ^ set(pre_refs))
    order = ([post["head"], (pre or {}).get("head"), "ORIG_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD",
              "FETCH_HEAD", "AUTO_MERGE"] + (parents.decode().split() if parents else [])
             + (stash.decode().split() if stash else []) + _cmd_revs(cmds) + changed + post["refs"])
    order = [c for c in dict.fromkeys(order) if c][:GIT_MAX_CANDS * 8]
    chk = git_out(root, ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
                  ("\n".join(c + "^{commit}" for c in order) + "\n").encode(), left())
    shas = list(dict.fromkeys(l.split()[0] for l in (chk or b"").decode().splitlines() if l.endswith(" commit")))
    if not shas or left() <= 0:
        return []
    # commits that did not exist before the command: reachable from the candidates but not from any ref
    # (or HEAD) as it was before. Of those, only fetched ones count (FETCH_HEAD written during the command).
    new = set()
    if pre_refs:
        inp = "\n".join(shas + ["--not"] + pre_refs) + "\n"
        out = git_out(root, ["rev-list", "--stdin"], inp.encode(), left())
        if out is None:
            return []  # can't tell old from new: count no git history (lines stay judged)
        new = set(out.decode().split())
    fetched = set()
    fh = os.path.join(post["key"], "FETCH_HEAD")
    if new and os.path.exists(fh) and os.path.getmtime(fh) >= since - 1:
        inp = "\n".join(["FETCH_HEAD", "--not"] + pre_refs) + "\n"
        out = git_out(root, ["rev-list", "--stdin"], inp.encode(), left())
        fetched = set(out.decode().split()) if out else set()
    dates = git_out(root, ["show", "-s", "--format=%H %ct %at"] + shas, timeout=left())
    when = {}
    for l in (dates or b"").decode().splitlines():
        p = l.split()
        if len(p) != 3:
            continue
        c, ct, at = p[0], int(p[1]), int(p[2])
        if not pre_refs:  # repo made by this command (clone): only commits dated before it count
            ok = ct < since - 1
        else:
            ok = c not in new or c in fetched
        if c in made:
            ok = False  # made by one of this session's earlier commands: its text is the session's, not history
        if ok:
            when[c] = min(ct, at)
    return [(c, when[c]) for c in shas if c in when][:GIT_MAX_CANDS]


def git_sources(root, pre, since, rels, cmds, deadline, made=()):
    """{relpath: {formatting-free line: {document status: (time, line, commit)}}}: text each file had in the
    commits git_old_commits finds."""
    olds = git_old_commits(root, pre, since, cmds, deadline, made)
    when = dict(olds)
    cands = [c for c, _ in olds]
    left = lambda: deadline - time.time()
    specs = [f"{c}:{r}" for r in rels for c in cands]
    blobs = _cat_batch(root, specs, left()) if specs and left() > 0 else {}
    out = {}
    for spec, data in blobs.items():
        c, r = spec.split(":", 1)
        text = data.decode("utf-8", "ignore")
        st = doc_status(text)
        got = out.setdefault(r, {})
        for l in text.splitlines():
            k = fkey(l)
            if not k:
                continue
            slot = got.setdefault(k, {})
            if st not in slot or when[c] < slot[st][0]:
                slot[st] = (when[c], l.strip(), c)
    return out


def git_blob_sha(raw):
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


def git_old_files(root, pre, since, cmds, deadline, n=6, made=()):
    """{relpath: {blob sha}}: every file version in the likeliest commits that existed before this command.
    A doc that now equals one of them byte for byte came from git whole: none of its lines are new writing."""
    out = {}
    for c, _ in git_old_commits(root, pre, since, cmds, deadline, made)[:n]:
        raw = git_out(root, ["ls-tree", "-r", "-z", "--full-tree", c], timeout=deadline - time.time())
        for ent in (raw or b"").split(b"\0"):
            meta, _, path = ent.partition(b"\t")
            parts = meta.split()
            if len(parts) == 3 and parts[1] == b"blob":
                out.setdefault(path.decode("utf-8", "ignore"), set()).add(parts[2].decode())
        if time.time() > deadline:
            break
    return out


def git_first_time(root, commit, rel, line, deadline):
    """When `line` first appeared in `rel`'s history up to `commit` (author or commit date, the earlier)."""
    out = git_out(root, ["log", "--format=%ct %at", "-S" + line, commit, "--", rel], timeout=deadline - time.time())
    ts = [min(int(x) for x in l.split()) for l in (out or b"").decode().splitlines() if len(l.split()) == 2]
    return min(ts) if ts else None


def _probe(k):
    """The longest word of a formatting-free line: any doc line with the same key contains it as typed."""
    return max(re.findall(r"[^\W_]+", k) or [k], key=len)


def disk_sources(before, cmds, deadline, want=None):
    """{formatting-free line: {document status: time}}: text that docs held before this command (copy, move,
    merge files). Time = when the guard first stored that copy. Files the command names are read first.
    want: only these keys are looked for; a doc (and a line) without one of their words is not split up."""
    probes = sorted({_probe(k) for k in want if k}) if want is not None else None
    named = " ".join(cmds)
    order = sorted(before, key=lambda p: (os.path.basename(p) not in named, os.path.exists(p)))
    out = {}
    for p in order:
        if time.time() > deadline:
            break
        h = before[p]
        text = get_blob(h)
        if text is None:
            continue
        try:
            t = os.path.getmtime(blob_path(h))
        except OSError:
            continue
        if probes is not None:
            hit = [w for w in probes if w in text]
            if not hit:
                continue
        st = doc_status(text)
        for l in text.splitlines():
            if probes is not None and not any(w in l for w in hit):
                continue
            k = fkey(l)
            if k:
                slot = out.setdefault(k, {})
                if st not in slot or t < slot[st]:
                    slot[st] = t
    return out


# ---- what the command could have written ----

def _resolve(tok, dirs):
    return [os.path.normpath(os.path.join(dd, os.path.expanduser(tok))) for dd in dirs]


def command_writers(cmds, cwd, wrote):
    """What this session's code in the command can write:
    {'any': True if it ran code that can write any file (inline program, a script it wrote, an AI agent),
     'named': [(path words, piped_in)] for text tools that write named files (redirects, sed -i, tee),
     'fed': argument phrases this session passed to other programs (they may write those words into docs)}."""
    out = {"any": False, "named": [], "fed": [], "redir": []}
    for c in cmds:
        dirs = roots_for(cwd, c)
        prev = ""
        for toks, piped, raw in segments(c):
            prog, args, path = program(toks)
            if not prog:
                continue
            upstream, prev = (prev if piped else ""), prog
            if prog in TEXT_TOOLS and upstream in OUTSIDE_TOOLS:
                continue  # e.g. curl ... | jq .body > x.md: outside text, reshaped
            targets = [m.group(1) for m in _REDIRECT.finditer(raw)]
            inline = any(re.match(r"^-[A-Za-z]*[ceE]$", a) for a in args) or "<<HEREDOC" in raw \
                or (prog == "deno" and args[:1] == ["eval"])
            if prog in AI_CLIS or (prog in ("find", "fd", "xargs") and (writes_files(prog, args) or prog == "xargs")):
                out["any"] = True  # find -exec / xargs run a command this session wrote
                continue
            if targets and prog not in TEXT_TOOLS and prog not in OUTSIDE_TOOLS and upstream not in OUTSIDE_TOOLS \
                    and (prog in DECODERS or (piped and upstream) or "<<" in raw):
                out["redir"].append(targets)  # this session's input, reshaped (base64 -d, xxd -r), into a named file
            scripts = [path] + [a for a in args if not a.startswith("-")][:1]
            if INTERP.match(prog) and "-m" in args:
                mod = args[args.index("-m") + 1] if args.index("-m") + 1 < len(args) else ""
                scripts += [mod.replace(".", "/") + ".py", mod.replace(".", "/") + "/__main__.py"]
            own_script = any(s and os.path.basename(s) in wrote and any(os.path.isfile(p) for p in _resolve(s, dirs))
                             for s in scripts)
            if (INTERP.match(prog) and inline) or own_script:
                out["any"] = True
                continue
            if prog in TEXT_TOOLS:
                edits = [a for a in args if not a.startswith("-")] if (
                    prog == "perl" and any(_INPLACE.match(a) for a in args)) else []
                if prog in ("sed", "gsed"):
                    scripts, unsafe, in_place, files = sed_options(args)
                    edits = files if in_place else []
                    if unsafe or any(sed_runs_or_writes(x) for x in scripts):
                        out["any"] = True  # e/w in the script: it can run or write anything, -i or not
                        continue
                if writes_files(prog, args) and not edits:
                    out["any"] = True  # sed w, awk > file, sort -o: writes where its own script says
                    continue
                if targets or edits or (prog == "tee"):
                    out["named"].append((targets + edits + (args if prog == "tee" else []), piped))
                continue
            # a program this session did not write: the words it was handed may still end up in a doc
            phrase, cur = [], []
            for a in args + [""]:
                if a and not a.startswith("-") and "/" not in a and "." not in a and re.match(r"^[\w'-]+$", a):
                    cur.append(a)
                else:
                    phrase += [f"{x} {y}" for x, y in zip(cur, cur[1:]) if x[:1].isupper() or y[:1].isupper()]
                    cur = []
            out["fed"] += phrase + [q for q in (a or b for a, b in _QUOTED.findall(raw)) if len(q.split()) >= 2]
    return out


def _names(p, words):
    """Does a list of command words name file `p` (path, file name, or a matching wildcard)?"""
    import fnmatch
    b = os.path.basename(p)
    for w in words:
        w = w.strip("'\"")
        if not w:
            continue
        if os.path.basename(w) == b or (any(ch in w for ch in "*?[") and fnmatch.fnmatch(b, os.path.basename(w))):
            return True
    return False


def trace(data, d, lines, src, ctx):
    """{line: kind} for each new line: 'own', 'code' (both judged) or 'skip:<where it came from>'."""
    since, cmds, cwd = ctx["since"], ctx["cmds"], data.get("cwd") or os.getcwd()
    hard = HOOK_T0 + 40  # tracing never eats the judge's time
    logs = session_logs(data.get("transcript_path", ""))
    idx = OwnIndex(d, logs)
    wrote = idx
    recent = read_calls(logs, since - 60)
    cur = (data.get("tool_input") or {}).get("command", "")
    if not is_read_only(cur):
        recent.append((ctx["t"], tool_words(data.get("tool_input") or {})))
    others = {}
    for name, files in other_logs(logs, since - 60, born_before=ctx["t"]).items():
        if time.time() > hard:
            break  # fewer other sessions known: more lines judged, never fewer
        if name != data.get("session_id"):
            w = read_calls(files, since - 60)
            if w:
                others[name] = "\n".join(x for _, x in w)
    stop = min(time.time() + TRACE_TOTAL, hard)
    deadline = min(time.time() + TRACE_SECONDS, hard)
    cw = command_writers(cmds, cwd, wrote)
    info = {}
    for ln in lines:
        keys = line_keys(ln)
        own_t = first_typed(keys, idx, recent)
        # the whole line, not just its start; very short lines are too common to tell whose they are
        by = [n[:8] for n, w in others.items() if any(len(k) >= OTHER_MIN_CHARS and k in w for k in keys)]
        info[ln] = (keys, own_t, by)
        if time.time() > stop:
            break

    own_git = {}  # repo -> this session made commits there (or made the repo): text only they hold is its own

    def via_own_git(p):
        g = git_root(os.path.dirname(p)) if p else None
        if not g:
            return False
        if g not in own_git:
            st = git_state(g)
            pre = ctx["git"].get(st["key"]) if st else None
            own_git[g] = bool(st and ctx.get("made", {}).get(st["key"])) or repo_untrusted(g, pre)
        return own_git[g]

    def could_write(p):
        return cw["any"] or any(_names(p, ws) for ws, _ in cw["named"]) or any(_names(p, ws) for ws in cw["redir"]) \
            or via_own_git(p)

    fed_files = set()  # docs a program wrote using words this session handed it (arguments, typed text)
    for ln in lines:
        if any(ph in ln for ph in cw["fed"]) or info[ln][1] is not None:
            fed_files.add(src.get(ln, ""))
    # earlier text in git or other docs only matters for lines this session may have written
    need = [ln for ln, (_, own_t, by) in info.items()
            if own_t is not None or (not by and (could_write(src.get(ln, "")) or src.get(ln, "") in fed_files))]
    gsrc, dsrc, by_root = {}, None, {}
    for ln in need:
        p = src.get(ln, "")
        g = git_root(os.path.dirname(p)) if p else None
        if g:
            by_root.setdefault(g, set()).add(os.path.relpath(p, g))
    for g, rels in by_root.items():
        if time.time() < deadline:
            st = git_state(g)
            pre = ctx["git"].get(st["key"]) if st else None
            gsrc[g] = git_sources(g, pre, since, sorted(rels), cmds, deadline,
                                  ctx.get("made", {}).get(st["key"] if st else "", ()))
    statuses = {}
    kinds, skipped = {}, []
    for ln in lines:
        if ln not in info or time.time() > stop:  # out of time: judge it rather than guess
            kinds[ln] = "code"
            continue
        keys, own_t, by = info[ln]
        p = src.get(ln, "")
        if own_t is None and by:
            kinds[ln] = "skip:another session typed it"
            skipped.append({"line": ln[:160], "by": by})
            continue
        if ln not in need:
            kinds[ln] = "skip:new text, but nothing this session wrote could have written it"
            continue
        if p not in statuses:
            statuses[p] = doc_status(read_text(p) or "")
        st, fk = statuses[p], fkey(ln)
        g = git_root(os.path.dirname(p)) if p else None
        rel = os.path.relpath(p, g) if g else ""
        hit = gsrc.get(g, {}).get(rel, {}).get(fk, {}).get(st)  # same text, under the same document label
        times = []
        if hit:
            t = hit[0]
            if own_t is not None and t >= own_t - FIRST_SLACK and time.time() < deadline:
                first = git_first_time(g, hit[2], rel, hit[1], deadline)
                if first is not None:
                    t = min(t, first)
            times.append((t, "git history"))
        if dsrc is None:
            dsrc = disk_sources(ctx["before"], cmds, deadline, want={fkey(x) for x in need})
        if st in dsrc.get(fk, {}):
            times.append((dsrc[fk][st], "another doc on disk"))
        out_t, where = min(times) if times else (None, None)
        if out_t is not None and (own_t is None or out_t < own_t - FIRST_SLACK):
            kinds[ln] = "skip:" + where
        elif own_t is not None or any(_names(p, ws) for ws, piped in cw["named"] if not piped):
            kinds[ln] = "own"   # typed by this session, or new words in a file its own text tool was told to edit
        else:
            kinds[ln] = "code"  # new words while its code ran, or from a tool it handed its words to
    if skipped:
        log({"session": (data.get("session_id") or "")[:8], "skipped_other_session": skipped})
    other = [{"line": ln[:160], "why": k[5:]} for ln, k in kinds.items()
             if k.startswith("skip:") and k != "skip:another session typed it"]
    if other:
        log({"session": (data.get("session_id") or "")[:8], "skipped_not_this_session": other[:50]})
    return kinds


def finished_ids(data, ids):
    """Tool calls in `ids` that already have a result in the transcript (failed, denied, interrupted...)."""
    tp, found = data.get("transcript_path") or "", set()
    paths = [tp] if tp else []
    sub = os.path.join(tp[:-6], "subagents") if tp.endswith(".jsonl") else ""
    if sub and os.path.isdir(sub):
        paths += [os.path.join(sub, f) for f in os.listdir(sub)
                  if f.endswith(".jsonl") and time.time() - os.path.getmtime(os.path.join(sub, f)) < INFLIGHT_MAX_AGE]
    for p in paths:
        try:
            with open(p, "rb") as fh:
                fh.seek(max(0, os.path.getsize(p) - (3 << 20)))
                tail = fh.read().decode("utf-8", "ignore")
        except OSError:
            continue
        for line in tail.splitlines():
            if '"tool_result"' in line:
                found |= {i for i in ids if i in line}
    return found


def session_dir(session):
    d = os.path.join(STATE, "sessions", re.sub(r"[^\w.-]", "_", session or "nosession").lstrip(".") or "nosession")
    os.makedirs(d, exist_ok=True)
    return d


def load(d, name, default):
    f = os.path.join(d, name)
    if not os.path.exists(f):
        return default
    try:
        return json.load(open(f))
    except ValueError:  # corrupt: start fresh instead of failing every command after this
        log({"error": f"corrupt {name}, reset", "dir": os.path.basename(d)})
        return default


def save_json(d, name, obj):
    """Write to a temp file, then swap it in: readers never see a half-written file."""
    tmp = os.path.join(d, f".{name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, os.path.join(d, name))


class locked:
    """One hook process at a time per session (parallel shell commands run hooks in parallel)."""
    def __init__(self, d):
        self.f = open(os.path.join(d, ".lock"), "w")

    def __enter__(self):
        fcntl.flock(self.f, fcntl.LOCK_EX)

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def blob_path(h):
    return os.path.join(STATE, "blobs", h[:2], h + ".gz")


def put_blob(text):
    """Store text once (by content hash, gzip). Returns the hash."""
    import gzip
    data = text.encode("utf-8", "ignore")
    h = hashlib.sha1(data).hexdigest()
    p = blob_path(h)
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f"{p}.{os.getpid()}.tmp"
        with gzip.open(tmp, "wb", compresslevel=6) as f:
            f.write(data)
        os.replace(tmp, p)
    return h


def get_blob(h):
    import gzip
    try:
        with gzip.open(blob_path(h), "rb") as f:
            return f.read().decode("utf-8", "ignore")
    except (OSError, EOFError):
        return None


def read_text(p):
    try:
        return open(p, encoding="utf-8", errors="ignore").read()
    except OSError:
        return None


def prune(keep):
    """Once a day: delete sessions idle PRUNE_DAYS, then stored copies no remaining session uses,
    then the old v1 snapshot folder."""
    mark = os.path.join(STATE, ".last_prune")
    try:
        if os.path.exists(mark) and time.time() - os.path.getmtime(mark) < 86400:
            return
        open(mark, "w").close()
        sroot, broot = os.path.join(STATE, "sessions"), os.path.join(STATE, "blobs")
        os.makedirs(sroot, exist_ok=True)
        os.makedirs(broot, exist_ok=True)
        used = set()
        for name in os.listdir(sroot):
            p = os.path.join(sroot, name)
            if name != keep and time.time() - os.path.getmtime(p) > PRUNE_DAYS * 86400:
                never_checked(name[:8], load(p, "pending.json", []), "session ended first")
                shutil.rmtree(p, ignore_errors=True)
                continue
            for meta in load(p, "index.json", {}).values():
                if len(meta) > 2:
                    used.add(meta[2])
        for sub in os.listdir(broot):
            for f in os.listdir(os.path.join(broot, sub)):
                fp = os.path.join(broot, sub, f)
                # 1-hour grace: a copy saved just now may not be in an index yet
                if f[:-3] not in used and time.time() - os.path.getmtime(fp) > 3600:
                    os.remove(fp)
        shutil.rmtree(os.path.join(STATE, "snap"), ignore_errors=True)  # v1 storage, replaced
    except OSError:
        pass


MADE_MAX = 20000  # commits remembered per repo as made by this session


def record_made_commits(d, pre_states, started):
    """Commits that appeared while this command ran (made by it, or by anything running at the same time),
    except fetched ones, are added to the session's list: later commands don't count them as history.
    {repo key: [commit]} for the whole session."""
    made = load(d, "made_commits.json", {})
    changed = False
    for key, pre in (pre_states or {}).items():
        root = (pre or {}).get("root")
        post = git_state(root) if root else None
        if not post or post["refs"] == pre.get("refs") and post["head"] == pre.get("head"):
            continue
        inp = "\n".join([c for c in [post["head"]] + post["refs"] if c] + ["--not"] + (pre.get("refs") or [])) + "\n"
        out = git_out(root, ["rev-list", "--stdin"], inp.encode(), 3)
        new = set((out or b"").decode().split())
        fh = os.path.join(post["key"], "FETCH_HEAD")
        if new and os.path.exists(fh) and os.path.getmtime(fh) >= started - 1:
            f = git_out(root, ["rev-list", "--stdin"], ("\n".join(["FETCH_HEAD", "--not"] + (pre.get("refs") or []))
                                                        + "\n").encode(), 3)
            new -= set((f or b"").decode().split())
        if new:
            made[key] = (made.get(key, []) + sorted(new))[-MADE_MAX:]
            changed = True
    if changed:
        save_json(d, "made_commits.json", made)
    return {k: set(v) for k, v in made.items()}


def git_head_text(p):
    root = git_root(os.path.dirname(p))
    if not root:
        return None
    import subprocess
    try:
        r = subprocess.run(["git", "-C", root, "show", "HEAD:" + os.path.relpath(p, root)],
                           capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return r.stdout if r.returncode == 0 else None


def running(inflight, now, data=None):
    """Commands still running. A command whose end hook never fired (failed commands in older setups, denied by
    another hook, interrupted) is dropped once the transcript shows its result, else after INFLIGHT_MAX_AGE."""
    live = {k: v for k, v in inflight.items() if now - v["t"] < INFLIGHT_MAX_AGE}
    old = [k for k, v in live.items() if now - v["t"] > FINISHED_CHECK_AGE and k != "?"]
    if old and data is not None:
        for k in finished_ids(data, old):
            live.pop(k, None)
    return live


def session_started(d):
    """When the guard first saw this session: the marker's creation time (touch can't move it), else the
    session folder's. Never None once the folder exists."""
    f = os.path.join(d, "started")
    t = _born(f)
    return t if t != float("inf") else _born(d) if _born(d) != float("inf") else time.time()


def bash_pre(data):
    d = session_dir(data.get("session_id"))
    if not os.path.exists(os.path.join(d, "started")):
        open(os.path.join(d, "started"), "w").close()  # first command: anything git-made after this is the session's
    cwd, cmd = data.get("cwd") or os.getcwd(), (data.get("tool_input") or {}).get("command", "")
    roots = roots_for(cwd, cmd)
    gstate = git_pre(roots)   # where branches point now: commits that exist before this command
    found = scan(roots, cmd)  # slow part, outside the lock
    with locked(d):
        now = time.time()
        inflight = running(load(d, "inflight.json", {}), now, data)
        # files changed after the earliest still-running command started may be that command's
        # unjudged write: leave their "before" copy alone so its end can still see the change
        busy_since = min([v["t"] for v in inflight.values()], default=now)
        idx, dirty = load(d, "index.json", {}), False
        for p, meta in found.items():
            old = idx.get(p)
            if old and old[:2] == meta:
                continue
            if meta[0] >= busy_since - 1:  # new or changed while a sibling runs: its end judges it
                continue
            text = read_text(p)
            if text is not None:
                idx[p], dirty = meta + [put_blob(text)], True
        if dirty:
            save_json(d, "index.json", idx)
        tid = data.get("tool_use_id") or "?"
        inflight[tid] = {"t": now, "roots": roots, "cmd": cmd[:20000]}
        save_json(d, "inflight.json", inflight)
        save_json(d, git_file(tid), gstate)  # own file: a repo's ref list can be large
    os.utime(d)  # session active, for prune()
    prune(os.path.basename(d))


def git_file(tid):
    return "git-" + hashlib.sha1(tid.encode()).hexdigest()[:16] + ".json"


def add_pending(d, entries):
    """Lines written to disk but not judged (judge down, too slow, too many): the next shell command
    in this session judges them, so they are not lost."""
    if not entries:
        return
    with locked(d):
        cur = load(d, "pending.json", [])
        seen = {(e["path"], e["line"]) for e in cur}
        cur += [e for e in entries if (e["path"], e["line"]) not in seen]
        save_json(d, "pending.json", cur)


def drop_pending(d, keys):
    with locked(d):
        cur = load(d, "pending.json", [])
        save_json(d, "pending.json", [e for e in cur if (e["path"], e["line"]) not in keys])


def still_in_file(e, path=None):
    text = read_text(path or e["path"])
    body = STATUS_SUFFIX.sub("", e["line"])
    return text is not None and any(l.strip() == body for l in text.splitlines())


def moved_to(e, paths, deadline):
    """A line still waiting to be judged whose file was moved or renamed: the watched doc it is in now."""
    for p in paths:
        if time.time() > deadline:
            return None
        if p != e["path"] and still_in_file(e, p):
            return p
    return None


def never_checked(session, entries, why):
    if entries:
        log({"session": session, "never_checked": [{"path": e["path"], "line": e["line"][:240]} for e in entries],
             "why": why})


def bash_post(data):
    d = session_dir(data.get("session_id"))
    sess = (data.get("session_id") or "")[:8]
    cwd, cmd = data.get("cwd") or os.getcwd(), (data.get("tool_input") or {}).get("command", "")
    with locked(d):
        lines, src, since, ctx = bash_post_diff(d, cwd, cmd, data.get("tool_use_id") or "?", data)
        queued = load(d, "pending.json", [])
        if queued:
            save_json(d, "pending.json", [])
    try:
        kinds = trace(data, d, lines, src, ctx) if lines else {}
    except Exception as e:  # tracing failed: judge every line (the old behaviour), never lose them
        log({"error": f"trace: {e}"[:300], "session": sess})
        kinds = {ln: "code" for ln in lines}
    lines = [ln for ln in lines if kinds.get(ln) in ("own", "code")]
    now, retry, gave_up = time.time(), [], []
    follow_until = now + 1.5
    for e in queued:
        if not still_in_file(e):
            p = moved_to(e, ctx["paths"], follow_until)
            if not p:
                continue  # fixed or deleted since: nothing left to judge
            e = {**e, "path": p}  # moved or renamed: still this session's unjudged line
        if now - e["t"] > PENDING_HOURS * 3600 or e["tries"] >= PENDING_TRIES:
            gave_up.append(e)
        else:
            retry.append(e)
    never_checked(sess, gave_up, "retries used up")
    entry = {e["line"]: e for e in retry}
    for ln in lines:
        entry.setdefault(ln, {"line": ln, "path": src.get(ln, ""), "t": now, "tries": 0})
    todo = list(entry)  # earlier unjudged lines first, then this command's
    notes = []
    if gave_up:
        notes.append(f"decision-guard: {len(gave_up)} line(s) could not be checked after several tries and were "
                     "given up on (listed in the guard log as never_checked): "
                     + "; ".join(f"{os.path.basename(e['path'])}: {e['line'][:80]}" for e in gave_up[:3]))
    if not todo:
        return {"systemMessage": notes[0]} if notes else None
    files = list(dict.fromkeys(entry[ln]["path"] for ln in todo))
    names = ", ".join(os.path.basename(p) for p in files[:5])
    left = []
    # saved first, as one more try: if judging fails or the hook is killed, the next command still has them
    add_pending(d, [{**entry[ln], "tries": entry[ln]["tries"] + 1} for ln in todo])
    rot, _ = find_rot(data.get("transcript_path", ""), todo, "bash: " + names, leftover=left, files=files)
    drop_pending(d, {(entry[ln]["path"], ln) for ln in todo})  # judged (or left over, re-added below)
    add_pending(d, [entry[ln] for ln in left])
    if left:
        notes.append(f"decision-guard: {len(left)} line(s) not checked yet (too many, or the judge was slow); "
                     "they will be checked after your next shell command.")
    if rot:
        rfiles = list(dict.fromkeys(entry[ln]["path"] for ln, _ in rot))
        earlier = [ln for ln, _ in rot if ln in {e["line"] for e in retry}]
        shared = [ln for ln, _ in rot if kinds.get(ln) == "code"]
        return {"decision": "block", "reason": explain("shell write to " + names, rot)
                + "\n\nFiles with flagged lines:\n" + "\n".join(f"  - {p}" for p in rfiles[:5]) + "\n"
                + (f"{len(earlier)} of these lines were written by an earlier command, when the check could not run.\n"
                   if earlier else "")
                + "The file was already written. "
                + ("These lines trace back to your own tool calls (text you typed, or your edit of existing words): "
                   "fix them now.\n" if len(shared) < len(rot) else "")
                + (f"{len(shared)} line(s) are new text that appeared while your command ran code you wrote, and no "
                   "other source has them:\n" + "\n".join(f"  - {ln[:120]}" for ln in shared[:5]) + "\n"
                   "If your code wrote them, fix them. If it did not (another program changed the same file at the "
                   "same moment), do NOT edit, revert or dismiss them: tell the user which file and lines were "
                   "flagged and let the user decide.\n" if shared else "")
                + ("\n" + "\n".join(notes) if notes else "")}
    return {"systemMessage": "\n".join(notes)} if notes else None


def bash_post_diff(d, cwd, cmd, tid, data=None):
    now = time.time()
    inflight = running(load(d, "inflight.json", {}), now, data)
    me = inflight.pop(tid, None) or {"t": now - 1, "roots": []}
    # a change counts as this session's if made after the earliest of: this command's start,
    # or any sibling command still running (its write may land in a file this command sees first)
    since = min([me["t"]] + [v["t"] for v in inflight.values()])
    save_json(d, "inflight.json", inflight)
    roots = list(dict.fromkeys(me["roots"] + roots_for(cwd, cmd)))
    idx, dirty = load(d, "index.json", {}), False
    lines, touched, src = [], [], {}
    before = {p: rec[2] for p, rec in idx.items() if len(rec) > 2}  # each doc's copy from before this command
    git = load(d, git_file(tid), {})
    made = record_made_commits(d, git, me["t"])
    SESSION_STARTED[0] = session_started(d)
    for k in inflight:  # a running sibling's start state is older, so it knows more commits as "before"
        for g, st in load(d, git_file(k), {}).items():
            git.setdefault(g, st)
    cmds = [c for c in [cmd] + [v.get("cmd", "") for v in inflight.values()] if c]  # siblings may have made it
    gfiles, git_deadline, from_git = {}, time.time() + TRACE_SECONDS, []
    found = scan(roots, cmd)
    for p, meta in found.items():
        rec = idx.get(p)
        if rec and rec[:2] == meta:
            continue
        g = git_root(os.path.dirname(p)) if meta[0] >= since - 1 else None
        if g:
            if g not in gfiles:
                st = git_state(g)
                gfiles[g] = git_old_files(g, git.get(st["key"]) if st else None, since, cmds, git_deadline,
                                          made=made.get(st["key"] if st else "", ()))
            if gfiles[g]:
                try:
                    raw = open(p, "rb").read()
                except OSError:
                    raw = None
                if raw is not None and git_blob_sha(raw) in gfiles[g].get(os.path.relpath(p, g), ()):
                    from_git.append(p)  # the whole file is a version git had before: nothing new written
                    idx[p], dirty = meta + [put_blob(raw.decode("utf-8", "ignore"))], True
                    continue
        new = read_text(p)
        if new is None:
            continue
        try:
            fst = os.stat(p)
        except OSError:
            continue
        # mv and cp -p keep a file's old modified time; its change time (ctime) still moves, so a doc
        # written elsewhere and moved in is still seen
        moved_in = meta[0] < since - 1 <= fst.st_ctime
        if meta[0] < since - 1 and not moved_in:
            old = None   # changed before any running command started (another session, an editor): don't judge
        elif rec:
            old = get_blob(rec[2]) if len(rec) > 2 else git_head_text(p)
        elif getattr(fst, "st_birthtime", 0) >= since - 1:
            old = ""     # created by this session's command
        elif moved_in:
            old = git_head_text(p) or ""  # moved in: what git had is old, everything else is new (then traced)
        else:
            old = git_head_text(p)  # modified, never seen: compare to git if possible, else skip
        if old is not None:
            nl = with_status(new_lines_shell(new, old), new)
            if nl:
                lines += nl
                touched.append(p)
                for ln in nl:
                    src.setdefault(ln, p)
        idx[p], dirty = meta + [put_blob(new)], True  # judged (or skipped) once: siblings won't repeat it
    if dirty:
        save_json(d, "index.json", idx)
    if from_git:
        log({"session": ((data or {}).get("session_id") or "")[:8], "skipped_whole_file_from_git": len(from_git),
             "files": [os.path.basename(p) for p in from_git[:20]]})
    try:
        os.remove(os.path.join(d, git_file(tid)))
    except OSError:
        pass
    live = {git_file(k) for k in inflight}
    for f in os.listdir(d):  # start states of commands whose end never came
        if f.startswith("git-") and f not in live and now - os.path.getmtime(os.path.join(d, f)) > INFLIGHT_MAX_AGE:
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
    ctx = {"since": since, "t": me["t"], "cmds": cmds, "git": git, "before": before, "made": made,
           "paths": list(found)}
    return lines, src, since, ctx


# ---------- main ----------

def rejected_message(e):
    """Why nothing was judged. A wrong model name isn't the content's fault, so don't tell the agent to split the write."""
    if "does not exist" in str(e) or "not a valid model" in str(e):
        return (f"DECISION GUARD: the decision model \"{MODEL}\" wasn't found ({str(e)[:160]}), so nothing was judged and this "
                "is not allowed through. Tell the user: fix the model name with /plugin configure "
                "decision-guard@decision-guard, or leave it empty for the default.")
    return (f"DECISION GUARD: could not check this content ({str(e)[:200]}). "
            "Nothing was judged, so it is not allowed through. Split the write into smaller pieces "
            "or shorten very long lines, then retry.")


def main():
    data = json.load(sys.stdin)
    event, tool = data.get("hook_event_name"), data.get("tool_name", "")
    unjudged = None
    use_saved_log(data)
    if event == "UserPromptSubmit":
        return
    try:
        if tool == "Bash":
            if event == "PreToolUse":
                bash_pre(data)
                return
            out = bash_post(data)
            if os.environ.get("DECISION_GUARD_DEBUG"):  # tests: proof the after-step finished
                log({"session": (data.get("session_id") or "")[:8], "post_done": True,
                     "secs": round(time.time() - HOOK_T0, 2), "blocked": bool(out and out.get("decision"))})
            if out:
                print(json.dumps(out))
            return
        if tool == "Artifact":
            out = artifact_pre(data)
            if out:
                print(json.dumps(out))
            return
        where, lines = lines_for_tool(tool, data.get("tool_input") or {})
        if tool in ("Write", "Edit", "MultiEdit") and where:
            unjudged = (where, lines)  # if the judge is down, the next shell command re-checks these
        rot, _ = find_rot(data.get("transcript_path", ""), lines, tool,
                          files=[where] if where and where != tool else None)
        if rot:
            saved_cancel(data)
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": explain(os.path.basename(where) or tool, rot)}}))
    except judge.ContentRejected as e:  # the content itself defeated the check: don't let it through
        log({"error": "content rejected: " + str(e)[:280], "tool": tool, "event": event,
             "session": (data.get("session_id") or "")[:8]})
        msg = rejected_message(e)
        if event == "PreToolUse" and tool != "Bash":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                              "permissionDecision": "deny", "permissionDecisionReason": msg}}))
        elif tool == "Bash" and event != "PreToolUse":
            print(json.dumps({"decision": "block", "reason": msg + " The file was already written: check it now."}))
    except Exception as e:  # network/service outage: fail open, but visibly
        log({"error": str(e)[:300], "tool": tool, "event": event,
             "session": (data.get("session_id") or "")[:8]})
        if unjudged and unjudged[1]:
            try:  # if the write happens, its lines are judged after the next shell command
                add_pending(session_dir(data.get("session_id")),
                            [{"line": ln, "path": unjudged[0], "t": time.time(), "tries": 1}
                             for ln in dict.fromkeys(unjudged[1])])
            except OSError:
                pass
        print(json.dumps({"systemMessage": f"decision-guard: check skipped ({str(e)[:120]})"}))


if __name__ == "__main__":
    main()
