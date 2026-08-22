import { randomUUID } from "crypto"

import { ticketSetProposalSchema, ticketStatementOfWorkSchema } from "@roo-code/types"

import type { Task } from "../task/Task"
import { BaseTool, type ToolCallbacks } from "./BaseTool"

type WriteName = "create_ticket" | "update_ticket" | "decompose_work"

export class BoardWriteTool<T extends WriteName> extends BaseTool<T> {
	constructor(readonly name: T) {
		super()
	}

	async execute(params: any, task: Task, { pushToolResult }: ToolCallbacks): Promise<void> {
		const provider = task.providerRef.deref()
		if (!provider) return pushToolResult(JSON.stringify({ ok: false, code: "board_unavailable" }))

		const parsed =
			this.name === "decompose_work"
				? ticketSetProposalSchema.safeParse(params.proposal)
				: ticketStatementOfWorkSchema.safeParse(params.statement_of_work)
		if (!parsed.success)
			return pushToolResult(
				JSON.stringify({
					ok: false,
					code: "invalid_request",
					message: parsed.error.issues.map((issue) => issue.message).join("; "),
				}),
			)

		const request =
			this.name === "decompose_work"
				? {
						requestId: randomUUID(),
						boardId: params.board_id,
						operation: "decompose_work" as const,
						proposal: parsed.data,
						createApprovedSet: params.create_approved_set === true,
					}
				: this.name === "create_ticket"
					? {
							requestId: randomUUID(),
							boardId: params.board_id,
							operation: "create_ticket" as const,
							ticket: parsed.data,
							initialState: params.initial_state,
						}
					: {
							requestId: randomUUID(),
							boardId: params.board_id,
							operation: "update_ticket" as const,
							ticketId: params.ticket_id,
							statementOfWork: parsed.data,
						}
		const result = await provider.boardStatePublisher.executeAgentRequest(request as any, params.expected_revision)
		pushToolResult(JSON.stringify(result))
	}
}

export const createTicketTool = new BoardWriteTool("create_ticket")
export const updateTicketTool = new BoardWriteTool("update_ticket")
export const decomposeWorkTool = new BoardWriteTool("decompose_work")
