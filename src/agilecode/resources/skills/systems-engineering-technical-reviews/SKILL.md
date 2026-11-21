---
name: systems-engineering-technical-reviews
description: Coordinate discretionary technical reviews and user-enabled formal SRR, PDR, CDR, TRR, and system acceptance checkpoints.
---
# Systems Engineering Technical Reviews

Use this skill when a consequential system or subsystem milestone warrants a deliberate technical readiness decision. Without formal checkpoints enabled, reviews remain selective and proportional to technical risk; they are not extra steps for every ticket.

## Review purposes

- **SRR — System Requirements Review:** Determine whether requirements adequately capture stakeholder needs and are sufficiently clear, consistent, feasible, and verifiable to support design. Delegate detailed evaluation to the Requirements Engineering team.
- **PDR — Preliminary Design Review:** Determine whether the proposed architecture can plausibly meet requirements, with major subsystem boundaries, interfaces, assumptions, alternatives, and risks sufficiently understood. Delegate detailed evaluation to architecture and other relevant specialists.
- **CDR — Critical Design Review:** Determine whether detailed designs, interfaces, and key technical decisions are mature enough for the intended implementation and integration. Delegate technical examination to the design and architecture specialists.
- **TRR — Test Readiness Review:** Determine whether the product, test environments, procedures, resources, and objective criteria are sufficiently ready for planned verification activities. Delegate assessment to the verification and integration specialists.
- **System Acceptance Review:** Determine whether integration, verification, validation, and intended-use evidence support accepting a system or milestone, and identify any remaining limitations. Coordinate the appropriate engineering teams and stakeholder decisions.

## Applying reviews

1. Choose whether a review is useful based on complexity, cross-team dependencies, consequences of failure, architectural commitment, or unresolved technical risk.
2. Define the evidence needed for a meaningful review and the readiness questions to discuss. These do not create automatic workflow gates.
3. Delegate preparation, specialist analysis, and verification of technical claims to the teams that own those disciplines.
4. For discretionary reviews, evaluate the findings and decide whether to proceed or arrange further work. For formal checkpoints, the project owner makes the Go/No-Go decision after the presentation and discussion.
5. Coordinate any follow-up through existing tickets and reassess when the necessary evidence becomes available.

When the project enables formal checkpoints, follow its selected ordered list (SRR, PDR, CDR, TRR, System Acceptance Review). The PM determines when work is sufficient for the next review, creates a normal ticket for a substantial Markdown slide presentation, waits for that ticket to finish and integrate, then begins the review using begin_checkpoint_review. Ticket execution pauses while the PM briefs the project owner, answers questions, and discusses the technical evidence. The project owner gives Go/No-Go; the PM decides what work follows and whether to re-review. Do not create hardcoded engineering phase stops or mandatory ticket-step checks.

For discretionary reviews, unrelated work need not stop. Delegate specialist analysis rather than duplicating it. Use existing tickets, processes, and the PM conversation.
