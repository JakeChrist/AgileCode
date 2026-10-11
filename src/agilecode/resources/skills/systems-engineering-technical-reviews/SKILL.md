---
name: systems-engineering-technical-reviews
description: Coordinate risk-based SRR, PDR, CDR, TRR, and system acceptance reviews without making them mandatory project gates.
---
# Systems Engineering Technical Reviews

Use this skill when a consequential system or subsystem milestone warrants a deliberate technical readiness decision. Reviews are selective and proportional to technical risk, not mandatory stages or extra steps for every ticket.

## Review purposes

- **SRR — System Requirements Review:** Determine whether requirements adequately capture stakeholder needs and are sufficiently clear, consistent, feasible, and verifiable to support design. Delegate detailed evaluation to the Requirements Engineering team.
- **PDR — Preliminary Design Review:** Determine whether the proposed architecture can plausibly meet requirements, with major subsystem boundaries, interfaces, assumptions, alternatives, and risks sufficiently understood. Delegate detailed evaluation to architecture and other relevant specialists.
- **CDR — Critical Design Review:** Determine whether detailed designs, interfaces, and key technical decisions are mature enough for the intended implementation and integration. Delegate technical examination to the design and architecture specialists.
- **TRR — Test Readiness Review:** Determine whether the product, test environments, procedures, resources, and objective criteria are sufficiently ready for planned verification activities. Delegate assessment to the verification and integration specialists.
- **System Acceptance Review:** Determine whether integration, verification, validation, and intended-use evidence support accepting a system or milestone, and identify any remaining limitations. Coordinate the appropriate engineering teams and stakeholder decisions.

## Applying reviews

1. Choose whether a review is useful based on complexity, cross-team dependencies, consequences of failure, architectural commitment, or unresolved technical risk.
2. Define focused entrance criteria (the evidence needed for a meaningful review) and exit criteria (the readiness questions that must be resolved). These are not automatic workflow gates.
3. Delegate preparation, specialist analysis, and verification of technical claims to the teams that own those disciplines.
4. Evaluate the returned findings at the system level. Decide whether to **proceed**, **proceed with explicit risks and action items**, or **hold/rework** due to material deficiencies or insufficient evidence.
5. Coordinate any follow-up through existing tickets and reassess when the necessary evidence becomes available.

Use system or subsystem milestones as appropriate; unrelated work need not stop for a review. Do not repeat specialist requirements analysis, detailed design, or verification yourself. Avoid ceremonial reviews, unnecessary documents, and new Project Manager execution machinery.
