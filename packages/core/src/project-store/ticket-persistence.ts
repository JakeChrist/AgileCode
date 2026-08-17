import { randomUUID } from "crypto"
import { constants } from "fs"
import * as fs from "fs/promises"
import * as path from "path"

import { ticketIdSchema, ticketSchema, type Ticket } from "@roo-code/types"

const LOCK_RETRY_DELAY_MS = 10
const LOCK_RETRIES = 500

export class TicketPersistenceError extends Error {
	constructor(
		message: string,
		readonly reason: "already-exists" | "not-found" | "invalid" | "locked" | "identity-mismatch" | "invalid-state",
		options?: ErrorOptions,
	) {
		super(message, options)
		this.name = "TicketPersistenceError"
	}
}

/** Test seam for simulating an interruption immediately before the atomic replacement. */
export interface TicketPersistenceOptions {
	beforeCommit?: (temporaryPath: string, destinationPath: string) => void | Promise<void>
}

function ticketDirectory(rootPath: string, archived: boolean): string {
	return path.join(rootPath, ".agilecode", archived ? "archive" : "tickets")
}

function ticketPath(rootPath: string, id: string, archived: boolean): string {
	return path.join(ticketDirectory(rootPath, archived), `${id}.json`)
}

function parseTicket(ticket: unknown): Ticket {
	const parsed = ticketSchema.safeParse(ticket)
	if (!parsed.success) {
		throw new TicketPersistenceError(
			`Ticket failed validation: ${parsed.error.issues[0]?.message ?? "unknown validation error"}`,
			"invalid",
		)
	}
	return parsed.data
}

async function exists(filePath: string): Promise<boolean> {
	try {
		await fs.access(filePath)
		return true
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return false
		throw error
	}
}

async function withTicketLock<T>(rootPath: string, id: string, operation: () => Promise<T>): Promise<T> {
	const lockDirectory = path.join(rootPath, ".agilecode", "locks")
	await fs.mkdir(lockDirectory, { recursive: true })
	const lockPath = path.join(lockDirectory, `${id}.lock`)
	let handle: fs.FileHandle | undefined
	for (let attempt = 0; attempt <= LOCK_RETRIES; attempt++) {
		try {
			handle = await fs.open(lockPath, "wx")
			break
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error
			if (attempt === LOCK_RETRIES) {
				throw new TicketPersistenceError(`Ticket ${id} is locked by another writer`, "locked", { cause: error })
			}
			await new Promise((resolve) => setTimeout(resolve, LOCK_RETRY_DELAY_MS))
		}
	}

	try {
		return await operation()
	} finally {
		await handle?.close()
		await fs.unlink(lockPath).catch((error: NodeJS.ErrnoException) => {
			if (error.code !== "ENOENT") throw error
		})
	}
}

async function writeTemporary(destinationPath: string, ticket: Ticket): Promise<string> {
	const temporaryPath = path.join(path.dirname(destinationPath), `.${ticket.id}.${randomUUID()}.tmp`)
	const handle = await fs.open(temporaryPath, "wx", 0o600)
	try {
		await handle.writeFile(`${JSON.stringify(ticket, null, 2)}\n`, "utf8")
		await handle.sync()
	} finally {
		await handle.close()
	}
	return temporaryPath
}

async function commitCreate(destinationPath: string, ticket: Ticket, options: TicketPersistenceOptions): Promise<void> {
	const temporaryPath = await writeTemporary(destinationPath, ticket)
	try {
		await options.beforeCommit?.(temporaryPath, destinationPath)
		try {
			// A hard link publishes the already-complete temporary file without an
			// existence-check race and never replaces an existing record.
			await fs.link(temporaryPath, destinationPath)
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code === "EEXIST") {
				throw new TicketPersistenceError(`Ticket ${ticket.id} already exists`, "already-exists", {
					cause: error,
				})
			}
			throw error
		}
	} finally {
		await fs.unlink(temporaryPath).catch(() => undefined)
	}
}

async function commitReplacement(
	destinationPath: string,
	ticket: Ticket,
	options: TicketPersistenceOptions,
): Promise<void> {
	const temporaryPath = await writeTemporary(destinationPath, ticket)
	const backupPath = `${destinationPath}.${randomUUID()}.bak`
	let backupCreated = false
	try {
		await fs.copyFile(destinationPath, backupPath, constants.COPYFILE_EXCL)
		backupCreated = true
		await options.beforeCommit?.(temporaryPath, destinationPath)
		await fs.rename(temporaryPath, destinationPath)
	} catch (error) {
		// rename is atomic: before it succeeds the original remains visible, and
		// afterwards the complete new record is visible. The backup is retained
		// only as a rollback aid for unusual platform/filesystem failures.
		if (!(await exists(destinationPath)) && backupCreated) await fs.rename(backupPath, destinationPath)
		throw error
	} finally {
		await fs.unlink(temporaryPath).catch(() => undefined)
		if (backupCreated) await fs.unlink(backupPath).catch(() => undefined)
	}
}

/** Create one active ticket without replacing a record with the same stable identity. */
export async function createTicket(
	rootPath: string,
	ticket: unknown,
	options: TicketPersistenceOptions = {},
): Promise<Ticket> {
	const parsed = parseTicket(ticket)
	if (parsed.lifecycle.state === "archived") {
		throw new TicketPersistenceError("A new ticket cannot already be archived", "invalid-state")
	}
	return withTicketLock(rootPath, parsed.id, async () => {
		if (await exists(ticketPath(rootPath, parsed.id, true))) {
			throw new TicketPersistenceError(`Ticket ${parsed.id} already exists in the archive`, "already-exists")
		}
		await commitCreate(ticketPath(rootPath, parsed.id, false), parsed, options)
		return parsed
	})
}

