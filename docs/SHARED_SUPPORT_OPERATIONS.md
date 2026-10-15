# Shared supporting operations

This note records the concrete ownership and safety changes for configuration,
catalogs, jobs and backup. It supplements the approved implementation plan and
does not replace full-suite or installed-package qualification.

## File-backed CLI editing

Ordinary settings `show`, provider `list` and team `export` retain their existing
plain-data output. Safe edits across separate CLI invocations use an edit envelope:

- `agilecode settings show --for-edit --scope project > settings-edit.json`
- `agilecode teams export global:my-team --for-edit > team-edit.json`
- `agilecode providers export local --for-edit > provider-edit.json`

Edit only the envelope's `value` object. Retain `expected`, `scope` and any
`expected_default` fields. Then use the ordinary `settings save`, `teams save` or
`providers save` command with that file. The envelope's scope is used when no
`--scope` flag is supplied; a conflicting flag is rejected. `--make-default`
checks the provider default captured with the edit.

The CLI passes the captured base to the existing configuration or catalog
operation. It does not implement a second merge policy. Independent provider/team
entries and independent settings fields survive each other's changes. A newer
conflicting value causes a conflict instead of a silent overwrite.

Plain JSON still creates a new provider/team or initializes an empty settings
scope. It cannot overwrite an existing definition without a captured base.
Errors explain how to export an editable snapshot. Built-in teams remain
immutable; export plain JSON and save a new global/project copy to customize one.

## Catalog ownership

`application/services/catalog.py` owns team selection/storage, catalog-facing
validation and bundled skill overlay policy. Canonical skill discovery, live
loading, profile storage, bundled installation and the one-time profile upgrade
are in the cohesive `application/services/catalog_resources.py` module.

`adapters/skills.py` and `core/pm_profiles.py` contain compatibility reexports of
the same class objects. They have no alternative policy bodies. Callers retaining
those import paths therefore execute the same canonical implementation. Atomic
replacement and shared-file locks remain low-level adapters.

Policies are preserved: project skills override valid global entries, skill text
is loaded live and records its actual hash, customized profile content is retained,
and differing bundled skill replacements are backed up before overlay. This
extraction does not freeze skills for a run or change customization policy.

## Jobs and artifacts

`JobService` owns registration, run/attempt association checks, recorded starting/
running/terminal transitions, scoped reads and routing to the actual live shell.
`ArtifactService` owns artifact creation, finalization and byte-range reads.
`ShellService` retains Bash/process handles, input/output bytes, environment
capture, process-group termination and process completion observation. It reports
record transitions to `JobService`; it no longer inserts or updates job rows.

Human and agent output readers call the same operations. Agent callers remain
ticket-scoped. A stored PID never confers live ownership.

### Crash-orphan recovery

`JobService.recover_unowned()` conservatively records `job_recovery` blockers for
starting/running/unknown jobs that have lost their live owner. It leaves the job
`unknown` and does not invent a completion time. Reopening preserves the same
blocker rather than treating an absent in-memory handle as a stopped process.

There is deliberately no automatic signal based on a legacy stored PID. A dead
shell leader would also not prove that all children stopped. The current public
Stop Job operation cannot control a process from an earlier engine instance.
The original process and its children must be identified and their cessation
independently verified before an explicit recovery operation may resolve the
blocker. A generic acknowledgement, another reopen or a bare PID is insufficient;
no new acknowledgement-only unblock command is supplied by this change.

## Unverified analysis backup

A completed-looking analysis record without a validated completion receipt is
not proof that its worktree is disposable. Domain-only backup preserves selected
unverified analysis work through its retained branch/checkpoint contract, or
reports the resource as unavailable. Restore checks actual completion evidence
before treating analysis as finished historical work. Verified completed analysis
whose branch/worktree were removed remains valid history without a recreated
worktree.

## Focused regression evidence

- `test_cli_edit_snapshots.py` invokes the real parser and execution adapter across
  fresh hosts, including conflicting/disjoint edits and unchanged read formats.
- `test_job_catalog_ownership.py` traces actual foreground/background job writes
  to their shared owner, checks compatibility aliases and scoped tool output,
  and proves an abruptly orphaned detached job stays blocked across two reopens.
- `test_backup_resource_roles.py` includes a real domain-only unverified-analysis
  backup/restore and two reopens preserving changed worktree content.
- Existing supporting-operation, worktree-environment, QML settings, pause,
  catalog customization and backup-role tests continue to exercise preserved
  behavior. Full qualification status belongs in the final verification record.
