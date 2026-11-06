#!/bin/sh
# Runs at session start. The guard lets writes through when it can't run, so a broken setup must be loud.
# Plain sh on purpose: it has to work when Python is the thing that's missing. Prints nothing when all is well.

problems=""
add() { problems="${problems}${problems:+ }$1"; }

if ! command -v python3 >/dev/null 2>&1; then
  add "python3 was not found, so no doc lines are being checked. Install Python 3.9 or newer."
elif ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
  add "python3 is older than 3.9, so no doc lines are being checked. Install Python 3.9 or newer."
fi

env_file="${DECISION_GUARD_ENV_FILE:-$HOME/.config/decision-guard/.env}"
if [ -z "$CLAUDE_PLUGIN_OPTION_OPENROUTER_API_KEY" ] && [ -z "$OPENROUTER_API_KEY" ] &&
   ! grep -Eq "^[[:space:]]*OPENROUTER_API_KEY=[\"']?[^\"'[:space:]]" "$env_file" 2>/dev/null; then
  add "No OpenRouter API key is set, so no doc lines are being checked. Add one by typing /plugin configure decision-guard@decision-guard in Claude Code."
fi

# A manual install from before the plugin would run every check twice, and pay twice.
if grep -q 'hooks/decision-guard/decision_guard.py' "$HOME/.claude/settings.json" 2>/dev/null; then
  add "The guard is also installed by hand in ~/.claude/settings.json, so every check runs and is paid for twice. Remove those hook entries."
fi

[ -z "$problems" ] && exit 0

# Only fixed text goes in the message above, so it needs no JSON escaping (paths and keys are never included).
msg="decision-guard: $problems"
printf '{"systemMessage": "%s", "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "%s Tell the user about this."}}\n' "$msg" "$msg"
