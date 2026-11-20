---
name: technical-planning-delegation
description: Delegate discrete, self-contained tickets using provisional near-term plans and evolving epic scope as evidence accumulates.
---
# Technical Planning and Delegation

Use this skill to decide what engineering work should be assigned next, not to generate a start-to-finish contract or an exhaustive backlog.

- Maintain the project's overall objectives while keeping its **working plan provisional and near-term**. Look a few steps ahead when those steps are supported by evidence; distinguish likely future activities from work sufficiently understood to assign now.
- Treat each **ticket as a small, discrete, self-contained unit of work** with one coherent outcome that can be completed, verified, and integrated once genuine dependencies are satisfied. Broader project objectives and epics are not executable tickets.
- Decompose larger objectives into separately executable tickets when their outcomes can be delivered independently. Keep coupled changes together when necessary for one complete result, including changes across files or disciplines; do not split artificially by file, team role, or process step.
- **Size before creating each ticket.** Ask: (1) Can this task be decomposed into smaller tasks that each yield a distinct, meaningful, verifiable result? (2) Would a competent developer or relevant specialist familiar with the project reasonably need more than one working day to complete and verify it? Aim for an afternoon; use one human working day as an approximate upper bound. If either answer is yes, decompose further and repeat both questions for each proposed ticket. An entire feature, subsystem, or workbench is too broad even if it can be named as one deliverable. Stop before splitting the internal implementation steps of one cohesive change into separate tickets. This is PM judgment, not an automatic enforcement gate or an agent execution-time limit.
- Specify each ticket's objective, relevant constraints, intended deliverable, and completion conditions. Focused investigations, requirements, feasibility findings, design choices, implementations, integration results, and verification evidence are valid kinds of work, but they do not justify bundling independent outcomes.
- Delegate each ticket to the team and process appropriate for the work. Do not assign development by default when the task is actually investigation, requirements engineering, architecture, or other specialist work.
- When a task's findings may substantially alter subsequent decisions, assign that task without manufacturing detailed future implementation tickets. Once results arrive, inspect them and decide what work still makes sense.
- Organize distinct, independently assessable capabilities into focused epics **when their boundaries are sufficiently understood**. Epics group related tickets; they are organizational records, not executable tickets or a requirement to decide every epic at project start. Adjust or split broad epics as engineering knowledge improves; avoid catch-all epics and arbitrary ticket-count targets.
- Identify genuine dependencies and opportunities for parallel work. Epic ticket numbers express rough order, not an enforced sequence of engineering stages.
- Reassess the near-term plan and work breakdown after material findings, changed requirements, architectural decisions, and integration or verification results. Completed tasks may invalidate prior assumptions, eliminate planned work, or reveal better next steps.

Keep simple changes simple. Avoid needless paperwork, speculative full-lifecycle plans, mandatory phase sequences, or additional project-management loops.
