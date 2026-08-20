import type { Ticket, TicketStatementOfWork, TicketWorkflowState } from "./ticket.js"

export type TicketReadinessField =
	| "objective"
	| "includedScope"
	| "requirements"
	| "acceptanceCriteria"
	| "validation"
	| "dependencies"

export interface TicketValidationIssue {
	field: TicketReadinessField
	message: string
}

export interface TicketReadinessResult {
	ready: boolean
	issues: TicketValidationIssue[]
}

export interface TicketReadinessOptions {
	/** Current states of referenced tickets. Omit this when checking content without resolving dependencies. */
	dependencyStates?: Readonly<Record<string, TicketWorkflowState | undefined>>
}

/**
 * Applies execution-readiness rules separately from the persistence schema. This
 * deliberately leaves context, constraints, deliverables, and excluded scope
 * optional: extra prose is not a substitute for an executable statement of work.
 */
export function validateTicketReadiness(
	statementOfWork: TicketStatementOfWork,
	options: TicketReadinessOptions = {},
): TicketReadinessResult {
	const issues: TicketValidationIssue[] = []
	if (!statementOfWork.objective.trim()) {
		issues.push({ field: "objective", message: "Objective must clearly describe the intended outcome" })
	}
	if (statementOfWork.includedScope.length === 0) {
		issues.push({ field: "includedScope", message: "Included scope must identify the work that is in scope" })
	}
	if (statementOfWork.requirements.length === 0) {
		issues.push({ field: "requirements", message: "Requirements must contain at least one concrete requirement" })
	}
	if (statementOfWork.acceptanceCriteria.length === 0) {
		issues.push({
			field: "acceptanceCriteria",
			message: "Acceptance criteria must contain at least one verifiable outcome",
		})
	}
	if (statementOfWork.validation.length === 0) {
		issues.push({ field: "validation", message: "Validation must explain how the work will be verified" })
	}

	if (options.dependencyStates) {
		const unresolved = statementOfWork.dependencies.filter(
			(dependency) => options.dependencyStates?.[dependency] !== "done",
		)
		if (unresolved.length > 0) {
			issues.push({
				field: "dependencies",
				message: `Required dependencies are unresolved: ${unresolved.join(", ")}`,
			})
		}
	}

	return { ready: issues.length === 0, issues }
}

/** Checks both statement-of-work completeness and all dependencies known to the supplied board. */
export function validateTicketExecutionEligibility(ticket: Ticket, tickets: readonly Ticket[]): TicketReadinessResult {
	const dependencyStates = Object.fromEntries(
		tickets.map(({ id, lifecycle }) => [
			id,
			lifecycle.state === "archived" ? lifecycle.archivedFrom : lifecycle.state,
		]),
	)
	return validateTicketReadiness(ticket.statementOfWork, { dependencyStates })
}
