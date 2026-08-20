import {
	agileCodeRepositorySettingsSchema,
	decideTicketTransition,
	getTicketStatementOfWorkLock,
	ticketSchema,
	validateTicketExecutionEligibility,
	type ActiveTicketWorkflowState,
	type AgileCodeProjectStore,
	type AgileCodeRepositorySettings,
	type BoardScope,
	type Ticket,
	type TicketStatementOfWork,
	type TicketExecutionState,
	type TicketTransitionResult,
	type TicketWorkflowAction,
} from "@roo-code/types"

import { writeBoardOrdering, writeRepositorySettings } from "./board-persistence.js"
import { loadAgileCodeStore, type AgileCodeStoreDiagnostic } from "./project-store.js"
import {
	archiveTicket,
	createTicket,
	deleteActiveTicket,
	deleteArchivedTicket,
	readTicket,
	restoreTicket,
	updateTicket,
} from "./ticket-persistence.js"
import { watchAgileCodeStore, type AgileCodeStoreWatcher } from "./store-watcher.js"
import { createTicketStorageName, generateTicketId } from "./ticket-identity.js"
import { validateTicketDependencies } from "./ticket-dependencies.js"

export type BoardServiceFailureCode =
	| "board-identity-mismatch"
	| "invalid-ticket"
	| "conflict"
	| "not-found"
	| "transition-rejected"
	| "persistence-failed"

export type BoardServiceResult<T> =
	| { ok: true; value: T; state: AgileCodeProjectStore }
	| { ok: false; code: BoardServiceFailureCode; message: string; cause?: unknown }

export interface BoardServiceChange {
	source: "internal" | "external"
	state: AgileCodeProjectStore
	diagnostics: AgileCodeStoreDiagnostic[]
}

export interface BoardServiceOptions {
	onDidChange?(change: BoardServiceChange): void | Promise<void>
	onDidError?(error: unknown): void | Promise<void>
	watch?: boolean
	now?: () => Date
	generateId?: () => string
	/** Test seam for simulating failure between ticket and board persistence. */
	beforeCreateBoardWrite?: () => void | Promise<void>
	/** Test seam for simulating an edit failure after a readiness reclassification. */
	beforeEditTicketWrite?: () => void | Promise<void>
}

/** Authoritative application boundary for one repository-owned AgileCode board. */
export class RepositoryBoardService {
	private watcher: AgileCodeStoreWatcher | undefined
	private current: AgileCodeProjectStore
	private diagnostics: AgileCodeStoreDiagnostic[]
	private mutation = Promise.resolve()
	private disposed = false

	private constructor(
		private readonly scope: BoardScope,
		loaded: { store: AgileCodeProjectStore; diagnostics: AgileCodeStoreDiagnostic[] },
		private readonly options: BoardServiceOptions,
	) {
		this.current = loaded.store
		this.diagnostics = loaded.diagnostics
	}

	static async create(scope: BoardScope, options: BoardServiceOptions = {}): Promise<RepositoryBoardService> {
		const loaded = await loadAgileCodeStore(scope.rootPath)
		if (loaded.store.manifest.scope.id !== scope.id) {
			throw new Error(`Board identity mismatch: expected ${scope.id}, found ${loaded.store.manifest.scope.id}`)
		}
		const service = new RepositoryBoardService(scope, loaded, options)
		if (options.watch !== false) await service.startWatcher()
		return service
	}

	get state(): AgileCodeProjectStore {
		return structuredClone(this.current)
	}

	get recoveryDiagnostics(): AgileCodeStoreDiagnostic[] {
		return structuredClone(this.diagnostics)
	}

	listTickets(): Ticket[] {
		return structuredClone(this.current.activeTickets)
	}

	listArchivedTickets(): Ticket[] {
		return structuredClone(this.current.archivedTickets)
	}

	get activeBoard() {
		return structuredClone(this.current.board)
	}

	get repositorySettings(): AgileCodeRepositorySettings {
		return structuredClone(this.current.settings)
	}

	async read(id: string): Promise<BoardServiceResult<Ticket>> {
		return this.query(async () => readTicket(this.scope.rootPath, id))
	}