/** Read and validate one ticket from either active or archived records. */
export async function readTicket(rootPath: string, id: string): Promise<Ticket> {
	const parsedId = ticketIdSchema.safeParse(id)
	if (!parsedId.success) throw new TicketPersistenceError(`Invalid ticket identity: ${id}`, "invalid")
	const activePath = ticketPath(rootPath, id, false)
	const archivedPath = ticketPath(rootPath, id, true)
	const sourcePath = (await exists(activePath)) ? activePath : (await exists(archivedPath)) ? archivedPath : undefined
	if (!sourcePath) throw new TicketPersistenceError(`Ticket ${id} was not found`, "not-found")
	let value: unknown
	try {
		value = JSON.parse(await fs.readFile(sourcePath, "utf8"))
	} catch (error) {
		throw new TicketPersistenceError(`Ticket ${id} does not contain valid JSON`, "invalid", { cause: error })
	}
	const ticket = parseTicket(value)
	if (ticket.id !== id)
		throw new TicketPersistenceError(`Ticket identity ${ticket.id} does not match ${id}`, "identity-mismatch")
	return ticket
}

/** Replace one complete active ticket after validating both its identity and resulting state. */
export async function updateTicket(
	rootPath: string,
	id: string,
	ticket: unknown,
	options: TicketPersistenceOptions = {},
): Promise<Ticket> {
	const parsed = parseTicket(ticket)
	if (parsed.id !== id)
		throw new TicketPersistenceError(
			`Ticket identity cannot change from ${id} to ${parsed.id}`,
			"identity-mismatch",
		)
	if (parsed.lifecycle.state === "archived") {
		throw new TicketPersistenceError("Use archiveTicket to archive an active ticket", "invalid-state")
	}
	return withTicketLock(rootPath, id, async () => {
		const destinationPath = ticketPath(rootPath, id, false)
		if (!(await exists(destinationPath)))
			throw new TicketPersistenceError(`Active ticket ${id} was not found`, "not-found")
		await commitReplacement(destinationPath, parsed, options)
		return parsed
	})
}

/** Publish a fully validated archived record, then remove its active counterpart. */
export async function archiveTicket(rootPath: string, id: string, ticket: unknown): Promise<Ticket> {
	const parsed = parseTicket(ticket)
	if (parsed.id !== id)
		throw new TicketPersistenceError(
			`Ticket identity cannot change from ${id} to ${parsed.id}`,
			"identity-mismatch",
		)
	if (parsed.lifecycle.state !== "archived") {
		throw new TicketPersistenceError("Archived ticket must have archived lifecycle metadata", "invalid-state")
	}
	return withTicketLock(rootPath, id, async () => {
		const activePath = ticketPath(rootPath, id, false)
		const archivedPath = ticketPath(rootPath, id, true)
		if (!(await exists(activePath)))
			throw new TicketPersistenceError(`Active ticket ${id} was not found`, "not-found")
		if (await exists(archivedPath))
			throw new TicketPersistenceError(`Ticket ${id} already exists in the archive`, "already-exists")
		const temporaryPath = await writeTemporary(archivedPath, parsed)
		try {
			await fs.link(temporaryPath, archivedPath)
			try {
				await fs.unlink(activePath)
			} catch (error) {
				await fs.unlink(archivedPath)
				throw error
			}
		} finally {
			await fs.unlink(temporaryPath).catch(() => undefined)
		}
		return parsed
	})
}

/** Restore a validated archived record without ever exposing two live copies. */
export async function restoreTicket(rootPath: string, id: string, ticket: unknown): Promise<Ticket> {
	const parsed = parseTicket(ticket)
	if (parsed.id !== id)
		throw new TicketPersistenceError(
			`Ticket identity cannot change from ${id} to ${parsed.id}`,
			"identity-mismatch",
		)
	if (parsed.lifecycle.state === "archived")
		throw new TicketPersistenceError("A restored ticket must have an active workflow state", "invalid-state")
	return withTicketLock(rootPath, id, async () => {
		const activePath = ticketPath(rootPath, id, false)
		const archivedPath = ticketPath(rootPath, id, true)
		if (!(await exists(archivedPath)))
			throw new TicketPersistenceError(`Archived ticket ${id} was not found`, "not-found")
		if (await exists(activePath)) throw new TicketPersistenceError(`Ticket ${id} already exists`, "already-exists")
		const temporaryPath = await writeTemporary(activePath, parsed)
		try {
			await fs.link(temporaryPath, activePath)
			try {
				await fs.unlink(archivedPath)
			} catch (error) {
				await fs.unlink(activePath)
				throw error
			}
		} finally {
			await fs.unlink(temporaryPath).catch(() => undefined)
		}
		return parsed
	})
}

/** Permanently remove an archived record. Eligibility is enforced by the application service. */
export async function deleteArchivedTicket(rootPath: string, id: string): Promise<void> {
	const parsedId = ticketIdSchema.safeParse(id)
	if (!parsedId.success) throw new TicketPersistenceError(`Invalid ticket identity: ${id}`, "invalid")
	await withTicketLock(rootPath, id, async () => {
		const archivedPath = ticketPath(rootPath, id, true)
		if (!(await exists(archivedPath)))
			throw new TicketPersistenceError(`Archived ticket ${id} was not found`, "not-found")
		await fs.unlink(archivedPath)
	})
}
