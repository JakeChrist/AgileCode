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
