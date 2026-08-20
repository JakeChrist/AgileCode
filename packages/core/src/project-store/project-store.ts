import { randomUUID } from "crypto"
import type { Dirent } from "fs"
import * as fs from "fs/promises"
import * as path from "path"

import {
	AGILECODE_STORE_FORMAT_VERSION,
	agileCodeBoardSchema,
	agileCodeProjectStoreSchema,
	agileCodeRepositorySettingsSchema,
	agileCodeStoreManifestSchema,
	ticketSchema,
	type AgileCodeProjectStore,
	type AgileCodeBoard,
	type AgileCodeRepositorySettings,
	type BoardScope,
	type Ticket,
} from "@roo-code/types"
import type { output, ZodTypeAny } from "zod"

import { reconcileBoardOrdering } from "./board-persistence.js"

const STORE_DIRECTORY = ".agilecode"

export class AgileCodeStoreInitializationError extends Error {
	constructor(
		message: string,
		readonly reason: "partial" | "malformed" | "unsupported-version",
		options?: ErrorOptions,
	) {
		super(message, options)
		this.name = "AgileCodeStoreInitializationError"
	}
}

export interface AgileCodeStoreInitializationResult {
	created: boolean
	path: string
	store: AgileCodeProjectStore
	diagnostics: AgileCodeStoreDiagnostic[]
}

export interface AgileCodeStoreDiagnostic {
	record: string
	problem: string
	kind: "malformed" | "unsupported-version" | "reconciled"
}

export interface AgileCodeStoreLoadResult {
	store: AgileCodeProjectStore
	diagnostics: AgileCodeStoreDiagnostic[]
}

function json(value: unknown): string {
	return `${JSON.stringify(value, null, 2)}\n`
}

async function readRecord<Schema extends ZodTypeAny>(filePath: string, schema: Schema): Promise<output<Schema>> {
	let source: string
	try {
		source = await fs.readFile(filePath, "utf8")
	} catch (error) {
		const code = (error as NodeJS.ErrnoException).code
		if (code === "ENOENT") {
			throw new AgileCodeStoreInitializationError(
				`AgileCode store is partial: required entry ${path.basename(filePath)} is missing`,
				"partial",
				{ cause: error },
			)
		}
		if (code === "EISDIR" || code === "ENOTDIR") {
			throw new AgileCodeStoreInitializationError(
				`AgileCode store is malformed: ${path.basename(filePath)} is not a regular file`,
				"malformed",
				{ cause: error },
			)
		}
		throw error
	}

	let value: unknown
	try {
		value = JSON.parse(source)
	} catch (error) {
		throw new AgileCodeStoreInitializationError(
			`AgileCode store is malformed: ${path.basename(filePath)} is not valid JSON`,
			"malformed",
			{ cause: error },
		)
	}

	if (
		typeof value === "object" &&
		value !== null &&
		"formatVersion" in value &&
		typeof value.formatVersion === "number" &&
		value.formatVersion > AGILECODE_STORE_FORMAT_VERSION
	) {
		throw new AgileCodeStoreInitializationError(
			`AgileCode store uses unsupported format version ${value.formatVersion} in ${path.basename(filePath)}; this version supports ${AGILECODE_STORE_FORMAT_VERSION}`,
			"unsupported-version",
		)
	}

	const parsed = schema.safeParse(value)
	if (!parsed.success) {
		throw new AgileCodeStoreInitializationError(
			`AgileCode store is malformed: ${path.basename(filePath)} failed validation: ${parsed.error.issues[0]?.message ?? "unknown validation error"}`,
			"malformed",
		)
	}
	return parsed.data
}

