import { activeBoardStates, decideTicketTransition, type Ticket, type TicketExecutionState } from "@roo-code/types"

export type ActiveBoardState = (typeof activeBoardStates)[number]

export interface ManualTicketTransition {
	destination: ActiveBoardState
	label: string
	operation: "move_ticket" | "start_ticket_execution"
}

export const ticketExecutionState = (ticket: Ticket): TicketExecutionState => {
	if (ticket.lifecycle.state === "in_progress") return "active"
	if (ticket.lifecycle.state === "blocked" && ticket.execution.historyItemIds.length > 0) return "resumable"
	return "none"
}

/** The single transition projection used by both pointer dragging and the status control. */
export const manualTicketTransitions = (ticket: Ticket): ManualTicketTransition[] => {
	if (ticket.lifecycle.state === "archived") return []

	return activeBoardStates.flatMap((destination) => {
		if (destination === ticket.lifecycle.state) return []
		const decision = decideTicketTransition(
			{ state: ticket.lifecycle.state, execution: ticketExecutionState(ticket) },
			{ type: "move", destination, actor: "user" },
		)
		if (!decision.allowed) return []
		return [
			{
				destination,
				label:
					destination === "in_progress"
						? decision.operation === "request_resume"
							? "Resume"
							: "Execute"
						: `Move to ${destination.replace("_", " ")}`,
				operation:
					decision.operation === "request_execution" || decision.operation === "request_resume"
						? "start_ticket_execution"
						: "move_ticket",
			},
		]
	})
}
