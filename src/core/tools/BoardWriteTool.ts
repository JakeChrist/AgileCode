import { randomUUID } from "crypto"

import { ticketSetProposalSchema, ticketStatementOfWorkSchema } from "@roo-code/types"

import type { Task } from "../task/Task"
import { BaseTool, type ToolCallbacks } from "./BaseTool"

type WriteName =
	| "create_ticket"
	| "update_ticket"
	| "decompose_work"
	| "move_ticket"
	| "reorder_tickets"
	| "block_ticket"
	| "record_review_feedback"
	| "archive_ticket"
	| "restore_ticket"
	| "delete_ticket"

export class BoardWriteTool<T extends WriteName> extends BaseTool<T> {
	constructor(readonly name: T) {
		super()
	}

	async execute(params: any, task: Task, { pushToolResult }: ToolCallbacks): Promise<void> {
		const provider = task.providerRef.deref()
		if (!provider) return pushToolResult(JSON.stringify({ ok: false, code: "board_unavailable" }))
		if (this.name === "delete_ticket" && params.confirmed !== true) {
			return pushToolResult(
				JSON.stringify({
					ok: false,
					code: "confirmation_required",
					message: "Permanent deletion requires explicit confirmed user intent.",
				}),
			)
		}

		const parsed =
			this.name === "decompose_work"
				? ticketSetProposalSchema.safeParse(params.proposal)
				: this.name === "create_ticket" || this.name === "update_ticket"
					? ticketStatementOfWorkSchema.safeParse(params.statement_of_work)
					: { success: true as const, data: undefined }
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
							originatingReview:
								params.originating_review_ticket_id && params.originating_review_comment_id
									? {
											ticketId: params.originating_review_ticket_id,
											commentId: params.originating_review_comment_id,
										}
									: undefined,
						}
					: this.name === "update_ticket"
						? {
								requestId: randomUUID(),
								boardId: params.board_id,
								operation: "update_ticket" as const,
								ticketId: params.ticket_id,
								statementOfWork: parsed.data!,
							}
						: this.name === "move_ticket"
							? {
									requestId: randomUUID(),
									boardId: params.board_id,
									operation: "move_ticket" as const,
									ticketId: params.ticket_id,
									destination: params.destination,
									position: params.position,
								}
							: this.name === "reorder_tickets"
								? {
										requestId: randomUUID(),
										boardId: params.board_id,
										operation: "reorder_tickets" as const,
										state: params.state,
										orderedIds: params.ordered_ids,
										expectedOrder: params.expected_order,
									}
								: this.name === "block_ticket"
									? {
											requestId: randomUUID(),
											boardId: params.board_id,
											operation: "block_ticket" as const,
											ticketId: params.ticket_id,
											reason: params.reason,
											position: params.position,
										}
									: {
											requestId: randomUUID(),
											boardId: params.board_id,
											operation: this.name,
											ticketId: params.ticket_id,
											...(this.name === "record_review_feedback"
												? { comment: params.comment, author: params.author }
												: this.name === "delete_ticket"
													? { confirmed: params.confirmed === true }
													: {}),
										}
		const result = await provider.boardStatePublisher.executeAgentRequest(request as any, params.expected_revision)
		pushToolResult(JSON.stringify(result))
	}
}

export const createTicketTool = new BoardWriteTool("create_ticket")
export const updateTicketTool = new BoardWriteTool("update_ticket")
export const decomposeWorkTool = new BoardWriteTool("decompose_work")
export const moveTicketTool = new BoardWriteTool("move_ticket")
export const reorderTicketsTool = new BoardWriteTool("reorder_tickets")
export const blockTicketTool = new BoardWriteTool("block_ticket")
export const recordReviewFeedbackTool = new BoardWriteTool("record_review_feedback")
export const archiveTicketTool = new BoardWriteTool("archive_ticket")
export const restoreTicketTool = new BoardWriteTool("restore_ticket")
export const deleteTicketTool = new BoardWriteTool("delete_ticket")
