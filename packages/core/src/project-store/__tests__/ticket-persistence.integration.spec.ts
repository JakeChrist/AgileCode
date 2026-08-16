import * as fs from "fs/promises"
import * as path from "path"

import type { Ticket } from "@roo-code/types"

import { createBoardScope } from "../../board-scope/board-scope-resolver.js"
import { initializeAgileCodeStore } from "../project-store.js"
import { archiveTicket, createTicket, readTicket, updateTicket } from "../ticket-persistence.js"

describe("individual ticket persistence", () => {
	let root: string

	beforeEach(async () => {
		const testRoot = path.join(process.cwd(), ".tmp")
		await fs.mkdir(testRoot, { recursive: true })
		root = await fs.mkdtemp(path.join(testRoot, "ticket-persistence-test-"))
		await initializeAgileCodeStore(await createBoardScope("workspace", root))
	})

	afterEach(async () => fs.rm(root, { recursive: true, force: true }))

	function ticket(id: string, title = `Ticket ${id}`): Ticket {
		return {
			formatVersion: 1,
			id,
			statementOfWork: {
				title,
				objective: "Persist every field",
				context: "Project-local ticket storage",
				requirements: ["Keep complete records"],
				deliverables: ["A durable record"],
				constraints: ["Remain in the repository"],
				includedScope: ["Ticket persistence"],
				excludedScope: ["Unrelated services"],
				dependencies: ["AC-005"],
				acceptanceCriteria: ["Round trips exactly"],
				validation: ["Integration tests"],
			},
			lifecycle: {
				state: "backlog",
				createdAt: "2026-08-14T12:00:00.000Z",
				reviewComments: [],
				blockedReasons: [],
				failedAttempts: [],
			},
			execution: { historyItemIds: ["history-1"] },
		}
	}

	it("round trips every field and rejects a colliding create", async () => {
		const record = ticket("AC-006")
		expect(await createTicket(root, record)).toEqual(record)
		expect(await readTicket(root, record.id)).toEqual(record)
		await expect(createTicket(root, record)).rejects.toMatchObject({ reason: "already-exists" })
	})

	it("validates the complete update and stable identity before replacing the record", async () => {
		const original = ticket("AC-006")
		await createTicket(root, original)
		await expect(updateTicket(root, original.id, { ...original, execution: {} })).rejects.toMatchObject({
			reason: "invalid",
		})
		await expect(updateTicket(root, original.id, { ...original, id: "AC-007" })).rejects.toMatchObject({
			reason: "identity-mismatch",
		})
		expect(await readTicket(root, original.id)).toEqual(original)
	})

	it("leaves the old complete JSON visible when replacement is interrupted", async () => {
		const original = ticket("AC-006", "Original")
		const replacement = ticket("AC-006", "Replacement")
		await createTicket(root, original)

		await expect(
			updateTicket(root, original.id, replacement, {
				beforeCommit: async (temporaryPath) => {
					expect(JSON.parse(await fs.readFile(temporaryPath, "utf8"))).toEqual(replacement)
					throw new Error("simulated interruption")
				},
			}),
		).rejects.toThrow("simulated interruption")

		expect(await readTicket(root, original.id)).toEqual(original)
		const entries = await fs.readdir(path.join(root, ".agilecode", "tickets"))
		expect(entries).toEqual(["AC-006.json"])
	})

	it("serializes concurrent updates to the same record without malformed content", async () => {
		await createTicket(root, ticket("AC-006", "Original"))
		let releaseFirst!: () => void
		const firstMayCommit = new Promise<void>((resolve) => (releaseFirst = resolve))
		let firstHasLock!: () => void
		const firstLocked = new Promise<void>((resolve) => (firstHasLock = resolve))

		const first = updateTicket(root, "AC-006", ticket("AC-006", "First"), {
			beforeCommit: async () => {
				firstHasLock()
				await firstMayCommit
			},
		})
		await firstLocked
		const second = updateTicket(root, "AC-006", ticket("AC-006", "Second"))
		releaseFirst()
		await Promise.all([first, second])

		expect((await readTicket(root, "AC-006")).statementOfWork.title).toBe("Second")
		expect(
			JSON.parse(await fs.readFile(path.join(root, ".agilecode", "tickets", "AC-006.json"), "utf8")),
		).toBeTruthy()
	})

	it("updates one ticket without changing any unrelated ticket file", async () => {
		await createTicket(root, ticket("AC-006", "First"))
		await createTicket(root, ticket("AC-007", "Unrelated"))
		const unrelatedPath = path.join(root, ".agilecode", "tickets", "AC-007.json")
		const before = await fs.readFile(unrelatedPath)
		const beforeStat = await fs.stat(unrelatedPath)

		await updateTicket(root, "AC-006", ticket("AC-006", "Updated"))

		expect(await fs.readFile(unrelatedPath)).toEqual(before)
		expect((await fs.stat(unrelatedPath)).ino).toBe(beforeStat.ino)
	})

	it("archives a complete record without touching another ticket", async () => {
		const active = ticket("AC-006")
		await createTicket(root, active)
		await createTicket(root, ticket("AC-007", "Unrelated"))
		const unrelatedPath = path.join(root, ".agilecode", "tickets", "AC-007.json")
		const unrelated = await fs.readFile(unrelatedPath)
		const archived: Ticket = {
			...active,
			lifecycle: {
				...active.lifecycle,
				state: "archived",
				archivedAt: "2026-08-16T12:00:00.000Z",
				archivedFrom: "backlog",
			},
		}

		expect(await archiveTicket(root, active.id, archived)).toEqual(archived)
		expect(await readTicket(root, active.id)).toEqual(archived)
		await expect(fs.access(path.join(root, ".agilecode", "tickets", "AC-006.json"))).rejects.toMatchObject({
			code: "ENOENT",
		})
		expect(await fs.readFile(unrelatedPath)).toEqual(unrelated)
	})
})
