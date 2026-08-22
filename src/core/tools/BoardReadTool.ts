import type { BoardSnapshot, Ticket, TicketWorkflowState } from "@roo-code/types"

import type { Task } from "../task/Task"
import { BaseTool, type ToolCallbacks } from "./BaseTool"

type ReadName = "list_boards" | "inspect_board" | "inspect_ticket"

const summary = (ticket: Ticket) => ({
	id: ticket.id,
	title: ticket.statementOfWork.title,
	state: ticket.lifecycle.state,
	dependencies: ticket.statementOfWork.dependencies,
	blockedReasons: ticket.lifecycle.blockedReasons,
	reviewComments: ticket.lifecycle.reviewComments,
})

export class BoardReadTool<T extends ReadName> extends BaseTool<T> {
	constructor(readonly name: T) {
		super()
	}

	async execute(params: any, task: Task, { pushToolResult }: ToolCallbacks): Promise<void> {
		const provider = task.providerRef.deref()
		if (!provider) return pushToolResult(JSON.stringify({ ok: false, code: "board_unavailable" }))
		if (this.name === "list_boards") {
			const scopes = await provider.boardScopeSelector.list()
			return pushToolResult(JSON.stringify({ ok: true, scopes }))
		}

		const snapshot: BoardSnapshot | undefined = provider.boardStatePublisher.readSnapshot(params.board_id)
		if (!snapshot) {
			return pushToolResult(JSON.stringify({ ok: false, code: "board_not_found", boardId: params.board_id }))
		}
		if (this.name === "inspect_ticket") {
			const active = snapshot.activeTickets.find(({ id }) => id === params.ticket_id)
			const archived = params.include_archived
				? snapshot.archivedTickets.find(({ id }) => id === params.ticket_id)
				: undefined
			const ticket = active ?? archived
			return pushToolResult(
				JSON.stringify(
					ticket
						? { ok: true, boardId: snapshot.scope.id, scope: snapshot.scope, ticket }
						: {
								ok: false,
								code: "ticket_not_found",
								boardId: snapshot.scope.id,
								ticketId: params.ticket_id,
							},
				),
			)
		}

		const requested = params.states as TicketWorkflowState[] | undefined
		const allowed = requested ? new Set(requested) : undefined
		const columns = Object.entries(snapshot.board.columns).map(([state, ids]) => ({
			state,
			tickets:
				allowed && !allowed.has(state as TicketWorkflowState)
					? []
					: ids.map((id) => summary(snapshot.activeTickets.find((ticket) => ticket.id === id)!)),
		}))
		const archived =
			params.include_archived && (!allowed || allowed.has("archived"))
				? snapshot.board.archiveOrder.map((id) =>
						summary(snapshot.archivedTickets.find((ticket) => ticket.id === id)!),
					)
				: undefined
		pushToolResult(
			JSON.stringify({ ok: true, boardId: snapshot.scope.id, scope: snapshot.scope, columns, archived }),
		)
	}
}

export const listBoardsTool = new BoardReadTool("list_boards")
export const inspectBoardTool = new BoardReadTool("inspect_board")
export const inspectTicketTool = new BoardReadTool("inspect_ticket")
