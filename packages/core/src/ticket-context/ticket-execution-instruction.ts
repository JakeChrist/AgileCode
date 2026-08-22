import {
	getTicketStatementOfWorkLock,
	findExecutionSlotOccupant,
	validateTicketExecutionEligibility,
	type BoardScope,
	type Ticket,
	type TicketStatementOfWork,
} from "@roo-code/types"

import { RepositoryBoardService } from "../project-store/board-service.js"

export type TicketExecutionInstructionFailureCode =
	| "repository-mismatch"
	| "ticket-not-found"
	| "ticket-archived"
	| "ticket-not-ready"
	| "ticket-locked"
	| "dependency-blocked"
	| "execution-slot-occupied"
	| "repository-unavailable"

export type TicketExecutionInstructionResult =
	| { ok: true; instruction: string; ticket: Ticket; board: BoardScope }
	| { ok: false; code: TicketExecutionInstructionFailureCode; message: string }

export interface CompileTicketExecutionInstructionOptions {
	/** Board identifier selected by the caller when the scope was resolved. */
	selectedBoardId: string
	/** Test seam; production callers should use the repository service created from the selected scope. */
	createService?: typeof RepositoryBoardService.create
}

const section = (heading: string, value: string | readonly string[] | undefined): string[] => {
	if (value === undefined || value.length === 0) return []
	const content = Array.isArray(value) ? value.map((item) => `- ${item}`).join("\n") : value
	return [`## ${heading}\n${content as string}`]
}

/**
 * Serializes only the approved ticket contract into the initial task instruction.
 * No chat or task-history input is accepted, so unrelated transcripts cannot leak in.
 */
export function formatTicketExecutionInstruction(ticket: Ticket, board: BoardScope): string {
	const sow: TicketStatementOfWork = ticket.statementOfWork
	return [
		"# AgileCode Ticket Execution",
		[
			`Ticket: ${ticket.id}`,
			`Selected repository board: ${board.id}`,
			`Repository root: ${board.rootPath}`,
			"Workflow: Board-Aware Agile Ticket Implementation",
		].join("\n"),
		"The approved ticket below is authoritative for this execution. Implement exactly its statement of work. Material scope expansion requires explicit handling through the board-aware workflow; do not silently incorporate it.",
		...section("Title", sow.title),
		...section("Objective", sow.objective),
		...section("Context", sow.context),
		...section("Included Scope", sow.includedScope),
		...section("Out of Scope", sow.excludedScope),
		...section("Requirements", sow.requirements),
		...section("Deliverables", sow.deliverables),
		...section("Constraints and Preserved Behavior", sow.constraints),
		...section("Dependencies", sow.dependencies),
		...section("Acceptance Criteria", sow.acceptanceCriteria),
		...section("Validation", sow.validation),
	]
		.join("\n\n")
		.trim()
}

/** Loads, verifies, and compiles one repository-owned ticket into a task-ready instruction. */
export async function compileTicketExecutionInstruction(
	board: BoardScope,
	ticketId: string,
	options: CompileTicketExecutionInstructionOptions,
): Promise<TicketExecutionInstructionResult> {
	if (options.selectedBoardId !== board.id) {
		return {
			ok: false,
			code: "repository-mismatch",
			message: `Selected board ${options.selectedBoardId} does not match repository board ${board.id}. Re-select the ticket from the correct repository board.`,
		}
	}

	let service: RepositoryBoardService
	try {
		service = await (options.createService ?? RepositoryBoardService.create)(board, { watch: false })
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error)
		return {
			ok: false,
			code: message.toLowerCase().includes("identity mismatch")
				? "repository-mismatch"
				: "repository-unavailable",
			message: `The selected repository board could not be verified: ${message}`,
		}
	}

	try {
		const active = service.listTickets()
		const ticket = active.find(({ id }) => id === ticketId)
		if (!ticket) {
			const archived = service.listArchivedTickets().some(({ id }) => id === ticketId)
			return archived
				? {
						ok: false,
						code: "ticket-archived",
						message: `Ticket ${ticketId} is archived. Restore and ready it before starting execution.`,
					}
				: {
						ok: false,
						code: "ticket-not-found",
						message: `Ticket ${ticketId} was not found on selected board ${board.id}. Refresh the board and select an existing ticket.`,
					}
		}
		const occupant = findExecutionSlotOccupant(active, ticketId)
		if (occupant) {
			return {
				ok: false,
				code: "execution-slot-occupied",
				message: `Ticket ${occupant.id} already has an active or resumable execution on this repository board. Complete or cancel it before starting ${ticketId}.`,
			}
		}

		const resumable = ticket.lifecycle.state === "blocked" && ticket.execution.historyItemIds.length > 0
		const lock = getTicketStatementOfWorkLock(ticket)
		if (lock.locked && !resumable) {
			return { ok: false, code: "ticket-locked", message: lock.reason ?? `Ticket ${ticketId} is locked.` }
		}
		if (ticket.lifecycle.state !== "ready" && !resumable) {
			return {
				ok: false,
				code: "ticket-not-ready",
				message: `Ticket ${ticketId} is ${ticket.lifecycle.state}, not ready. Move it to Ready after resolving its readiness issues.`,
			}
		}

		const eligibility = validateTicketExecutionEligibility(ticket, active)
		if (!eligibility.ready) {
			const dependencyBlocked = eligibility.issues.some(({ field }) => field === "dependencies")
			return {
				ok: false,
				code: dependencyBlocked ? "dependency-blocked" : "ticket-not-ready",
				message: `Ticket ${ticketId} is not executable: ${eligibility.issues.map(({ message }) => message).join("; ")}`,
			}
		}

		return { ok: true, instruction: formatTicketExecutionInstruction(ticket, board), ticket, board }
	} finally {
		await service.dispose()
	}
}
