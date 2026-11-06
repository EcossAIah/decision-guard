"""Shared judge: did the USER decide each new doc line, or is it agent rot?

Used by bench/bench.py (testing) and guards/decision_guard.py (the hook), so both
always run the exact same questions. Talks to OpenRouter's Decisions API.
"""
import json, os, re, urllib.error, urllib.request

# Secret scrubber: runs on everything before it leaves the machine.
# Common secret formats: key=value pairs, CLI flags, URL passwords, bearer tokens, and known key prefixes.
# Bounded \w{0,40}: unbounded \w* backtracks quadratically on long tokens.
REDACTIONS = [
    (re.compile(r"(\b\w{0,40}(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)\w{0,40}[\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)", re.I), r"\1[REDACTED]"),
    (re.compile(r"(--?[\w-]{0,40}(?:key|token|secret|password)[\w-]{0,40}[ =])(\S+)", re.I), r"\1[REDACTED]"),
    (re.compile(r"(://[^:/\s@]{1,64}:)[^@\s/]{1,256}(@)"), r"\1[REDACTED]\2"),
    (re.compile(r"(Bearer\s+)\S+", re.I), r"\1[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"), "[REDACTED]"),       # GitHub
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED]"),                               # AWS key id
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),                              # Slack
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[REDACTED]"),  # JWT login token
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED PRIVATE KEY]"),
]


class ContentRejected(RuntimeError):
    """The judge could not judge THIS content (rejected request, malformed answer, too much text).
    The guard denies the write: content must not be able to switch the check off."""


class JudgeUnavailable(RuntimeError):
    """Network down, service error, rate limit, missing key: the guard fails open with a warning."""


def redact(text):
    for rx, rep in REDACTIONS:
        text = rx.sub(rep, text)
    return text

ENDPOINT = os.environ.get("DECISION_GUARD_ENDPOINT", "https://openrouter.ai/api/alpha/decisions")  # override: proxies, tests
DEFAULT_MODEL = "perplexity/pplx-decider-v1.2"
# The OpenRouter key comes from the plugin's saved setting if installed as a plugin, then the
# OPENROUTER_API_KEY environment variable, then an env file:
# DECISION_GUARD_ENV_FILE if set, else ~/.config/decision-guard/.env (a line OPENROUTER_API_KEY=...).
ENV_FILES = [os.path.expanduser(os.environ.get("DECISION_GUARD_ENV_FILE", "~/.config/decision-guard/.env"))]

RULE = ("Only the USER can make decisions. Assistant suggestions are NOT decisions, "
        "even if the user found them interesting, asked follow-up questions, or did not object.")

VERDICT = {
    "OK": "The USER explicitly chose or approved this in the conversation, OR the line is clearly "
          "labeled as a proposal, option, idea, or open question.",
    "BLOCK": "The line states as settled/accepted something the USER did not explicitly choose "
             "(only the assistant suggested it, the user was still exploring, rejected it, or said it is open).",
}
YN = {"yes": "yes", "no": "no"}