	async create(ticket: unknown, storageName?: string): Promise<BoardServiceResult<Ticket>> {
		return this.mutate(async () => {
			const parsed = ticketSchema.parse(ticket)
			this.validateDependencies(parsed.id, parsed.statementOfWork.dependencies)
			await createTicket(this.scope.rootPath, parsed, { storageName })
			try {
				const board = structuredClone(this.current.board)
				board.columns[parsed.lifecycle.state as ActiveTicketWorkflowState].push(parsed.id)
				await this.options.beforeCreateBoardWrite?.()
				await writeBoardOrdering(this.scope.rootPath, board)
			} catch (error) {
				await deleteActiveTicket(this.scope.rootPath, parsed.id)
				throw error
			}
			return parsed
		})
	}

	/** Creates a complete durable ticket while keeping identity allocation inside this board boundary. */
	async createFromStatementOfWork(statementOfWork: TicketStatementOfWork): Promise<BoardServiceResult<Ticket>> {
		const now = (this.options.now?.() ?? new Date()).toISOString()
		const ticket: Ticket = {
			formatVersion: 1,
			id: (this.options.generateId ?? generateTicketId)(),
			statementOfWork,
			lifecycle: {
				state: "backlog",
				createdAt: now,
				reviewComments: [],
				blockedReasons: [],
				failedAttempts: [],
			},
			execution: { historyItemIds: [] },
		}
		return this.create(ticket, createTicketStorageName(ticket.id, statementOfWork.title))
	}

	async update(id: string, replacement: unknown): Promise<BoardServiceResult<Ticket>> {
		return this.mutate(async () => {
			const before = await readTicket(this.scope.rootPath, id)
			const parsed = ticketSchema.parse(replacement)
			if (parsed.lifecycle.state !== before.lifecycle.state) {
				throw new ServiceError("transition-rejected", "Lifecycle state must be changed through transition()")
			}
			this.assertStatementOfWorkEditable(before, parsed.statementOfWork)
			return updateTicket(this.scope.rootPath, id, parsed)
		})
	}

	async updateDependencies(id: string, dependencies: readonly string[]): Promise<BoardServiceResult<Ticket>> {
		return this.mutate(async () => {
			const ticket = await readTicket(this.scope.rootPath, id)
			this.assertStatementOfWorkEditable(ticket)
			this.validateDependencies(id, dependencies)
			ticket.statementOfWork.dependencies = [...dependencies]
			return updateTicket(this.scope.rootPath, id, ticket)
		})
	}

	/** Updates only customer-authored content, preserving all system-managed ticket metadata. */
	async updateStatementOfWork(
		id: string,
		statementOfWork: TicketStatementOfWork,
		execution: TicketExecutionState = "none",
	): Promise<BoardServiceResult<Ticket>> {
		return this.mutate(async () => {
			const before = await readTicket(this.scope.rootPath, id)
			this.assertStatementOfWorkEditable(before)
			if (execution !== "none") {
				throw new ServiceError(
					"transition-rejected",
					"Tickets with an active or resumable execution may not be edited",
				)
			}

			const parsedStatement = ticketSchema.shape.statementOfWork.parse(statementOfWork)
			this.validateDependencies(id, parsedStatement.dependencies)
			const next: Ticket = { ...before, statementOfWork: parsedStatement }
			const readiness = validateTicketExecutionEligibility(next, [
				...this.current.activeTickets,
				...this.current.archivedTickets,
			])
			if (before.lifecycle.state === "ready" && !readiness.ready) next.lifecycle.state = "backlog"

			if (next.lifecycle.state === before.lifecycle.state) {
				await this.options.beforeEditTicketWrite?.()
				return updateTicket(this.scope.rootPath, id, next)
			}

			const previousBoard = structuredClone(this.current.board)
			const board = structuredClone(previousBoard)
			board.columns.ready = board.columns.ready.filter((ticketId) => ticketId !== id)
			board.columns.backlog.push(id)
			await writeBoardOrdering(this.scope.rootPath, board)
			try {
				await this.options.beforeEditTicketWrite?.()
				return await updateTicket(this.scope.rootPath, id, next)
			} catch (error) {
				await writeBoardOrdering(this.scope.rootPath, previousBoard)
				throw error
			}
		})
	}

	async addReviewComment(id: string, comment: string, author?: string): Promise<BoardServiceResult<Ticket>> {
		return this.mutate(async () => {
			const ticket = await readTicket(this.scope.rootPath, id)
			const createdAt = (this.options.now?.() ?? new Date()).toISOString()
			ticket.lifecycle.reviewComments.push({ id: `${id}-${createdAt}`, comment, createdAt, author })
			return updateTicket(this.scope.rootPath, id, ticket)
		})
	}

