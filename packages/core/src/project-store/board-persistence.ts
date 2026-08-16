import { randomUUID } from "crypto"
import * as fs from "fs/promises"
import * as path from "path"

import {
	activeBoardStates,
	agileCodeBoardSchema,
	agileCodeRepositorySettingsSchema,
	type AgileCodeBoard,
	type Ticket,
} from "@roo-code/types"
import type { ZodType } from "zod"

export interface BoardPersistenceOptions {
	/** Test seam for simulating an interruption before atomic replacement. */
	beforeCommit?: (temporaryPath: string, destinationPath: string) => void | Promise<void>
}

export interface BoardReconciliationIssue {
	id: string
	kind:
		| "unknown-active-reference"
		| "unknown-archive-reference"
		| "missing-active-reference"
		| "missing-archive-reference"
		| "state-mismatch"
	location: string
}

export interface BoardReconciliationResult {
	board: AgileCodeBoard
	issues: BoardReconciliationIssue[]
}

function recordPath(rootPath: string, name: "board.json" | "settings.json"): string {
	return path.join(rootPath, ".agilecode", name)
}

async function readValidated<T>(filePath: string, schema: ZodType<T>): Promise<T> {
	const parsed = schema.safeParse(JSON.parse(await fs.readFile(filePath, "utf8")))
	if (!parsed.success)
		throw new Error(`${path.basename(filePath)} failed validation: ${parsed.error.issues[0]?.message}`)
	return parsed.data
}

async function replaceValidated<T>(
	filePath: string,
	value: unknown,
	schema: ZodType<T>,
	options: BoardPersistenceOptions,
): Promise<T> {
	const parsed = schema.safeParse(value)
	if (!parsed.success)
		throw new Error(`${path.basename(filePath)} failed validation: ${parsed.error.issues[0]?.message}`)
	const temporaryPath = path.join(path.dirname(filePath), `.${path.basename(filePath)}.${randomUUID()}.tmp`)
	const handle = await fs.open(temporaryPath, "wx", 0o600)
	try {
		await handle.writeFile(`${JSON.stringify(parsed.data, null, 2)}\n`, "utf8")
		await handle.sync()
	} finally {
		await handle.close()
	}
	try {
		await options.beforeCommit?.(temporaryPath, filePath)
		await fs.rename(temporaryPath, filePath)
	} finally {
		await fs.unlink(temporaryPath).catch(() => undefined)
	}
	return parsed.data
}

export function reconcileBoardOrdering(
	board: AgileCodeBoard,
	activeTickets: readonly Ticket[],
	archivedTickets: readonly Ticket[],
): BoardReconciliationResult {
	const active = new Map(activeTickets.map((ticket) => [ticket.id, ticket]))
	const archived = new Set(archivedTickets.map((ticket) => ticket.id))
	const placed = new Set<string>()
	const issues: BoardReconciliationIssue[] = []
	const columns: AgileCodeBoard["columns"] = {
		backlog: [],
		ready: [],
		in_progress: [],
		blocked: [],
		review: [],
		done: [],
	}

	for (const state of activeBoardStates) {
		for (const id of board.columns[state]) {
			const ticket = active.get(id)
			if (!ticket) issues.push({ id, kind: "unknown-active-reference", location: state })
			else if (ticket.lifecycle.state === "archived") {
				issues.push({ id, kind: "state-mismatch", location: `${state}->archived` })
			} else if (ticket.lifecycle.state !== state) {
				issues.push({ id, kind: "state-mismatch", location: `${state}->${ticket.lifecycle.state}` })
				columns[ticket.lifecycle.state].push(id)
				placed.add(id)
			} else {
				columns[state].push(id)
				placed.add(id)
			}
		}
	}
	for (const ticket of [...activeTickets].sort((a, b) => a.id.localeCompare(b.id))) {
		if (!placed.has(ticket.id) && ticket.lifecycle.state !== "archived") {
			issues.push({ id: ticket.id, kind: "missing-active-reference", location: ticket.lifecycle.state })
			columns[ticket.lifecycle.state].push(ticket.id)
		}
	}

	const archiveOrder = board.archiveOrder.filter((id) => {
		if (archived.has(id)) return true
		issues.push({ id, kind: "unknown-archive-reference", location: "archiveOrder" })
		return false
	})
	for (const id of [...archived].sort((a, b) => a.localeCompare(b))) {
		if (!archiveOrder.includes(id)) {
			issues.push({ id, kind: "missing-archive-reference", location: "archiveOrder" })
			archiveOrder.push(id)
		}
	}
	return { board: { formatVersion: board.formatVersion, columns, archiveOrder }, issues }
}

export const readBoardOrdering = (rootPath: string) =>
	readValidated(recordPath(rootPath, "board.json"), agileCodeBoardSchema)
export const readRepositorySettings = (rootPath: string) =>
	readValidated(recordPath(rootPath, "settings.json"), agileCodeRepositorySettingsSchema)
export const writeBoardOrdering = (rootPath: string, board: unknown, options: BoardPersistenceOptions = {}) =>
	replaceValidated(recordPath(rootPath, "board.json"), board, agileCodeBoardSchema, options)
export const writeRepositorySettings = (rootPath: string, settings: unknown, options: BoardPersistenceOptions = {}) =>
	replaceValidated(recordPath(rootPath, "settings.json"), settings, agileCodeRepositorySettingsSchema, options)
