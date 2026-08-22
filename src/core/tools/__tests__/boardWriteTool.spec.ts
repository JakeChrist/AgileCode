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

const run = async (name: "create_ticket" | "update_ticket", params: object, executeAgentRequest = vi.fn()) => {
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
})