async function readTickets(
	directory: string,
	diagnostics: AgileCodeStoreDiagnostic[],
	archived: boolean,
): Promise<Ticket[]> {
	let entries: Dirent[]
	try {
		entries = await fs.readdir(directory, { withFileTypes: true })
	} catch (error) {
		const code = (error as NodeJS.ErrnoException).code
		if (code === "ENOENT") {
			throw new AgileCodeStoreInitializationError(
				`AgileCode store is partial: required directory ${path.basename(directory)} is missing`,
				"partial",
				{ cause: error },
			)
		}
		if (code === "ENOTDIR") {
			throw new AgileCodeStoreInitializationError(
				`AgileCode store is malformed: ${path.basename(directory)} is not a directory`,
				"malformed",
				{ cause: error },
			)
		}
		throw error
	}

	const tickets: Ticket[] = []
	for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
		if (!entry.isFile() || path.extname(entry.name) !== ".json") {
			diagnostics.push({
				record: path.join(path.basename(directory), entry.name),
				problem: "Unexpected entry; only regular JSON ticket files are loaded",
				kind: "malformed",
			})
			continue
		}
		let ticket: Ticket
		try {
			ticket = await readRecord(path.join(directory, entry.name), ticketSchema)
		} catch (error) {
			if (!(error instanceof AgileCodeStoreInitializationError)) throw error
			diagnostics.push({
				record: path.join(path.basename(directory), entry.name),
				problem: error.message,
				kind: error.reason === "unsupported-version" ? "unsupported-version" : "malformed",
			})
			continue
		}
		const expectedPrefix = `${ticket.id}-`
		if (entry.name !== `${ticket.id}.json` && !entry.name.startsWith(expectedPrefix)) {
			diagnostics.push({
				record: path.join(path.basename(directory), entry.name),
				problem: `File name does not match ticket id ${ticket.id}`,
				kind: "malformed",
			})
			continue
		}
		if ((ticket.lifecycle.state === "archived") !== archived) {
			diagnostics.push({
				record: path.join(path.basename(directory), entry.name),
				problem: `Ticket ${ticket.id} is stored in the wrong directory for its workflow state`,
				kind: "malformed",
			})
			continue
		}
		tickets.push(ticket)
	}
	return tickets
}

const emptyBoard = (): AgileCodeBoard => ({
	formatVersion: AGILECODE_STORE_FORMAT_VERSION,
	columns: { backlog: [], ready: [], in_progress: [], blocked: [], review: [], done: [] },
	archiveOrder: [],
})

const defaultSettings = (): AgileCodeRepositorySettings => ({
	formatVersion: AGILECODE_STORE_FORMAT_VERSION,
	automaticArchival: { enabled: false, retentionDays: 30 },
	repositorySelection: { preferredScopeId: null },
	showArchived: false,
	suppressDragToExecuteWarning: false,
	workflowPreferences: {},
})

async function recoverRecord<T>(
	filePath: string,
	schema: ZodTypeAny,
	fallback: T,
	diagnostics: AgileCodeStoreDiagnostic[],
): Promise<T> {
	try {
		return (await readRecord(filePath, schema)) as T
	} catch (error) {
		if (!(error instanceof AgileCodeStoreInitializationError) || error.reason === "partial") throw error
		diagnostics.push({ record: path.basename(filePath), problem: error.message, kind: error.reason })
		return fallback
	}
}

