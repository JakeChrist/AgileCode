export type ProhibitedPreparationAction = "file_edit" | "side_effect_command" | "task_start" | "board_mutation"

export interface PreparationPermissionReport {
	code: "preparation_permission_denied"
	action: ProhibitedPreparationAction
	message: string
}

export class PreparationPermissionError extends Error {
	readonly code = "preparation_permission_denied" as const

	constructor(
		readonly action: ProhibitedPreparationAction,
		message = `Ticket preparation cannot perform ${action.replaceAll("_", " ")}.`,
	) {
		super(message)
		this.name = "PreparationPermissionError"
	}

	toReport(): PreparationPermissionReport {
		return { code: this.code, action: this.action, message: this.message }
	}
}

/**
 * The only capabilities available while preparing a codebase-informed ticket.
 *
 * Keeping the inspection callback separate from every prohibited operation makes
 * the boundary enforceable in code rather than relying on instructions to a model.
 */
export class TicketPreparationPermissionBoundary {
	inspect<T>(inspection: () => Promise<T>): Promise<T> {
		return inspection()
	}

	editProjectFiles(): never {
		throw new PreparationPermissionError("file_edit")
	}

	runSideEffectCommand(): never {
		throw new PreparationPermissionError("side_effect_command")
	}

	startTask(): never {
		throw new PreparationPermissionError("task_start")
	}

	mutateBoard(): never {
		throw new PreparationPermissionError("board_mutation")
	}
}

export const ticketPreparationPermissions = new TicketPreparationPermissionBoundary()
