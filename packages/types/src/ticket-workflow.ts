import type { Ticket, TicketWorkflowState } from "./ticket.js"

/** Customer-visible meanings. These are the authoritative board definitions. */
export const ticketWorkflowStateDefinitions = {
	backlog: "Defined work that is not yet ready or selected for execution.",
	ready: "A complete ticket that is eligible for an authorized execution.",
	in_progress: "Work whose associated execution has successfully started or resumed and is currently running.",
	blocked:
		"Work that cannot or should not proceed, either before execution or while retaining a resumable execution.",
	review: "The internal engineering workflow is complete and is awaiting user acceptance.",
	done: "The user has explicitly accepted the completed work.",
	archived: "Work outside the active board, retaining the state from which it was archived.",
} as const satisfies Record<TicketWorkflowState, string>

export type TicketExecutionState = "none" | "active" | "resumable"
export type ActiveTicketWorkflowState = Exclude<TicketWorkflowState, "archived">

export interface TicketStatementOfWorkLock {
	locked: boolean
	reason?: string
}

/**
 * Derives the statement-of-work lock exclusively from durable lifecycle signals.
 * A Blocked ticket without task history was blocked before execution and remains editable.
 */
export function getTicketStatementOfWorkLock(
	ticket: Pick<Ticket, "lifecycle" | "execution">,
): TicketStatementOfWorkLock {
	if (ticket.lifecycle.state === "in_progress") {
		return {
			locked: true,
			reason: "Editing is unavailable while this ticket is in progress because its approved statement of work is the contract for the running task.",
		}
	}
	if (ticket.lifecycle.state === "blocked" && ticket.execution.historyItemIds.length > 0) {
		return {
			locked: true,
			reason: "Editing is unavailable while this ticket is blocked with execution context that can be resumed.",
		}
	}
	if (
		ticket.lifecycle.state !== "backlog" &&
		ticket.lifecycle.state !== "ready" &&
		ticket.lifecycle.state !== "blocked"
	) {
		return {
			locked: true,
			reason: `Editing is unavailable while this ticket is ${ticket.lifecycle.state.replace("_", " ")}; its lifecycle state locks the statement of work.`,
		}
	}
	return { locked: false }
}

export type TicketWorkflowAction =
	| { type: "move"; destination: ActiveTicketWorkflowState; actor: "user" | "agent"; reason?: string }
	| { type: "execution_started" | "execution_resumed" }
	| { type: "execution_completed" }
	| { type: "waiting_for_user"; reason: string }
	| { type: "technical_failure"; summary: string }
	| { type: "cancel"; actor: "user" | "agent" }
	| { type: "review_rejected"; comment: string }
	| { type: "corrective_work_requested"; comment: string }
	| { type: "accept" }
	| { type: "archive" }
	| { type: "restore" }
	| { type: "delete_permanently"; confirmed: boolean }

export type TicketTransitionEffect = "metadata" | "runtime" | "user_confirmation"

export type TicketTransitionResult =
	| {
			allowed: true
			state: TicketWorkflowState
			effect: TicketTransitionEffect
			operation:
				| "change_state"
				| "request_execution"
				| "request_resume"
				| "cancel_execution"
				| "archive"
				| "restore"
				| "delete"
			archivedFrom?: ActiveTicketWorkflowState
	  }
	| { allowed: false; state: TicketWorkflowState; reason: string }

export interface TicketTransitionContext {
	state: TicketWorkflowState
	execution: TicketExecutionState
	/** Required for restore because Archived intentionally preserves its previous state. */
	archivedFrom?: ActiveTicketWorkflowState
}

const rejected = (state: TicketWorkflowState, reason: string): TicketTransitionResult => ({
	allowed: false,
	state,
	reason,
})

const changed = (state: TicketWorkflowState): TicketTransitionResult => ({
	allowed: true,
	state,
	effect: "metadata",
	operation: "change_state",
})