/** Loads every independently valid record and reports non-destructive recovery decisions. */
export async function loadAgileCodeStore(rootPath: string): Promise<AgileCodeStoreLoadResult> {
	const directory = path.join(rootPath, STORE_DIRECTORY)
	const diagnostics: AgileCodeStoreDiagnostic[] = []
	const manifest = await readRecord(path.join(directory, "store.json"), agileCodeStoreManifestSchema)
	const activeTickets = await readTickets(path.join(directory, "tickets"), diagnostics, false)
	const archivedTickets = await readTickets(path.join(directory, "archive"), diagnostics, true)
	const seen = new Set<string>()
	for (const records of [activeTickets, archivedTickets]) {
		for (let index = records.length - 1; index >= 0; index--) {
			const ticket = records[index]!
			if (!seen.has(ticket.id)) seen.add(ticket.id)
			else {
				records.splice(index, 1)
				diagnostics.push({
					record: `${ticket.lifecycle.state === "archived" ? "archive" : "tickets"}/${ticket.id}.json`,
					problem: `Ticket ${ticket.id} has more than one record; this copy was excluded`,
					kind: "malformed",
				})
			}
		}
	}
	const board = await recoverRecord(
		path.join(directory, "board.json"),
		agileCodeBoardSchema,
		emptyBoard(),
		diagnostics,
	)
	const reconciled = reconcileBoardOrdering(board, activeTickets, archivedTickets)
	for (const issue of reconciled.issues)
		diagnostics.push({
			record: "board.json",
			problem: `${issue.kind} for ${issue.id} at ${issue.location}`,
			kind: "reconciled",
		})
	const store = {
		manifest,
		board: reconciled.board,
		settings: await recoverRecord(
			path.join(directory, "settings.json"),
			agileCodeRepositorySettingsSchema,
			defaultSettings(),
			diagnostics,
		),
		activeTickets,
		archivedTickets,
	}
	const parsed = agileCodeProjectStoreSchema.safeParse(store)
	if (!parsed.success) {
		throw new AgileCodeStoreInitializationError(
			`AgileCode store is malformed: records are inconsistent: ${parsed.error.issues[0]?.message ?? "unknown validation error"}`,
			"malformed",
		)
	}
	return { store: parsed.data, diagnostics }
}

/** Compatibility helper for callers that only need the recovered store. */
export async function readAgileCodeStore(rootPath: string): Promise<AgileCodeProjectStore> {
	return (await loadAgileCodeStore(rootPath)).store
}

async function writeEmptyStore(directory: string, scope: BoardScope): Promise<void> {
	await fs.mkdir(path.join(directory, "tickets"), { recursive: true })
	await fs.mkdir(path.join(directory, "archive"), { recursive: true })
	await Promise.all([
		fs.writeFile(path.join(directory, "store.json"), json({ formatVersion: 1, scope }), { flag: "wx" }),
		fs.writeFile(
			path.join(directory, "board.json"),
			json({
				formatVersion: 1,
				columns: { backlog: [], ready: [], in_progress: [], blocked: [], review: [], done: [] },
				archiveOrder: [],
			}),
			{ flag: "wx" },
		),
		fs.writeFile(
			path.join(directory, "settings.json"),
			json({
				formatVersion: 1,
				automaticArchival: { enabled: false, retentionDays: 30 },
				repositorySelection: { preferredScopeId: null },
				showArchived: false,
				suppressDragToExecuteWarning: false,
				workflowPreferences: {},
			}),
			{ flag: "wx" },
		),
	])
}

/** Creates the minimum store atomically, or validates and preserves the store already present. */
export async function initializeAgileCodeStore(scope: BoardScope): Promise<AgileCodeStoreInitializationResult> {
	const directory = path.join(scope.rootPath, STORE_DIRECTORY)
	try {
		await fs.lstat(directory)
		const loaded = await loadAgileCodeStore(scope.rootPath)
		return { created: false, path: directory, ...loaded }
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error
	}

	const staging = path.join(scope.rootPath, `.agilecode-initialize-${randomUUID()}`)
	await fs.mkdir(staging)
	try {
		await writeEmptyStore(staging, scope)
		try {
			await fs.rename(staging, directory)
		} catch (error) {
			if (
				(error as NodeJS.ErrnoException).code !== "EEXIST" &&
				(error as NodeJS.ErrnoException).code !== "ENOTEMPTY"
			) {
				throw error
			}
			const loaded = await loadAgileCodeStore(scope.rootPath)
			return { created: false, path: directory, ...loaded }
		}
		const loaded = await loadAgileCodeStore(scope.rootPath)
		return { created: true, path: directory, ...loaded }
	} finally {
		await fs.rm(staging, { recursive: true, force: true })
	}
}
