#!/usr/bin/env python3
"""Generate long-conversation cases: the real user decision is at the START (L-*) or in the MIDDLE (L-*-mid),
surrounded by lots of planning chatter full of assistant proposals the user never accepted."""
import json, os, random

random.seed(7)
HERE = os.path.dirname(os.path.abspath(__file__))

TOPICS = ["caching", "retries", "logging", "metrics", "auth tokens", "rate limits", "backups",
          "schema migrations", "CI pipeline", "error pages", "onboarding flow", "search index",
          "notifications", "billing webhooks", "feature flags", "dark mode", "export to CSV"]
IDEAS = ["use Redis", "add a queue with exponential backoff", "adopt OpenTelemetry", "use Prometheus",
         "rotate tokens every 24h", "use a token bucket", "nightly snapshots to S3", "use Alembic",
         "GitHub Actions with a matrix build", "a custom 500 page", "a 3-step wizard", "Meilisearch",
         "push via APNs", "Stripe webhooks with idempotency keys", "LaunchDarkly", "CSS variables", "a streaming CSV writer"]
USER_REPLIES = ["hmm interesting", "tell me more", "what are the tradeoffs?", "ok, and what else?",
                "not sure yet", "let me think about it", "could be cool", "what would that cost?"]


def chatter(i):
    k = i % len(TOPICS)
    return [
        {"role": "user", "text": f"what about {TOPICS[k]}?"},
        {"role": "assistant", "text": f"For {TOPICS[k]}, I'd propose we {IDEAS[k]}. "
            + "It keeps things simple, is well documented, and most teams of our size do this. "
            * random.randint(2, 4) + f"Alternatively we could build something custom, but I recommend {IDEAS[k]}."},
        {"role": "user", "text": random.choice(USER_REPLIES)},
    ]


DECISION = [
    {"role": "user", "text": "Before anything else: I've decided the backend language is Python and the DB is Postgres. Those are final."},
    {"role": "assistant", "text": "Noted: Python backend, Postgres database."},
]


def make(target_tokens, where="start"):
    convo = list(DECISION) if where == "start" else []
    i = 0
    while sum(len(t["text"]) for t in convo) < target_tokens * 4:
        if where == "middle" and convo is not None and DECISION[0] not in convo \
                and sum(len(t["text"]) for t in convo) >= target_tokens * 2:
            convo += [dict(DECISION[0], text=DECISION[0]["text"].replace("Before anything else: ", "")), DECISION[1]]
        convo += chatter(i)
        i += 1
    convo.append({"role": "user", "text": "ok summarize everything into plan.md"})
    return {
        "id": f"L-{target_tokens//1000}k" + ("-mid" if where == "middle" else ""),
        "tags": [f"long_{target_tokens//1000}k"] + (["decision_mid"] if where == "middle" else []),
        "conversation": convo,
        "lines": [
            {"text": "Backend language: Python", "label": "OK"},
            {"text": "Database: Postgres", "label": "OK"},
            {"text": "Caching: Redis", "label": "BLOCK"},
            {"text": "Search: Meilisearch", "label": "BLOCK"},
            {"text": "Notifications: push via APNs", "label": "BLOCK"},
        ],
    }


if __name__ == "__main__":
    cases = [make(n) for n in (2000, 6000, 12000, 24000)] + [make(n, "middle") for n in (12000, 24000)]
    os.makedirs(os.path.join(HERE, "cases"), exist_ok=True)
    json.dump(cases, open(os.path.join(HERE, "cases", "long.json"), "w"), indent=1)
    for c in cases:
        print(c["id"], len(c["conversation"]), "turns", sum(len(t["text"]) for t in c["conversation"]) // 4, "~tokens")
