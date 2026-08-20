import * as fs from "fs/promises"
import * as os from "os"
import * as path from "path"

import { AGILECODE_STORE_FORMAT_VERSION, TICKET_FORMAT_VERSION, type BoardScope, type Ticket } from "@roo-code/types"
import { afterEach, describe, expect, it, vi } from "vitest"

import { initializeAgileCodeStore } from "../project-store.js"
import { RepositoryBoardService } from "../board-service.js"

const roots: string[] = []
const identity = (character: string) => `workspace:${character.repeat(64)}` as const

async function scope(character: string): Promise<BoardScope> {
	const rootPath = await fs.mkdtemp(path.join(os.tmpdir(), "board-service-"))
	roots.push(rootPath)
	return { id: identity(character), kind: "workspace", rootPath }
}

function ticket(id: string, state: Ticket["lifecycle"]["state"] = "backlog"): Ticket {
	return {
		formatVersion: TICKET_FORMAT_VERSION,
		id,
		statementOfWork: {
			title: `Ticket ${id}`,
			objective: "Prove the repository service contract",
			context: "Application service integration test",
			requirements: ["Use one authoritative service"],
			constraints: ["Remain repository local"],
			includedScope: ["Board operations"],
			dependencies: [],
			acceptanceCriteria: ["The operation succeeds consistently"],
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

afterEach(async () => {
	await Promise.all(roots.splice(0).map((root) => fs.rm(root, { recursive: true, force: true })))
})

describe("RepositoryBoardService", () => {
	it("allocates stable identities and storage names and rejects forced collisions", async () => {
		const repository = await scope("9")
		await initializeAgileCodeStore(repository)
		const service = await RepositoryBoardService.create(repository, {
			watch: false,
			now: () => new Date("2026-08-20T00:00:00.000Z"),
			generateId: () => "AC-ABC123",
		})
		const sow = ticket("AC-IGNORED").statementOfWork
		const created = await service.createFromStatementOfWork({ ...sow, title: "Cross-platform: storage / names" })
		expect(created).toMatchObject({ ok: true, value: { id: "AC-ABC123" } })
		const originalName = "AC-ABC123-cross-platform-storage-names.json"
		expect(await fs.readdir(path.join(repository.rootPath, ".agilecode", "tickets"))).toEqual([originalName])

		const renamed = { ...(created as { ok: true; value: Ticket }).value }
		renamed.statementOfWork = { ...renamed.statementOfWork, title: "A completely different title" }
		expect((await service.update(renamed.id, renamed)).ok).toBe(true)
		expect(await fs.readdir(path.join(repository.rootPath, ".agilecode", "tickets"))).toEqual([originalName])

		const collision = await service.createFromStatementOfWork(sow)
		expect(collision).toMatchObject({ ok: false })
		expect((await service.read("AC-ABC123")) as { ok: true; value: Ticket }).toMatchObject({
			value: { statementOfWork: { title: "A completely different title" } },
		})
		service.dispose()
	})

	it("removes the independent record when board insertion fails", async () => {
		const repository = await scope("7")
		await initializeAgileCodeStore(repository)
		const service = await RepositoryBoardService.create(repository, {
			watch: false,
			generateId: () => "AC-FAIL01",
			beforeCreateBoardWrite: () => {
				throw new Error("forced board persistence failure")
			},
		})

		const result = await service.createFromStatementOfWork(ticket("AC-IGNORED").statementOfWork)

		expect(result).toMatchObject({ ok: false, code: "persistence-failed" })
		expect(await fs.readdir(path.join(repository.rootPath, ".agilecode", "tickets"))).toEqual([])
		expect(service.activeBoard.columns.backlog).toEqual([])
		service.dispose()
	})

	it("provides ticket, board, settings, diagnostics, archive, restore, and delete operations", async () => {
		const repository = await scope("a")
		await initializeAgileCodeStore(repository)
		const onDidChange = vi.fn()
		const service = await RepositoryBoardService.create(repository, {
			watch: false,
			now: () => new Date("2026-08-17T12:00:00.000Z"),
			onDidChange,
		})

		const created = await service.create(ticket("AC-010"))
		expect(created.ok).toBe(true)
		expect(service.listTickets().map(({ id }) => id)).toEqual(["AC-010"])
		expect(service.activeBoard.columns.backlog).toEqual(["AC-010"])
		expect(service.listArchivedTickets()).toEqual([])
		expect(service.repositorySettings.formatVersion).toBe(AGILECODE_STORE_FORMAT_VERSION)
		expect(service.recoveryDiagnostics).toEqual([])

		const archived = await service.archive("AC-010")
		expect(archived.ok).toBe(true)
		expect(service.listArchivedTickets()[0]?.lifecycle).toMatchObject({
			state: "archived",
			archivedFrom: "backlog",
			archivedAt: "2026-08-17T12:00:00.000Z",
		})
		expect((await service.restore("AC-010")).ok).toBe(true)
		expect(service.activeBoard.columns.backlog).toEqual(["AC-010"])
		expect((await service.archive("AC-010")).ok).toBe(true)
		expect((await service.deletePermanently("AC-010", true)).ok).toBe(true)
		expect(service.listArchivedTickets()).toEqual([])
		expect(onDidChange).toHaveBeenCalledTimes(5)
		service.dispose()
	})

	it("enforces transition, permanent deletion, identity, and ticket validation contracts", async () => {
		const repository = await scope("b")
		await initializeAgileCodeStore(repository)
		const service = await RepositoryBoardService.create(repository, { watch: false })
		await service.create(ticket("AC-011"))

		const invalidMove = await service.move("AC-011", "done", "user")
		expect(invalidMove).toMatchObject({ ok: false, code: "transition-rejected" })
		const invalidUpdate = await service.update("AC-011", {
			...ticket("AC-011"),
			lifecycle: { ...ticket("AC-011").lifecycle, state: "ready" },
		})
		expect(invalidUpdate).toMatchObject({ ok: false, code: "transition-rejected" })
		const invalidDelete = await service.deletePermanently("AC-011", true)
		expect(invalidDelete).toMatchObject({ ok: false, code: "transition-rejected" })
		expect(service.listTickets()[0]?.lifecycle.state).toBe("backlog")

		const other = await scope("c")
		await initializeAgileCodeStore(other)
		await expect(RepositoryBoardService.create({ ...other, id: repository.id }, { watch: false })).rejects.toThrow(
			"Board identity mismatch",
		)
		service.dispose()
	})

	it("keeps incomplete drafts out of Ready and execution", async () => {
		const repository = await scope("8")
		await initializeAgileCodeStore(repository)
		const service = await RepositoryBoardService.create(repository, { watch: false })
		const draft = ticket("AC-030")
		draft.statementOfWork = {
			title: "Vague request",
			objective: "",
			context: "",
			requirements: [],
			constraints: [],
			includedScope: [],
			dependencies: [],
			acceptanceCriteria: [],
			validation: [],
		}
		expect((await service.create(draft)).ok).toBe(true)

		const incomplete = await service.move("AC-030", "ready", "user")
		expect(incomplete).toMatchObject({ ok: false, code: "transition-rejected" })
		if (!incomplete.ok) {
			expect(incomplete.message).toContain("objective:")
			expect(incomplete.message).toContain("acceptanceCriteria:")
			expect(incomplete.message).toContain("validation:")
		}

		service.dispose()
	})

	it("edits eligible tickets while preserving metadata and reclassifying an incomplete Ready ticket", async () => {
		const repository = await scope("6")
		await initializeAgileCodeStore(repository)
		const service = await RepositoryBoardService.create(repository, { watch: false })
		const original = ticket("AC-032")
		original.lifecycle.reviewComments.push({
			id: "review-1",
			comment: "Keep this review",
			createdAt: "2026-08-17T01:00:00.000Z",
		})
		original.lifecycle.failedAttempts.push({
			historyItemId: "task-1",
			summary: "Provider failed",
			failedAt: "2026-08-17T02:00:00.000Z",
		})
		original.execution.historyItemIds.push("task-1")
		await service.create(original)
		await service.move(original.id, "ready", "user")

		const edited = await service.updateStatementOfWork(original.id, {
			...original.statementOfWork,
			title: "Revised title",
			objective: "",
		})

		expect(edited).toMatchObject({
			ok: true,
			value: {
				id: original.id,
				statementOfWork: { title: "Revised title" },
				lifecycle: {
					state: "backlog",
					createdAt: original.lifecycle.createdAt,
					reviewComments: original.lifecycle.reviewComments,
					failedAttempts: original.lifecycle.failedAttempts,
				},
				execution: original.execution,
			},
		})
		expect(service.activeBoard.columns.ready).toEqual([])
		expect(service.activeBoard.columns.backlog).toEqual([original.id])
		service.dispose()
	})

	it("enforces execution locks for UI, tool, and stale full-record callers", async () => {
		const repository = await scope("1")
		await initializeAgileCodeStore(repository)
		const service = await RepositoryBoardService.create(repository, { watch: false })
		const running = ticket("AC-033", "in_progress")
		running.execution.historyItemIds.push("task-running")
		await service.create(running)

		const revised = { ...running.statementOfWork, title: "Silently revised scope" }
		expect(await service.updateStatementOfWork(running.id, revised)).toMatchObject({
			ok: false,
			code: "transition-rejected",
			message: expect.stringContaining("contract for the running task"),
		})
		expect(await service.updateDependencies(running.id, ["AC-999"])).toMatchObject({
			ok: false,
			code: "transition-rejected",
		})
		expect(await service.update(running.id, { ...running, statementOfWork: revised })).toMatchObject({
			ok: false,
			code: "transition-rejected",
		})
		expect((await service.read(running.id)) as { ok: true; value: Ticket }).toMatchObject({
			value: { statementOfWork: { title: running.statementOfWork.title } },
		})
		service.dispose()
	})

	it("locks resumable Blocked work but edits pre-execution Blocked and cancelled Ready work", async () => {
		const repository = await scope("2")
		await initializeAgileCodeStore(repository)
		const service = await RepositoryBoardService.create(repository, { watch: false })
		const resumable = ticket("AC-034", "blocked")
		resumable.lifecycle.blockedReasons.push({ reason: "Waiting for input", createdAt: "2026-08-20T00:00:00.000Z" })
		resumable.execution.historyItemIds.push("task-resumable")
		const preExecution = ticket("AC-035", "blocked")
		preExecution.lifecycle.blockedReasons.push({
			reason: "Missing dependency",
			createdAt: "2026-08-20T00:00:00.000Z",
		})
		const cancelled = ticket("AC-036", "ready")
		cancelled.execution.historyItemIds.push("task-cancelled")
		await service.create(resumable)
		await service.create(preExecution)
		await service.create(cancelled)

		expect(
			await service.updateStatementOfWork(resumable.id, {
				...resumable.statementOfWork,
				title: "Forbidden revision",
			}),
		).toMatchObject({ ok: false, message: expect.stringContaining("can be resumed") })
		expect(
			await service.updateStatementOfWork(preExecution.id, {
				...preExecution.statementOfWork,
				title: "Clarified before execution",
			}),
		).toMatchObject({ ok: true })
		expect(
			await service.updateStatementOfWork(cancelled.id, {
				...cancelled.statementOfWork,
				title: "Revised after cancellation",
			}),
		).toMatchObject({ ok: true })
		service.dispose()
	})

	it("rejects missing and cyclic dependencies without changing the stored ticket", async () => {
		const repository = await scope("5")
		await initializeAgileCodeStore(repository)
		const service = await RepositoryBoardService.create(repository, { watch: false })
		const first = ticket("AC-032")
		const second = ticket("AC-033")
		second.statementOfWork.dependencies = [first.id]
		await service.create(first)
		await service.create(second)

		const missing = await service.updateStatementOfWork(first.id, {
			...first.statementOfWork,
			dependencies: ["AC-404"],
		})
		expect(missing).toMatchObject({ ok: false, code: "invalid-ticket" })
		const cyclic = await service.updateStatementOfWork(first.id, {
			...first.statementOfWork,
			dependencies: [second.id],
		})
		expect(cyclic).toMatchObject({ ok: false, code: "invalid-ticket" })
		expect((await service.read(first.id)) as { ok: true; value: Ticket }).toMatchObject({
			value: { statementOfWork: { dependencies: [] } },
		})
		service.dispose()
	})

	it("rolls back readiness movement when edited ticket persistence fails", async () => {
		const repository = await scope("4")
		await initializeAgileCodeStore(repository)
		let fail = false
		const service = await RepositoryBoardService.create(repository, {
			watch: false,
			beforeEditTicketWrite: () => {
				if (fail) throw new Error("forced persistence failure")
			},
		})
		const original = ticket("AC-032")
		await service.create(original)
		await service.move(original.id, "ready", "user")
		fail = true

		const result = await service.updateStatementOfWork(original.id, {
			...original.statementOfWork,
			objective: "",
		})

		expect(result).toMatchObject({ ok: false })
		expect((await service.read(original.id)) as { ok: true; value: Ticket }).toMatchObject({
			value: {
				lifecycle: { state: "ready" },
				statementOfWork: { objective: original.statementOfWork.objective },
			},
		})
		const board = (await RepositoryBoardService.create(repository, { watch: false })).activeBoard
		expect(board.columns.ready).toEqual([original.id])
		service.dispose()
	})

	it("isolates repositories and gives UI and tool adapters equivalent results", async () => {
		const first = await scope("d")
		const second = await scope("e")
		await Promise.all([initializeAgileCodeStore(first), initializeAgileCodeStore(second)])
		const firstService = await RepositoryBoardService.create(first, { watch: false })
		const secondService = await RepositoryBoardService.create(second, { watch: false })
		const uiCaller = (service: RepositoryBoardService, value: Ticket) => service.create(value)
		const toolCaller = (service: RepositoryBoardService, value: Ticket) => service.create(value)

		const [uiResult, toolResult] = await Promise.all([
			uiCaller(firstService, ticket("AC-012")),
			toolCaller(secondService, ticket("AC-012")),
		])
		expect(uiResult.ok).toBe(true)
		expect(toolResult.ok).toBe(true)
		expect(firstService.state).toEqual({ ...secondService.state, manifest: firstService.state.manifest })

		await firstService.move("AC-012", "ready", "user")
		expect(firstService.activeBoard.columns.ready).toEqual(["AC-012"])
		expect(secondService.activeBoard.columns.backlog).toEqual(["AC-012"])
		firstService.dispose()
		secondService.dispose()
	})

	it("persists first, middle, last, and no-op reorders without rewriting tickets", async () => {
		const repository = await scope("3")
		await initializeAgileCodeStore(repository)
		const service = await RepositoryBoardService.create(repository, { watch: false })
		for (const id of ["AC-001", "AC-002", "AC-003", "AC-004"]) await service.create(ticket(id))
		const ticketPath = path.join(repository.rootPath, ".agilecode", "tickets", "AC-002.json")
		const ticketBefore = await fs.readFile(ticketPath, "utf8")

		let expected = ["AC-001", "AC-002", "AC-003", "AC-004"]
		for (const ordered of [
			["AC-004", "AC-001", "AC-002", "AC-003"], // last to first
			["AC-004", "AC-002", "AC-001", "AC-003"], // first to middle
			["AC-004", "AC-002", "AC-003", "AC-001"], // middle to last
			["AC-004", "AC-002", "AC-003", "AC-001"], // no-op
		]) {
			expect(await service.reorder("backlog", ordered, expected)).toMatchObject({ ok: true })
			expected = ordered
		}

		expect(await fs.readFile(ticketPath, "utf8")).toBe(ticketBefore)
		const reloaded = await RepositoryBoardService.create(repository, { watch: false })
		expect(reloaded.activeBoard.columns.backlog).toEqual(expected)
		service.dispose()
		reloaded.dispose()
	})

	it("rejects stale reorders without dropping or duplicating tickets", async () => {
		const repository = await scope("0")
		await initializeAgileCodeStore(repository)
		const first = await RepositoryBoardService.create(repository, { watch: false })
		for (const id of ["AC-001", "AC-002", "AC-003"]) await first.create(ticket(id))
		const stale = await RepositoryBoardService.create(repository, { watch: false })

		expect(
			await first.reorder("backlog", ["AC-003", "AC-001", "AC-002"], ["AC-001", "AC-002", "AC-003"]),
		).toMatchObject({ ok: true })
		expect(
			await stale.reorder("backlog", ["AC-002", "AC-003", "AC-001"], ["AC-001", "AC-002", "AC-003"]),
		).toMatchObject({ ok: false, code: "conflict" })

		const reloaded = await RepositoryBoardService.create(repository, { watch: false })
		expect(reloaded.activeBoard.columns.backlog).toEqual(["AC-003", "AC-001", "AC-002"])
		expect(new Set(reloaded.activeBoard.columns.backlog).size).toBe(3)
		first.dispose()
		stale.dispose()
		reloaded.dispose()
	})

	it("publishes one validated notification after an external change", async () => {
		const repository = await scope("f")
		await initializeAgileCodeStore(repository)
		const onDidChange = vi.fn()
		const watched = await RepositoryBoardService.create(repository, { onDidChange })
		const writer = await RepositoryBoardService.create(repository, { watch: false })
		await writer.create(ticket("AC-013"))

		await vi.waitFor(() => expect(onDidChange).toHaveBeenCalledTimes(1))
		expect(onDidChange.mock.calls[0]?.[0]).toMatchObject({ source: "external" })
		expect(watched.listTickets()[0]?.id).toBe("AC-013")
		watched.dispose()
		writer.dispose()
	})
})
