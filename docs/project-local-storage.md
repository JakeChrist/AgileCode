# Project-local AgileCode storage format (version 1)

AgileCode stores durable board information in `.agilecode/` under the Git repository root. In a non-Git
workspace it uses the workspace-folder root instead. It never chooses a user-level or machine-global fallback.
Both kinds of board use exactly the same layout:

```text
.agilecode/
├── store.json
├── board.json
├── settings.json
├── tickets/
│   └── AC-004.json
└── archive/
    └── AC-003.json
```

AgileCode **does not** add this directory to `.gitignore`. Teams may commit it so tickets travel with the
repository. Every JSON file is UTF-8, formatted with two-space indentation, ends with a newline, and has a
`formatVersion`. Version 1 readers must reject unsupported versions rather than guessing. A future migration
must update files atomically and retain a recoverable copy until the new store validates.

## Files and relationships

- `store.json` identifies the schema version and board scope. `scope.kind` is `git` or `workspace`, so the
  representation works for either identity contract. The directory location remains authoritative; `rootPath`
  is diagnostic and must be refreshed after the project moves.
- `tickets/<ticket-id>.json` contains one active ticket record using the ticket domain contract. The case-sensitive
  filename must exactly equal `<ticket-id>.json`; IDs cannot contain path separators. Creating or editing one ticket
  therefore never rewrites other ticket records.
- `archive/<ticket-id>.json` contains one ticket whose state is `archived`. The record retains `archivedFrom`,
  `archivedAt`, review feedback, blocked and failed-attempt summaries, and task-history IDs. Archiving is an atomic
  move from `tickets/` to `archive/`; restoring performs the reverse move and restores `archivedFrom`.
- `board.json` contains only ordered ticket IDs for the six active workflow columns. Each active ticket occurs once,
  in the column matching its lifecycle state. Archived IDs never occur on the board.
- `settings.json` is reserved for repository-specific, portable preferences. Version 1 has no setting keys, so its
  complete content is `{ "formatVersion": 1 }`. Secrets and user- or machine-specific preferences do not belong here.

An initialized empty store creates all five entries (including empty `tickets/` and `archive/` directories). Its
JSON files are:

```json
// store.json (comments shown here are not written to disk)
{
	"formatVersion": 1,
	"scope": {
		"id": "git:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"kind": "git",
		"rootPath": "/work/project"
	}
}
```

```json
// board.json
{
	"formatVersion": 1,
	"columns": {
		"backlog": [],
		"ready": [],
		"in_progress": [],
		"blocked": [],
		"review": [],
		"done": []
	}
}
```

```json
// settings.json
{ "formatVersion": 1 }
```

## Deliberate exclusions

Project storage contains concise references to ordinary task history, not a second history system. No file or field
may contain full Chat transcripts, complete agent responses, terminal logs, copied task messages, credentials,
provider secrets, caches, temporary execution state, absolute default save locations outside the board root, or
complete duplicate task-history records. The schemas are strict so transcript- or log-shaped extra fields fail
validation rather than silently becoming part of the format.

The executable schemas and cross-file consistency checks live in `@roo-code/types`. Readers validate each record,
then validate the assembled store to detect duplicate identities, missing ticket files, misplaced archives, and
board/state mismatches.
