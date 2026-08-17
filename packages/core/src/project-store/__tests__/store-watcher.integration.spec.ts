import * as fs from "fs/promises"
import * as path from "path"

import type { AgileCodeStoreChange } from "../store-watcher.js"
import { createBoardScope } from "../../board-scope/board-scope-resolver.js"
import { initializeAgileCodeStore } from "../project-store.js"
import { watchAgileCodeStore } from "../store-watcher.js"

describe("project-local store watcher", () => {
	let root: string
	const watchers: Array<{ dispose(): void }> = []

	beforeEach(async () => {
		const testRoot = path.join(process.cwd(), ".tmp")
		await fs.mkdir(testRoot, { recursive: true })
		root = await fs.mkdtemp(path.join(testRoot, "store-watcher-test-"))
		await initializeAgileCodeStore(await createBoardScope("workspace", root))
	})

	afterEach(async () => {
		for (const watcher of watchers.splice(0)) watcher.dispose()
		await fs.rm(root, { recursive: true, force: true })
	})

	function ticket(id: string, title = `Ticket ${id}`) {
		return {
			formatVersion: 1,
			id,
			statementOfWork: {
				title,
				objective: "Observe external changes",
				context: "A second process edits the store",
				requirements: ["Reload safely"],
				deliverables: ["Fresh board state"],
				constraints: ["Keep the last valid state"],
				includedScope: ["Project store"],
				excludedScope: ["Unrelated data"],
				dependencies: [],
				acceptanceCriteria: ["The watcher emits a validated snapshot"],
				validation: ["Integration test"],
			},
			lifecycle: {
				state: "backlog" as const,
				createdAt: "2026-08-17T00:00:00.000Z",
				reviewComments: [],
				blockedReasons: [],
				failedAttempts: [],
			},
			execution: { historyItemIds: [] },
		}
	}

	async function writeTicket(id: string, title?: string): Promise<void> {
		await fs.writeFile(
			path.join(root, ".agilecode", "tickets", `${id}.json`),
			`${JSON.stringify(ticket(id, title))}\n`,
		)
	}

	async function waitFor(predicate: () => boolean): Promise<void> {
		const timeout = Date.now() + 3_000
		while (!predicate()) {
			if (Date.now() > timeout) throw new Error("Timed out waiting for a store watcher event")
			await new Promise((resolve) => setTimeout(resolve, 10))
		}
	}

	it("coalesces a rapid multi-record update into one consistent snapshot", async () => {
		const changes: AgileCodeStoreChange[] = []
		const watcher = await watchAgileCodeStore(root, {
			debounceMs: 60,
			onDidChange: (change) => {
				changes.push(change)
			},
		})
		watchers.push(watcher)

		await Promise.all([writeTicket("AC-101"), writeTicket("AC-102")])
		const settingsPath = path.join(root, ".agilecode", "settings.json")
		const settings = JSON.parse(await fs.readFile(settingsPath, "utf8"))
		settings.showArchived = true
		await fs.writeFile(settingsPath, `${JSON.stringify(settings)}\n`)

		await waitFor(() => changes.length > 0)
		await new Promise((resolve) => setTimeout(resolve, 150))
		expect(changes).toHaveLength(1)
		expect(changes[0]!.store.activeTickets.map(({ id }) => id)).toEqual(["AC-101", "AC-102"])
		expect(changes[0]!.store.board.columns.backlog).toEqual(["AC-101", "AC-102"])
		expect(changes[0]!.store.settings.showArchived).toBe(true)
	})

	it("reports malformed edits while retaining a ticket until a valid edit arrives", async () => {
		await writeTicket("AC-201", "Original")
		const initialEvents: AgileCodeStoreChange[] = []
		const initial = await watchAgileCodeStore(root, {
			debounceMs: 20,
			onDidChange: (change) => {
				initialEvents.push(change)
			},
		})
		watchers.push(initial)
		const ticketPath = path.join(root, ".agilecode", "tickets", "AC-201.json")

		await fs.writeFile(ticketPath, "{invalid json\n")
		await waitFor(() => initialEvents.length === 1)
		expect(initialEvents[0]!.store.activeTickets[0]!.statementOfWork.title).toBe("Original")
		expect(initialEvents[0]!.diagnostics).toContainEqual(
			expect.objectContaining({ record: "tickets/AC-201.json", kind: "malformed" }),
		)

		await writeTicket("AC-201", "Externally updated")
		await waitFor(() => initialEvents.length === 2)
		expect(initialEvents[1]!.store.activeTickets[0]!.statementOfWork.title).toBe("Externally updated")
	})

	it("reconciles a deletion without removing unrelated tickets", async () => {
		await Promise.all([writeTicket("AC-301"), writeTicket("AC-302")])
		const changes: AgileCodeStoreChange[] = []
		const watcher = await watchAgileCodeStore(root, {
			debounceMs: 20,
			onDidChange: (change) => {
				changes.push(change)
			},
		})
		watchers.push(watcher)

		await fs.rm(path.join(root, ".agilecode", "tickets", "AC-301.json"))
		await waitFor(() => changes.length === 1)
		expect(changes[0]!.store.activeTickets.map(({ id }) => id)).toEqual(["AC-302"])
		expect(changes[0]!.store.board.columns.backlog).toEqual(["AC-302"])
	})

	it("stops emitting after disposal", async () => {
		const changes: AgileCodeStoreChange[] = []
		const watcher = await watchAgileCodeStore(root, {
			debounceMs: 20,
			onDidChange: (change) => {
				changes.push(change)
			},
		})
		watcher.dispose()

		await writeTicket("AC-401")
		await new Promise((resolve) => setTimeout(resolve, 100))
		expect(changes).toEqual([])
	})
})
