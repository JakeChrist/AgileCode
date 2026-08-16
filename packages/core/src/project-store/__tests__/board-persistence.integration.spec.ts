import * as fs from "fs/promises"
import * as path from "path"

import type { AgileCodeBoard, AgileCodeRepositorySettings, Ticket } from "@roo-code/types"

import { createBoardScope } from "../../board-scope/board-scope-resolver.js"
import {
	readBoardOrdering,
	readRepositorySettings,
	reconcileBoardOrdering,
	writeBoardOrdering,
	writeRepositorySettings,
} from "../board-persistence.js"
import { initializeAgileCodeStore } from "../project-store.js"

describe("board record persistence", () => {
	let root: string

	beforeEach(async () => {
		const testRoot = path.join(process.cwd(), ".tmp")
		await fs.mkdir(testRoot, { recursive: true })
		root = await fs.mkdtemp(path.join(testRoot, "board-persistence-test-"))
		await initializeAgileCodeStore(await createBoardScope("workspace", root))
	})

	afterEach(async () => fs.rm(root, { recursive: true, force: true }))

	const board = (): AgileCodeBoard => ({
		formatVersion: 1,
		columns: { backlog: ["AC-007"], ready: [], in_progress: [], blocked: [], review: [], done: [] },
		archiveOrder: ["AC-001"],
	})

	const settings = (): AgileCodeRepositorySettings => ({
		formatVersion: 1,
		automaticArchival: { enabled: true, retentionDays: 45 },
		repositorySelection: { preferredScopeId: `git:${"b".repeat(64)}` },
		showArchived: true,
		suppressDragToExecuteWarning: true,
		workflowPreferences: { requireReview: true, branchPrefix: "ticket/" },
	})

	it("round trips ordering and reversible repository settings across a reload", async () => {
		await writeBoardOrdering(root, board())
		await writeRepositorySettings(root, settings())

		expect(await readBoardOrdering(root)).toEqual(board())
		expect(await readRepositorySettings(root)).toEqual(settings())
		await writeRepositorySettings(root, { ...settings(), suppressDragToExecuteWarning: false })
		expect((await readRepositorySettings(root)).suppressDragToExecuteWarning).toBe(false)
	})

	it("does not touch ticket records when order changes", async () => {
		const ticketPath = path.join(root, ".agilecode", "tickets", "AC-007.json")
		await fs.writeFile(ticketPath, '{"statementOfWork":"untouched"}\n')
		const before = await fs.stat(ticketPath)

		await writeBoardOrdering(root, board())

		expect(await fs.readFile(ticketPath, "utf8")).toBe('{"statementOfWork":"untouched"}\n')
		expect((await fs.stat(ticketPath)).ino).toBe(before.ino)
	})

	it("preserves the last valid board when replacement is interrupted", async () => {
		const original = await readBoardOrdering(root)
		await expect(
			writeBoardOrdering(root, board(), {
				beforeCommit: () => {
					throw new Error("injected write failure")
				},
			}),
		).rejects.toThrow("injected write failure")

		expect(await readBoardOrdering(root)).toEqual(original)
		expect((await fs.readdir(path.join(root, ".agilecode"))).filter((name) => name.endsWith(".tmp"))).toEqual([])
	})

	it("reconciles unknown, missing, and misplaced references deterministically", () => {
		const ticket = (id: string, state: "backlog" | "ready" | "archived"): Ticket => ({
			formatVersion: 1,
			id,
			statementOfWork: {
				title: id,
				objective: "test",
				context: "test",
				requirements: [],
				constraints: [],
				includedScope: [],
				dependencies: [],
				acceptanceCriteria: [],
				validation: [],
			},
			lifecycle:
				state === "archived"
					? {
							state,
							createdAt: "2026-08-14T00:00:00.000Z",
							archivedAt: "2026-08-15T00:00:00.000Z",
							archivedFrom: "done",
							reviewComments: [],
							blockedReasons: [],
							failedAttempts: [],
						}
					: {
							state,
							createdAt: "2026-08-14T00:00:00.000Z",
							reviewComments: [],
							blockedReasons: [],
							failedAttempts: [],
						},
			execution: { historyItemIds: [] },
		})
		const ordering = board()
		ordering.columns.backlog = ["UNKNOWN", "AC-008"]
		ordering.archiveOrder = ["OLD"]

		const result = reconcileBoardOrdering(
			ordering,
			[ticket("AC-009", "backlog"), ticket("AC-008", "ready")],
			[ticket("AC-002", "archived")],
		)

		expect(result.board.columns).toEqual({
			backlog: ["AC-009"],
			ready: ["AC-008"],
			in_progress: [],
			blocked: [],
			review: [],
			done: [],
		})
		expect(result.board.archiveOrder).toEqual(["AC-002"])
		expect(result.issues).toEqual([
			{ id: "UNKNOWN", kind: "unknown-active-reference", location: "backlog" },
			{ id: "AC-008", kind: "state-mismatch", location: "backlog->ready" },
			{ id: "AC-009", kind: "missing-active-reference", location: "backlog" },
			{ id: "OLD", kind: "unknown-archive-reference", location: "archiveOrder" },
			{ id: "AC-002", kind: "missing-archive-reference", location: "archiveOrder" },
		])
	})
})
