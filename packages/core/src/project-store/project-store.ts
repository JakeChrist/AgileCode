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
	type BoardScope,
	type Ticket,
} from "@roo-code/types"
import type { output, ZodTypeAny } from "zod"

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

async function readTickets(directory: string): Promise<Ticket[]> {
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
			throw new AgileCodeStoreInitializationError(
				`AgileCode store is malformed: unexpected entry ${path.join(path.basename(directory), entry.name)}`,
				"malformed",
			)
		}
		const ticket = await readRecord(path.join(directory, entry.name), ticketSchema)
		if (entry.name !== `${ticket.id}.json`) {
			throw new AgileCodeStoreInitializationError(
				`AgileCode store is malformed: ticket file ${entry.name} does not match ticket id ${ticket.id}`,
				"malformed",
			)
		}
		tickets.push(ticket)
	}
	return tickets
}

/** Reads and fully validates an existing project-local store without modifying it. */
export async function readAgileCodeStore(rootPath: string): Promise<AgileCodeProjectStore> {
	const directory = path.join(rootPath, STORE_DIRECTORY)
	const store = {
		manifest: await readRecord(path.join(directory, "store.json"), agileCodeStoreManifestSchema),
		board: await readRecord(path.join(directory, "board.json"), agileCodeBoardSchema),
		settings: await readRecord(path.join(directory, "settings.json"), agileCodeRepositorySettingsSchema),
		activeTickets: await readTickets(path.join(directory, "tickets")),
		archivedTickets: await readTickets(path.join(directory, "archive")),
	}
	const parsed = agileCodeProjectStoreSchema.safeParse(store)
	if (!parsed.success) {
		throw new AgileCodeStoreInitializationError(
			`AgileCode store is malformed: records are inconsistent: ${parsed.error.issues[0]?.message ?? "unknown validation error"}`,
			"malformed",
		)
	}
	return parsed.data
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
			}),
			{ flag: "wx" },
		),
		fs.writeFile(path.join(directory, "settings.json"), json({ formatVersion: 1 }), { flag: "wx" }),
	])
}

/** Creates the minimum store atomically, or validates and preserves the store already present. */
export async function initializeAgileCodeStore(scope: BoardScope): Promise<AgileCodeStoreInitializationResult> {
	const directory = path.join(scope.rootPath, STORE_DIRECTORY)
	try {
		await fs.lstat(directory)
		return { created: false, path: directory, store: await readAgileCodeStore(scope.rootPath) }
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
			return { created: false, path: directory, store: await readAgileCodeStore(scope.rootPath) }
		}
		return { created: true, path: directory, store: await readAgileCodeStore(scope.rootPath) }
	} finally {
		await fs.rm(staging, { recursive: true, force: true })
	}
}
