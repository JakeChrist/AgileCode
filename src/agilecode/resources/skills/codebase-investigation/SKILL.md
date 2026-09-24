---
name: codebase-investigation
description: Locate relevant code, trace callers, and turn a ticket into bounded requirements without proposing unrelated work.
---
# Codebase investigation
Use this skill when the ticket requires discovering unfamiliar behavior. Do not load it merely because search is available.

Start with the ticket's objective and explicit exclusions. Use path search for likely modules, literal search for exact names, and symbol search for definitions. Read the actual code around each result and follow callers, imports, configuration, and tests. Treat reference-search hits as candidates, not proof of semantic linkage.

Separate observed behavior from hypotheses. Cite paths and line ranges in your findings. Distinguish the original project context from the ticket worktree; the latter is the implementation authority. Do not modify source during understanding, requirements, architecture, or planning.

Requirements should use stable REQ-1 style identifiers, observable acceptance conditions, and specific verification methods. Identify missing intent with a focused question rather than inventing a feature. A plan should identify changed interfaces, necessary tests, and the order of implementation.
