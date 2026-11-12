# Changelog

## 0.2.2

- Fixed: docs moved into a repo inside a whole folder (for example from Claude's temp folder) kept their old dates and were not checked. A doc that wasn't there before a shell command is now treated as new, whatever its dates say. Old docs in a folder watched for the first time are still not checked.

## 0.2.1

- Fixed: answers given in question boxes were often not shown to the model. Claude Code words them in different ways ("The user answered: ...", "Your questions have been answered: ..."), and only one wording was recognized. Lines recording those answers were flagged as undecided. Answers are now recognized by the question box they reply to, whatever the wording, and long typed answers are kept whole.
- Fixed: the log could grow to hundreds of MB when many lines went unchecked. Each such record now lists at most 50 lines, plus a count.

## 0.2.0

- Added a `decision_model` setting: use any decision model on OpenRouter.
- Lines are now sent to the model as state. This is much more accurate on Jev.
- Writes to Claude's temp folders are now checked.
- A mistyped model name now gets a clear message.

## 0.1.0

- First release as a Claude Code plugin, with a masked prompt for the OpenRouter key.
- Added a setup check at session start: warns about a missing key, a missing or old Python, or a second manual install.
