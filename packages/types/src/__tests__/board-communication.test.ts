import { expectTypeOf } from "vitest"

import {
	boardErrorSchema,
	boardRequestSchema,
	boardResultSchema,
	boardStateEventSchema,
	type BoardExtensionMessage,
	type BoardRequest,
	type BoardWebviewMessage,
} from "../index.js"

const boardId = `git:${"a".repeat(64)}`
const statementOfWork = {
	title: "Typed board messages",
	objective: "Keep both sides aligned",
	context: "The board crosses a process boundary",
	requirements: ["Use explicit contracts"],
	constraints: ["Remain compatible"],
	includedScope: ["Board messages"],
	dependencies: [],
	acceptanceCriteria: ["Invalid messages fail validation"],
	validation: ["Run type tests"],
}
const ticket = {
	formatVersion: 1 as const,
	id: "AC-011",
	statementOfWork,
	lifecycle: {
		state: "backlog" as const,
		createdAt: "2026-08-14T00:00:00.000Z",
		reviewComments: [],
		blockedReasons: [],
		failedAttempts: [],
	},
	execution: { historyItemIds: [] },
}
const board = {
	formatVersion: 1 as const,
	columns: { backlog: ["AC-011"], ready: [], in_progress: [], blocked: [], review: [], done: [] },
	archiveOrder: [],
}
const settings = {
	formatVersion: 1 as const,
	automaticArchival: { enabled: false, retentionDays: 30 },
	repositorySelection: { preferredScopeId: null },
	showArchived: false,
	suppressDragToExecuteWarning: false,
	workflowPreferences: {},
}
const scope = { id: boardId, kind: "git" as const, rootPath: "/repository" }
const snapshot = { scope, board, settings, activeTickets: [ticket], archivedTickets: [] }

describe("board communication contract", () => {
	it("validates load, inspect, mutation, improvement, execution, review, and archive requests", () => {
		const requests = [
			{ operation: "load_board" },
			{ operation: "get_ticket", ticketId: "AC-011" },
			{ operation: "create_ticket", ticket: statementOfWork },
			{ operation: "update_ticket", ticketId: "AC-011", statementOfWork },
			{ operation: "move_ticket", ticketId: "AC-011", destination: "ready", position: 0 },
			{ operation: "improve_ticket_draft", draft: statementOfWork },
			{ operation: "start_ticket_execution", ticketId: "AC-011" },
			{ operation: "cancel_ticket_execution", ticketId: "AC-011" },
			{ operation: "accept_ticket", ticketId: "AC-011" },
			{ operation: "reject_ticket", ticketId: "AC-011", comment: "Needs a test" },
			{ operation: "archive_ticket", ticketId: "AC-011" },
			{ operation: "restore_ticket", ticketId: "AC-011" },
			{ operation: "delete_ticket", ticketId: "AC-011", confirmed: true },
			{ operation: "update_board_settings", settings },
		]

		for (const [index, request] of requests.entries()) {
			expect(boardRequestSchema.safeParse({ requestId: `request-${index}`, boardId, ...request }).success).toBe(
				true,
			)
		}
	})

	it("rejects missing operation data and unconfirmed permanent deletion", () => {
		expect(boardRequestSchema.safeParse({ requestId: "1", boardId, operation: "get_ticket" }).success).toBe(false)
		expect(
			boardRequestSchema.safeParse({
				requestId: "2",
				boardId,
				operation: "delete_ticket",
				ticketId: "AC-011",
				confirmed: false,
			}).success,
		).toBe(false)
	})

	it("validates correlated success and structured error results", () => {
		expect(
			boardResultSchema.safeParse({ requestId: "1", boardId, operation: "load_board", ok: true, snapshot })
				.success,
		).toBe(true)
		const error = {
			operation: "move_ticket",
			code: "invalid_transition",
			message: "This ticket cannot move while execution is active.",
			retryable: false,
		}
		expect(boardErrorSchema.safeParse(error).success).toBe(true)
		expect(
			boardResultSchema.safeParse({ requestId: "2", boardId, operation: "move_ticket", ok: false, error })
				.success,
		).toBe(true)
		expect(
			boardResultSchema.safeParse({ requestId: "3", boardId, operation: "get_ticket", ok: false, error }).success,
		).toBe(false)
	})

	it("validates repository-identified state pushes", () => {
		expect(
			boardStateEventSchema.safeParse({ type: "board_state_changed", boardId, scope, revision: 3, snapshot })
				.success,
		).toBe(true)
		expect(
			boardStateEventSchema.safeParse({
				type: "board_state_changed",
				boardId: `workspace:${"b".repeat(64)}`,
				scope,
				revision: 3,
				snapshot,
			}).success,
		).toBe(false)
	})

	it("provides strict message envelopes to extension and webview callers", () => {
		expectTypeOf<BoardWebviewMessage>().toMatchTypeOf<{ type: "board_request"; request: BoardRequest }>()
		expectTypeOf<BoardExtensionMessage>().toHaveProperty("type")
		// @ts-expect-error get_ticket always requires a ticket identifier
		const invalidRequest: BoardRequest = { requestId: "1", boardId, operation: "get_ticket" }
		expect(invalidRequest.operation).toBe("get_ticket")
	})
})
