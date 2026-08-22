import { randomUUID } from "crypto"

import { ticketStatementOfWorkSchema } from "@roo-code/types"

import type { Task } from "../task/Task"
import { BaseTool, type ToolCallbacks } from "./BaseTool"

type WriteName = "create_ticket" | "update_ticket"

export class BoardWriteTool<T extends WriteName> extends BaseTool<T> {
	constructor(readonly name: T) {
		super()
	}

	async execute(params: any, task: Task, { pushToolResult }: ToolCallbacks): Promise<void> {
		const provider = task.providerRef.deref()
		if (!provider) return pushToolResult(JSON.stringify({ ok: false, code: "board_unavailable" }))

		const parsed = ticketStatementOfWorkSchema.safeParse(params.statement_of_work)
		if (!parsed.success) {
			return pushToolResult(
				JSON.stringify({
					ok: false,
					code: "invalid_request",
					message: parsed.error.issues.map((issue) => issue.message).join("; "),
				}),
			)
		}
		const result = await provider.boardStatePublisher.executeAgentRequest(
			this.name === "create_ticket"
				? {
						requestId: randomUUID(),
						boardId: params.board_id,
						operation: "create_ticket",
						ticket: parsed.data,
						initialState: params.initial_state,
					}
				: {
						requestId: randomUUID(),
						boardId: params.board_id,
						operation: "update_ticket",
						ticketId: params.ticket_id,
						statementOfWork: parsed.data,
					},
			params.expected_revision,
		)
		pushToolResult(JSON.stringify(result))
	}
}

export const createTicketTool = new BoardWriteTool("create_ticket")
export const updateTicketTool = new BoardWriteTool("update_ticket")
