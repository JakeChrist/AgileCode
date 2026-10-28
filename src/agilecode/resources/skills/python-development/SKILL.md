---
name: python-development
description: Implement focused Python changes with explicit interfaces, guarded edits, and error handling.
---
# Focused Python implementation
Use this skill for Python implementation when it is relevant to the assigned step.

Follow the persisted plan. Inspect existing conventions and dependencies before adding abstractions. Favor small cohesive objects and composition. Add type annotations at public boundaries and explicit validation for external input. Keep domain code independent of UI dependencies.

Read each file and use its returned byte hash for changes. Prefer exact replacements over rewriting unrelated content. Preserve formatting, encoding, and newline conventions. If the expected hash is stale, reread and reconcile the current file rather than forcing the write.

Iterate on incomplete implementation and ordinary coding errors, using focused sanity checks only when needed to guide edits. Do not write verification tests or conduct independent V&V or code review; those are later stages. Record deviations from the plan and why they were necessary. Do not make intermediate Git commits; the engine controls the final commit stage.
