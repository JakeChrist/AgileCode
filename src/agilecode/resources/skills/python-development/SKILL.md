---
name: python-development
description: Implement focused Python changes with explicit interfaces, guarded edits, and error handling.
---
# Focused Python implementation
Use this skill for Python implementation when it is relevant to the assigned step.

Follow the persisted plan. Inspect existing conventions and dependencies before adding abstractions. Favor small cohesive objects and composition. Add type annotations at public boundaries and explicit validation for external input. Keep domain code independent of UI dependencies.

Read each file and use its returned byte hash for changes. Prefer exact replacements over rewriting unrelated content. Preserve formatting, encoding, and newline conventions. If the expected hash is stale, reread and reconcile the current file rather than forcing the write.

Make the changes assigned by the implementation plan. Report the files changed and any material deviations from the plan. The engine handles the final commit.