	async reorder(
		state: ActiveTicketWorkflowState,
		orderedIds: readonly string[],
		expectedOrder: readonly string[],
	): Promise<BoardServiceResult<void>> {
		return this.mutate(async () => {
			// Read immediately before applying the mutation. A watcher notification may
			// lag behind another board instance's write, so the cached state is not a
			// safe concurrency precondition.
			const authoritative = await loadAgileCodeStore(this.scope.rootPath)
			const existing = authoritative.store.board.columns[state]
			if (JSON.stringify(existing) !== JSON.stringify(expectedOrder)) {
				throw new ServiceError("conflict", `The ${state} column changed; reload it before reordering`)
			}
			if (
				orderedIds.length !== existing.length ||
				new Set(orderedIds).size !== existing.length ||
				orderedIds.some((id) => !existing.includes(id))
			) {
				throw new ServiceError("invalid-ticket", `Reorder must contain every ${state} ticket exactly once`)
			}
			const board = structuredClone(authoritative.store.board)
			board.columns[state] = [...orderedIds]
			await writeBoardOrdering(this.scope.rootPath, board)
		})
	}

	async transition(
		id: string,
		action: TicketWorkflowAction,
		execution: TicketExecutionState = "none",
	): Promise<BoardServiceResult<TicketTransitionResult>> {
		return this.mutate(async () => {
			const ticket = await readTicket(this.scope.rootPath, id)
			const requestsReadiness =
				(action.type === "move" && (action.destination === "ready" || action.destination === "in_progress")) ||
				action.type === "execution_started" ||
				action.type === "execution_resumed"
			if (requestsReadiness) {
				const readiness = validateTicketExecutionEligibility(ticket, [
					...this.current.activeTickets,
					...this.current.archivedTickets,
				])
				if (!readiness.ready) {
					throw new ServiceError(
						"transition-rejected",
						readiness.issues.map(({ field, message }) => `${field}: ${message}`).join("; "),
					)
				}
			}
			const decision = decideTicketTransition(
				{ state: ticket.lifecycle.state, execution, archivedFrom: ticket.lifecycle.archivedFrom },
				action,
			)
			if (!decision.allowed) throw new ServiceError("transition-rejected", decision.reason)
			if (decision.effect === "runtime") return decision

			const board = structuredClone(this.current.board)
			if (decision.operation === "delete") {
				await deleteArchivedTicket(this.scope.rootPath, id)
				board.archiveOrder = board.archiveOrder.filter((entry) => entry !== id)
				await writeBoardOrdering(this.scope.rootPath, board)
				return decision
			}

			const now = (this.options.now?.() ?? new Date()).toISOString()
			const next: Ticket = structuredClone(ticket)
			next.lifecycle.state = decision.state
			if (action.type === "archive") {
				next.lifecycle.archivedAt = now
				next.lifecycle.archivedFrom = decision.archivedFrom
				await archiveTicket(this.scope.rootPath, id, next)
			} else if (action.type === "restore") {
				delete next.lifecycle.archivedAt
				delete next.lifecycle.archivedFrom
				await restoreTicket(this.scope.rootPath, id, next)
			} else {
				if (action.type === "accept") next.lifecycle.acceptedAt = now
				if (action.type === "execution_completed") next.lifecycle.completedAt = now
				if (action.type === "waiting_for_user")
					next.lifecycle.blockedReasons.push({ reason: action.reason, createdAt: now })
				if (action.type === "technical_failure")
					next.lifecycle.blockedReasons.push({ reason: action.summary, createdAt: now })
				if (action.type === "review_rejected" || action.type === "corrective_work_requested") {
					next.lifecycle.reviewComments.push({ id: `${id}-${now}`, comment: action.comment, createdAt: now })
				}
				await updateTicket(this.scope.rootPath, id, next)
			}
			for (const column of Object.values(board.columns)) {
				const index = column.indexOf(id)
				if (index >= 0) column.splice(index, 1)
			}
			board.archiveOrder = board.archiveOrder.filter((entry) => entry !== id)
			if (decision.state === "archived") board.archiveOrder.push(id)
			else board.columns[decision.state].push(id)
			await writeBoardOrdering(this.scope.rootPath, board)
			return decision
		})
	}

