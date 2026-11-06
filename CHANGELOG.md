# Changelog

## 0.2.0

- Added a `decision_model` setting: use any decision model on OpenRouter.
- Lines are now sent to the model as state. This is much more accurate on Jev.
- Writes to Claude's temp folders are now checked.
- A mistyped model name now gets a clear message.

## 0.1.0

- First release as a Claude Code plugin, with a masked prompt for the OpenRouter key.
- Added a setup check at session start: warns about a missing key, a missing or old Python, or a second manual install.
