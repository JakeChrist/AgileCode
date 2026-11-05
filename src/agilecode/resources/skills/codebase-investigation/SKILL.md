---
name: codebase-investigation
description: Locate relevant code, trace callers, and turn a ticket into bounded requirements without proposing unrelated work.
---
# Codebase investigation
Use this skill when the ticket requires discovering unfamiliar behavior. Do not load it merely because search is available.

Start with the ticket's objective and explicit exclusions. Use path search for likely modules, literal search for exact names, and symbol search for definitions. Read the actual code around each result and follow callers, imports, configuration, and tests. Treat reference-search hits as candidates, not proof of semantic linkage.

Separate observed behavior from hypotheses. Cite paths and line ranges in your findings. Distinguish the original project context from the ticket worktree; the latter is the implementation authority. Do not modify production source code during understanding, requirements, architecture, or planning. Creating or editing repository documents explicitly assigned to the current step is permitted.

Define only requirements needed to satisfy the ticket and preserve relevant existing behavior. Use stable REQ-1 style identifiers and observable acceptance conditions, with verification methods only as detailed as the work warrants. Do not turn hypothetical edge cases or optional improvements into new requirements. Identify materially missing intent with a focused question rather than inventing a feature. Keep architecture and implementation planning as narrow as the real change allows. Test design and execution belong to their assigned verification steps.