/**
 * Decides a board transition without performing persistence or task operations.
 * Callers must only persist the returned state after the named runtime operation succeeds.
 */
export function decideTicketTransition(
	context: TicketTransitionContext,
	action: TicketWorkflowAction,
): TicketTransitionResult {
	const { state, execution } = context

	if (action.type === "delete_permanently") {
		if (state !== "archived") return rejected(state, "Only archived tickets may be permanently deleted")
		if (!action.confirmed) return rejected(state, "Permanent deletion requires explicit user confirmation")
		return { allowed: true, state, effect: "user_confirmation", operation: "delete" }
	}
	if (action.type === "restore") {
		if (state !== "archived" || !context.archivedFrom) {
			return rejected(state, "Restore requires an archived ticket with its prior state")
		}
		return { allowed: true, state: context.archivedFrom, effect: "metadata", operation: "restore" }
	}
	if (state === "archived") return rejected(state, "Archived tickets must be restored before any other action")

	if (action.type === "archive") {
		if (execution !== "none")
			return rejected(state, "Running or resumable execution must be cancelled before archival")
		return { allowed: true, state: "archived", effect: "metadata", operation: "archive", archivedFrom: state }
	}

	if (action.type === "move") {
		if (execution === "active") return rejected(state, "Ordinary movement cannot contradict a running execution")
		if (execution === "resumable") {
			if (state === "blocked" && action.destination === "in_progress") {
				return { allowed: true, state, effect: "runtime", operation: "request_resume" }
			}
			return rejected(state, "Ordinary movement cannot discard or contradict a resumable execution")
		}
		if (
			state === "review" ||
			state === "done" ||
			action.destination === "review" ||
			action.destination === "done"
		) {
			return rejected(
				state,
				"Review and Done may only change through completion, rejection, or acceptance actions",
			)
		}
		if (action.destination === "in_progress") {
			if (state !== "ready") return rejected(state, "Only Ready work can request a new execution")
			return { allowed: true, state: "ready", effect: "runtime", operation: "request_execution" }
		}
		return changed(action.destination)
	}

	if (action.type === "execution_started") {
		if (state !== "ready" || execution !== "active") {
			return rejected(state, "In Progress requires a successfully started execution for a Ready ticket")
		}
		return changed("in_progress")
	}
	if (action.type === "execution_resumed") {
		if (state !== "blocked" || execution !== "active") {
			return rejected(state, "Only a blocked resumable execution can become In Progress after resume")
		}
		return changed("in_progress")
	}
	if (action.type === "execution_completed") {
		if (state !== "in_progress" || execution !== "none") {
			return rejected(state, "Review requires completion of the active engineering execution")
		}
		return changed("review")
	}
	if (action.type === "waiting_for_user") {
		if (state !== "in_progress" || execution !== "resumable" || !action.reason.trim()) {
			return rejected(state, "Waiting for User requires a reason and a resumable execution")
		}
		return changed("blocked")
	}
	if (action.type === "technical_failure") {
		if (state !== "in_progress" || execution !== "none" || !action.summary.trim()) {
			return rejected(state, "Technical failure requires a stopped execution and a failure summary")
		}
		return changed("blocked")
	}
	if (action.type === "cancel") {
		if (execution === "none") return rejected(state, "There is no execution to cancel")
		return { allowed: true, state: "ready", effect: "runtime", operation: "cancel_execution" }
	}
	if (action.type === "review_rejected" || action.type === "corrective_work_requested") {
		if (state !== "review" || execution !== "none" || !action.comment.trim()) {
			return rejected(state, "Corrective work requires Review state and a recorded user comment")
		}
		return changed("ready")
	}
	if (action.type === "accept") {
		if (state !== "review" || execution !== "none") {
			return rejected(state, "Only reviewed work without an execution can be accepted")
		}
		return { allowed: true, state: "done", effect: "user_confirmation", operation: "change_state" }
	}

	return rejected(state, "Unsupported transition")
}