	move(
		id: string,
		destination: ActiveTicketWorkflowState,
		actor: "user" | "agent",
		execution: TicketExecutionState = "none",
	) {
		return this.transition(id, { type: "move", destination, actor }, execution)
	}

	block(id: string, reason: string, execution: TicketExecutionState = "resumable") {
		return this.transition(id, { type: "waiting_for_user", reason }, execution)
	}

	archive(id: string, execution: TicketExecutionState = "none") {
		return this.transition(id, { type: "archive" }, execution)
	}

	restore(id: string) {
		return this.transition(id, { type: "restore" })
	}

	deletePermanently(id: string, confirmed: boolean) {
		return this.transition(id, { type: "delete_permanently", confirmed })
	}

	async updateSettings(settings: unknown): Promise<BoardServiceResult<AgileCodeRepositorySettings>> {
		return this.mutate(async () =>
			writeRepositorySettings(this.scope.rootPath, agileCodeRepositorySettingsSchema.parse(settings)),
		)
	}

	dispose(): void {
		this.disposed = true
		this.watcher?.dispose()
	}

	private async query<T>(operation: () => Promise<T>): Promise<BoardServiceResult<T>> {
		try {
			await this.assertIdentity()
			return { ok: true, value: await operation(), state: this.state }
		} catch (error) {
			return failure(error)
		}
	}

	private async mutate<T>(operation: () => Promise<T>): Promise<BoardServiceResult<T>> {
		let release!: () => void
		const previous = this.mutation
		this.mutation = new Promise<void>((resolve) => (release = resolve))
		await previous
		try {
			if (this.disposed) throw new ServiceError("persistence-failed", "Board service is disposed")
			await this.assertIdentity()
			const value = await operation()
			const loaded = await loadAgileCodeStore(this.scope.rootPath)
			this.current = loaded.store
			this.diagnostics = loaded.diagnostics
			await this.options.onDidChange?.({
				source: "internal",
				state: this.state,
				diagnostics: this.recoveryDiagnostics,
			})
			return { ok: true, value, state: this.state }
		} catch (error) {
			return failure(error)
		} finally {
			release()
		}
	}

	private assertStatementOfWorkEditable(ticket: Ticket, replacement?: TicketStatementOfWork): void {
		if (replacement && JSON.stringify(replacement) === JSON.stringify(ticket.statementOfWork)) return
		const lock = getTicketStatementOfWorkLock(ticket)
		if (lock.locked) {
			throw new ServiceError("transition-rejected", lock.reason ?? "This ticket's statement of work is locked")
		}
	}

	private async assertIdentity(): Promise<void> {
		const loaded = await loadAgileCodeStore(this.scope.rootPath)
		if (loaded.store.manifest.scope.id !== this.scope.id) {
			throw new ServiceError(
				"board-identity-mismatch",
				`Board ${this.scope.id} cannot access ${loaded.store.manifest.scope.id}`,
			)
		}
	}

	private validateDependencies(id: string, dependencies: readonly string[]): void {
		const tickets = [...this.current.activeTickets, ...this.current.archivedTickets]
		try {
			validateTicketDependencies(id, dependencies, tickets)
		} catch (error) {
			throw new ServiceError("invalid-ticket", error instanceof Error ? error.message : String(error))
		}
	}

	private async startWatcher(): Promise<void> {
		this.watcher = await watchAgileCodeStore(this.scope.rootPath, {
			onDidChange: async ({ store, diagnostics }) => {
				if (store.manifest.scope.id !== this.scope.id) {
					await this.options.onDidError?.(new Error(`External board identity changed from ${this.scope.id}`))
					return
				}
				if (JSON.stringify(store) === JSON.stringify(this.current)) return
				this.current = store
				this.diagnostics = diagnostics
				await this.options.onDidChange?.({
					source: "external",
					state: this.state,
					diagnostics: this.recoveryDiagnostics,
				})
			},
			onDidError: this.options.onDidError,
		})
	}
}

class ServiceError extends Error {
	constructor(
		readonly code: BoardServiceFailureCode,
		message: string,
	) {
		super(message)
	}
}

function failure(error: unknown): BoardServiceResult<never> {
	if (error instanceof ServiceError) return { ok: false, code: error.code, message: error.message }
	const message = error instanceof Error ? error.message : String(error)
	const code = message.includes("not found")
		? "not-found"
		: message.includes("validation") || message.includes("failed")
			? "invalid-ticket"
			: "persistence-failed"
	return { ok: false, code, message, cause: error }
}
