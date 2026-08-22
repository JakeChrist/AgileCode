import { describe, expect, it, vi } from "vitest"

import type { Task } from "../../task/Task"
import type { ToolCallbacks } from "../BaseTool"
import { BoardWriteTool } from "../BoardWriteTool"

const statementOfWork = {
	title: "Create agent ticket tools",
	objective: "Let Scrum Master author one ticket.",
	context: "Ticket authoring must use board persistence.",
	requirements: ["Persist atomically."],
	constraints: [],
	includedScope: ["Single ticket creation."],
	dependencies: [],
	acceptanceCriteria: ["The ticket appears on the board."],
	validation: ["Run handler tests."],
}

const run = async (
	name: "create_ticket" | "update_ticket" | "decompose_work" | "move_ticket" | "reorder_tickets" | "block_ticket",
	params: object,
	executeAgentRequest = vi.fn(),
) => {
	const pushToolResult = vi.fn()
	const task = {
		providerRef: { deref: () => ({ boardStatePublisher: { executeAgentRequest } }) },
	} as unknown as Task
	await new BoardWriteTool(name).execute(params, task, { pushToolResult } as unknown as ToolCallbacks)
	return { result: JSON.parse(pushToolResult.mock.calls[0][0]), executeAgentRequest }
}

describe("BoardWriteTool", () => {
	it("creates through the shared board mutation path and reports identity and state", async () => {
		const execute = vi.fn(async () => ({ ok: true, ticket: { id: "AC-062", lifecycle: { state: "backlog" } } }))
		const { result, executeAgentRequest } = await run(
			"create_ticket",
			{ board_id: "git:board", statement_of_work: statementOfWork, expected_revision: 4 },
			execute,
		)
		expect(executeAgentRequest).toHaveBeenCalledWith(
			expect.objectContaining({ boardId: "git:board", operation: "create_ticket", ticket: statementOfWork }),
			4,
		)
		expect(result).toMatchObject({ ok: true, ticket: { id: "AC-062", lifecycle: { state: "backlog" } } })
	})

	it("updates the complete statement of work without accepting lifecycle fields", async () => {
		const execute = vi.fn(async () => ({ ok: true, ticket: { id: "AC-032", lifecycle: { state: "ready" } } }))
		await run(
			"update_ticket",
			{ board_id: "git:board", ticket_id: "AC-032", statement_of_work: statementOfWork, expected_revision: 8 },
			execute,
		)
		expect(execute).toHaveBeenCalledWith(
			expect.objectContaining({ operation: "update_ticket", ticketId: "AC-032", statementOfWork }),
			8,
		)
	})

	it("rejects invalid content before persistence", async () => {
		const { result, executeAgentRequest } = await run("create_ticket", {
			board_id: "git:board",
			statement_of_work: { ...statementOfWork, title: "" },
			expected_revision: 1,
		})
		expect(result).toMatchObject({ ok: false, code: "invalid_request" })
		expect(executeAgentRequest).not.toHaveBeenCalled()
	})

	it("returns a proposal without requesting persistence unless explicitly approved", async () => {
		const execute = vi.fn(async (request) => ({ ok: true, proposal: request.proposal, created: [] }))
		const proposal = {
			sourceWorkDefinition: "SOW",
			tickets: [{ proposalId: "core", statementOfWork, dependsOn: [], sourceItems: ["R1"] }],
			unassignedSourceItems: [],
		}
		await run(
			"decompose_work",
			{ board_id: "git:board", proposal, create_approved_set: false, expected_revision: 2 },
			execute,
		)
		expect(execute).toHaveBeenCalledWith(
			expect.objectContaining({ operation: "decompose_work", proposal, createApprovedSet: false }),
			2,
		)
	})

	it.each([
		[
			"move_ticket",
			{ ticket_id: "AC-064", destination: "ready", position: 1 },
			{ operation: "move_ticket", ticketId: "AC-064", destination: "ready", position: 1 },
		],
		[
			"block_ticket",
			{ ticket_id: "AC-064", reason: "Waiting for approval", position: 0 },
			{ operation: "block_ticket", ticketId: "AC-064", reason: "Waiting for approval", position: 0 },
		],
	] as const)("builds an explicit %s request", async (name, params, expected) => {
		const execute = vi.fn(async () => ({ ok: true }))
		await run(name, { board_id: "git:board", expected_revision: 9, ...params }, execute)
		expect(execute).toHaveBeenCalledWith(expect.objectContaining({ boardId: "git:board", ...expected }), 9)
	})

	it("sends complete desired and inspected orders for conflict-safe prioritization", async () => {
		const execute = vi.fn(async () => ({ ok: true }))
		await run(
			"reorder_tickets",
			{
				board_id: "git:board",
				state: "backlog",
				ordered_ids: ["AC-2", "AC-1"],
				expected_order: ["AC-1", "AC-2"],
				expected_revision: 10,
			},
			execute,
		)
		expect(execute).toHaveBeenCalledWith(
			expect.objectContaining({
				operation: "reorder_tickets",
				state: "backlog",
				orderedIds: ["AC-2", "AC-1"],
				expectedOrder: ["AC-1", "AC-2"],
			}),
			10,
		)
	})
})
