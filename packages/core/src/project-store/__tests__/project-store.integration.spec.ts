import * as fs from "fs/promises"
import * as path from "path"

import { createBoardScope } from "../../board-scope/board-scope-resolver.js"
import { initializeAgileCodeStore, loadAgileCodeStore } from "../project-store.js"

describe("project-local store initialization", () => {
	let root: string

	beforeEach(async () => {
		const testRoot = path.join(process.cwd(), ".tmp")
		await fs.mkdir(testRoot, { recursive: true })
		root = await fs.mkdtemp(path.join(testRoot, "project-store-test-"))
	})

	afterEach(async () => fs.rm(root, { recursive: true, force: true }))

	async function initialize() {
		return initializeAgileCodeStore(await createBoardScope("workspace", root))
	}

	async function snapshot(): Promise<Map<string, string>> {
		const values = new Map<string, string>()
		async function visit(directory: string): Promise<void> {
			for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
				const absolute = path.join(directory, entry.name)
				const relative = path.relative(root, absolute)
				if (entry.isDirectory()) {
					values.set(`${relative}/`, "directory")
					await visit(absolute)
				} else values.set(relative, await fs.readFile(absolute, "utf8"))
			}
		}
		await visit(root)
		return values
	}

	it("creates a valid empty store and does not create or change .gitignore", async () => {
		const ignore = "node_modules/\n"
		await fs.writeFile(path.join(root, ".gitignore"), ignore)

		const result = await initialize()

		expect(result.created).toBe(true)
		expect(result.store.activeTickets).toEqual([])
		expect(result.store.archivedTickets).toEqual([])
		expect(await fs.readdir(path.join(root, ".agilecode"))).toEqual([
			"archive",
			"board.json",
			"settings.json",
			"store.json",
			"tickets",
		])
		expect(await fs.readFile(path.join(root, ".gitignore"), "utf8")).toBe(ignore)
	})

	it("is idempotent and leaves an existing valid store byte-for-byte unchanged", async () => {
		await initialize()
		const before = await snapshot()

		const result = await initialize()

		expect(result.created).toBe(false)
		expect(await snapshot()).toEqual(before)
	})

	it.each([
		["partial", async () => fs.mkdir(path.join(root, ".agilecode"))],
		[
			"unsupported-version",
			async () => {
				await initialize()
				await fs.writeFile(path.join(root, ".agilecode", "store.json"), '{"formatVersion":2}\n')
			},
		],
	] as const)("reports and preserves a %s store", async (reason, arrange) => {
		await arrange()
		const before = await snapshot()

		const operation = initialize()
		await expect(operation).rejects.toMatchObject({ reason })
		expect(await snapshot()).toEqual(before)
	})

	function ticket(id: string, state: "backlog" | "ready" = "backlog") {
		return {
			formatVersion: 1,
			id,
			statementOfWork: {
				title: `Ticket ${id}`,
				objective: "Validate recovery",
				context: "Mixed store",
				requirements: ["Load valid records"],
				deliverables: ["Recovered board"],
				constraints: ["Preserve files"],
				includedScope: ["Store loading"],
				excludedScope: ["Destructive repair"],
				dependencies: [],
				acceptanceCriteria: ["Works"],
				validation: ["Integration tests"],
			},
			lifecycle: {
				state,
				createdAt: "2026-08-17T00:00:00.000Z",
				reviewComments: [],
				blockedReasons: [],
				failedAttempts: [],
			},
			execution: { historyItemIds: [] },
		}
	}

	it("loads valid tickets while isolating malformed JSON, invalid schemas, and unsupported versions", async () => {
		await initialize()
		const directory = path.join(root, ".agilecode", "tickets")
		await fs.writeFile(path.join(directory, "AC-001.json"), `${JSON.stringify(ticket("AC-001"))}\n`)
		await fs.writeFile(path.join(directory, "AC-002.json"), "{not json\n")
		await fs.writeFile(path.join(directory, "AC-003.json"), '{"formatVersion":1,"id":"AC-003"}\n')
		await fs.writeFile(
			path.join(directory, "AC-004.json"),
			`${JSON.stringify({ ...ticket("AC-004"), formatVersion: 2 })}\n`,
		)

		const before = await snapshot()
		const result = await loadAgileCodeStore(root)

		expect(result.store.activeTickets.map(({ id }) => id)).toEqual(["AC-001"])
		expect(result.store.board.columns.backlog).toEqual(["AC-001"])
		expect(result.diagnostics).toEqual(
			expect.arrayContaining([
				expect.objectContaining({
					record: "tickets/AC-002.json",
					kind: "malformed",
					problem: expect.stringContaining("not valid JSON"),
				}),
				expect.objectContaining({
					record: "tickets/AC-003.json",
					kind: "malformed",
					problem: expect.stringContaining("failed validation"),
				}),
				expect.objectContaining({
					record: "tickets/AC-004.json",
					kind: "unsupported-version",
					problem: expect.stringContaining("version 2"),
				}),
				expect.objectContaining({
					record: "board.json",
					kind: "reconciled",
					problem: expect.stringContaining("AC-001"),
				}),
			]),
		)
		expect(await snapshot()).toEqual(before)
	})

	it("recovers deterministically from malformed ordering without deleting ticket files", async () => {
		await initialize()
		const directory = path.join(root, ".agilecode")
		await fs.writeFile(
			path.join(directory, "tickets", "AC-020.json"),
			`${JSON.stringify(ticket("AC-020", "ready"))}\n`,
		)
		await fs.writeFile(
			path.join(directory, "tickets", "AC-010.json"),
			`${JSON.stringify(ticket("AC-010", "ready"))}\n`,
		)
		await fs.writeFile(path.join(directory, "board.json"), "{not json\n")
		const before = await snapshot()

		const result = await loadAgileCodeStore(root)

		expect(result.store.board.columns.ready).toEqual(["AC-010", "AC-020"])
		expect(result.diagnostics[0]).toMatchObject({ record: "board.json", kind: "malformed" })
		expect(await snapshot()).toEqual(before)
	})

	it("drops orphan ordering references in memory and reports the affected identifier", async () => {
		await initialize()
		const boardPath = path.join(root, ".agilecode", "board.json")
		const board = JSON.parse(await fs.readFile(boardPath, "utf8"))
		board.columns.backlog.push("AC-999")
		await fs.writeFile(boardPath, `${JSON.stringify(board)}\n`)

		const result = await loadAgileCodeStore(root)

		expect(result.store.board.columns.backlog).toEqual([])
		expect(result.diagnostics).toContainEqual({
			record: "board.json",
			kind: "reconciled",
			problem: "unknown-active-reference for AC-999 at backlog",
		})
	})

	it("isolates malformed settings and preserves the record for explicit recovery", async () => {
		await initialize()
		const settingsPath = path.join(root, ".agilecode", "settings.json")
		await fs.writeFile(settingsPath, '{"formatVersion":2,"private":"do not report me"}\n')
		const before = await fs.readFile(settingsPath, "utf8")

		const result = await loadAgileCodeStore(root)

		expect(result.store.settings.formatVersion).toBe(1)
		expect(result.diagnostics).toEqual([
			expect.objectContaining({
				record: "settings.json",
				kind: "unsupported-version",
				problem: expect.not.stringContaining("private"),
			}),
		])
		expect(await fs.readFile(settingsPath, "utf8")).toBe(before)
	})

	it("does not create .gitignore when it is absent", async () => {
		await initialize()
		await expect(fs.stat(path.join(root, ".gitignore"))).rejects.toMatchObject({ code: "ENOENT" })
	})
})
