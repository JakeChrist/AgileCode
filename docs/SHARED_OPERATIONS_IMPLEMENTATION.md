# Shared operations implementation record

## Rollback boundary

This work implements the reviewed Shared Implementation Plan approved on
2026-10-15. The original repository baseline is:

`411cd20fff918271ddeb26d2c32c175ee5f20496`

The first commit in this series is explicitly titled
`BEGIN shared-operations consolidation (rollback baseline 411cd20)`.
It adds this record only, before application changes. The baseline is also
identified by the local tag `agilecode/pre-shared-operations-2026-10-15`.

Each following implementation commit must stay scoped to this consolidation.
The final verification record will identify the last commit in the series.
To revert, inspect and revert that explicit baseline-to-final range on a clean
working tree. Do not blindly revert through a later HEAD containing unrelated
work, and do not use a destructive reset on a shared branch.

Application rollback and project-data rollback are separate. Preserve compatible
project and affected global-data backups before applying a data migration. An
older binary must not be opened against unsupported newer data merely because
its code has been restored.

## Governing requirements

- One implementation per operation, with GUI, CLI and model-tool wrappers.
- Separate project definitions and operations, agent execution, and presentation.
- One conversation engine implementation for ticket steps, PM and discussions.
- Auto Complete is permission only; it has no separate execution workflow.
- Leaving In Progress settles managed ticket work before the move succeeds.
- Remove duplicate implementations and bypasses, rather than retaining parallel
  implementations behind a new interface.
- Preserve the existing behavior of product choices the approved plan leaves
  open. Do not silently introduce additional permission or lifecycle policies.
- Retain usable history, resource ownership and honest interrupted outcomes.

## Implemented scope

The implementation consolidates ticket movement/execution, agent conversations,
configuration/catalog operations, jobs/artifacts, source operations, backup and
migration ownership. See `SHARED_OPERATIONS_ARCHITECTURE.md` for the owner map,
`SHARED_CONVERSATION_OPERATIONS.md` for removed duplicate loops, and
`SHARED_SUPPORT_OPERATIONS.md` for settings-edit and crash-orphan behavior.

## Verification

The untouched baseline was tested separately: 262 passed, 18 failed, no skips.
Its 24 real QML tests included 20 passes and 4 failures. Existing stale fixture
expectations were corrected where they contradicted the recorded application
behavior; regression tests exercise the replacement behavior rather than merely
removing assertions.

The consolidated source suite passed **563 tests, with zero failures, errors or
skips**, on Python 3.13.5 with desktop dependencies installed, on 2026-10-15.
Additional focused architecture checks trace actual SQLite movement writes from
the GUI, CLI and registered PM tool to the same owner; deliberate duplicate and
bypass injections are rejected. Tests include real Git/worktree/file operations,
controlled cancellation, abrupt process death, restart twice, stale drafts,
permission revocation, real Qt/QML, schema upgrades and backup restoration.

The delivery verification report records the exact final commit, installed wheel
and source-distribution qualification, artifact hashes, environment, warnings and
checks not performed. Historical verification documents are marked as historical
rather than being presented as evidence for this revision.

Passing these tests is evidence for the tested cases, not a guarantee of an
error-free application. Live local-model behavior and the user's interactive
machine still require validation in that environment. Crash-orphan jobs remain
blocked when safe physical ownership cannot be established; they are not silently
marked stopped or signalled by an unverified PID.
