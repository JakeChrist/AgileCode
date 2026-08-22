import type { BoardSnapshot, Ticket } from "@roo-code/types"
import { beforeEach, describe, expect, it, vi } from "vitest"

import type { Task } from "../../task/Task"
import type { ToolCallbacks } from "../BaseTool"
import { BoardReadTool } from "../BoardReadTool"

const scope = { id: `git:${"a".repeat(64)}`, kind: "git" as const, rootPath: "/repo/a" }
const ticket = (id: string, state: Ticket["lifecycle"]["state"]): Ticket =>
	({
		formatVersion: 1,
		id,
		statementOfWork: { title: id, objective: "objective", dependencies: [] },
		lifecycle: { state, createdAt: "2026-08-22T00:00:00.000Z", reviewComments: [] },
		execution: { historyItemIds: [] },
	}) as unknown as Ticket
const active = ticket("AC-061", "ready")
const archived = ticket("AC-001", "archived")
const snapshot: BoardSnapshot = {
	scope,
	board: {
		formatVersion: 1,
		columns: { backlog: [], ready: [active.id], in_progress: [], blocked: [], review: [], done: [] },
		archiveOrder: [archived.id],
	},
	settings: {} as BoardSnapshot["settings"],
	activeTickets: [active],
	archivedTickets: [archived],
	diagnostics: [],
}

describe("BoardReadTool", () => {
	let pushToolResult: ReturnType<typeof vi.fn>
	let task: Task

	beforeEach(() => {
		pushToolResult = vi.fn()
		task = {
			providerRef: {
				deref: () => ({
					boardScopeSelector: { list: vi.fn(async () => [scope]) },
					boardStatePublisher: { readSnapshot: vi.fn((id) => (id === scope.id ? snapshot : undefined)) },
				}),
			},
		} as unknown as Task
	})

	const run = async (name: "list_boards" | "inspect_board" | "inspect_ticket", params: object) => {
		await new BoardReadTool(name).execute(params, task, { pushToolResult } as unknown as ToolCallbacks)
		return JSON.parse(pushToolResult.mock.calls[0][0])
	}

	it("discovers board scopes with structured identities", async () => {
		expect(await run("list_boards", {})).toEqual({ ok: true, scopes: [scope] })
	})

	it("preserves authoritative ordering and excludes archives by default", async () => {
		const result = await run("inspect_board", { board_id: scope.id })
		expect(result.boardId).toBe(scope.id)
		expect(result.columns.find(({ state }: any) => state === "ready").tickets).toEqual([
			expect.objectContaining({ id: active.id }),
		])
		expect(result.archived).toBeUndefined()
	})

	it("requires explicit archive access and never falls back across board identities", async () => {
		expect(await run("inspect_ticket", { board_id: scope.id, ticket_id: archived.id })).toMatchObject({
			ok: false,
			code: "ticket_not_found",
		})
		pushToolResult.mockClear()
		expect(await run("inspect_ticket", { board_id: "git:missing", ticket_id: active.id })).toEqual({
			ok: false,
			code: "board_not_found",
			boardId: "git:missing",
		})
	})

	it("returns complete archived records only when requested", async () => {
		const result = await run("inspect_ticket", {
			board_id: scope.id,
			ticket_id: archived.id,
			include_archived: true,
		})
		expect(result).toMatchObject({
			ok: true,
			boardId: scope.id,
			ticket: { id: archived.id, statementOfWork: { objective: "objective" }, lifecycle: { state: "archived" } },
		})
	})
})
