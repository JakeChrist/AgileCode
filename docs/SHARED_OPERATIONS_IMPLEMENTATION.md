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

## Verification status

Implementation and verification are in progress. This marker does not claim
that any architectural change, regression test or package qualification has
been completed. Baseline results, implemented scope and final checks will be
recorded as work proceeds. Passing, failing, skipped and unrun checks must be
distinguished.