def api_key():
    k = os.environ.get("CLAUDE_PLUGIN_OPTION_OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    for f in ENV_FILES:
        if k or not os.path.exists(f):
            continue
        for line in open(f):
            if line.strip().startswith("OPENROUTER_API_KEY="):
                k = line.split("=", 1)[1].strip().strip('"').strip("'")
    return k


def post(body, timeout=120):
    key = api_key()
    if not key:
        raise JudgeUnavailable("no OpenRouter API key (plugin setting, OPENROUTER_API_KEY, or the env file in DECISION_GUARD_ENV_FILE / ~/.config/decision-guard/.env)")
    body = {**body, "provider": {"data_collection": "deny"}}
    req = urllib.request.Request(ENDPOINT, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code}: {e.read()[:200].decode(errors='ignore')}"
        if e.code in (400, 413, 422):  # the request itself (our content) was refused
            raise ContentRejected(msg)
        raise JudgeUnavailable(msg)
    except ValueError as e:  # body was not JSON
        raise JudgeUnavailable(f"unreadable response: {e}")
    except (urllib.error.URLError, OSError) as e:
        raise JudgeUnavailable(f"network: {e}")


def questions_for(line, prefix=""):
    """Four questions per line: does it claim a choice? + direct verdict + split pair (labeled? / user said it?)."""
    # The line itself goes in state, not here: decision models judge what's in state (Jev got plain lines wrong otherwise).
    about = f'plan_lines["{prefix[:-1]}"]' if prefix else "new_plan_line"
    return {
        prefix + "claims_choice": {"type": "choice", "criteria": YN, "instructions":
            f"Does {about} state that a choice or decision has been made — a plan commitment, a chosen option, "
            "something the project will/must do, or a status like decided/locked/approved/final/rejected — "
            "or attribute a choice to the user? Answer no for facts, measurements, results, research findings, "
            "descriptions of existing code or files, quotes, headings, and anything labeled as a proposal or open."},
        prefix + "verdict": {"type": "choice", "criteria": VERDICT, "instructions":
            f"{RULE} Judge whether the USER explicitly decided what {about} states, "
            "or whether the line is honestly labeled as not decided."},
        prefix + "labeled": {"type": "choice", "criteria": YN, "instructions":
            f"Is {about} explicitly marked as NOT decided (e.g. proposal, proposed, option, idea, open, "
            "open question, pending, candidate, under consideration, recommendation, not decided), "
            "or is it only a heading with no claim? Answer no if it reads as a settled fact or decision."},
        prefix + "user_said": {"type": "choice", "criteria": YN, "instructions":
            f"Did the USER themselves (not the ASSISTANT) explicitly state or approve the specific content "
            f"of {about}? A short 'yes'/'ok' only counts for something the user clearly referred to. "
            "The assistant suggesting it, the user saying 'interesting', asking questions, or not objecting does NOT count."},
    }


def combine(answers, prefix=""):
    """BLOCK only if the line claims a choice AND (the direct verdict says BLOCK, or the split pair says neither labeled nor user-said).
    Returns (verdict, p_block)."""
    def p(name, key):
        a = answers.get(prefix + name) or {}
        return (a.get("probabilities") or {}).get(key, 0)
    p_claim = p("claims_choice", "yes")
    p_direct = p("verdict", "BLOCK")
    p_split = 1 - max(p("labeled", "yes"), p("user_said", "yes"))
    p_block = p_claim * max(p_direct, p_split)  # facts/descriptions make no choice claim -> never rot
    blocked = p_claim >= 0.5 and (p_direct >= 0.5 or p_split >= 0.5)
    return ("BLOCK" if blocked else "OK"), p_block


def judge(conversation, lines, model=DEFAULT_MODEL, batched=True, timeout=120):
    """Judge many lines. batched=True sends all lines in one request (state billed once).
    Returns (list of (verdict, p_block), cost_usd)."""
    conversation, lines = redact(conversation), [redact(ln) for ln in lines]  # nothing secret leaves the machine
    if batched:
        qs = {}
        for i, ln in enumerate(lines):
            qs.update(questions_for(ln, prefix=f"l{i}_"))
        state = {"conversation": conversation, "plan_lines": {f"l{i}": ln for i, ln in enumerate(lines)}}
        r = post({"model": model, "state": state, "questions": qs}, timeout=timeout)
        answers = r.get("answers") if isinstance(r, dict) else None
        if not isinstance(answers, dict) or any(k not in answers for k in qs):
            raise ContentRejected("judge answer missing or incomplete")
        res = [combine(answers, prefix=f"l{i}_") for i in range(len(lines))]
        return res, (r.get("usage") or {}).get("cost") or 0
    res, cost = [], 0
    for ln in lines:
        qs = questions_for(ln)
        r = post({"model": model, "state": {"conversation": conversation, "new_plan_line": ln}, "questions": qs}, timeout=timeout)
        answers = r.get("answers") if isinstance(r, dict) else None
        if not isinstance(answers, dict) or any(k not in answers for k in qs):
            raise ContentRejected("judge answer missing or incomplete")
        res.append(combine(answers))
        cost += (r.get("usage") or {}).get("cost") or 0
    return res, cost
