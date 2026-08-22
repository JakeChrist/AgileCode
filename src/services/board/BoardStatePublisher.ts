import {
	compileTicketExecutionInstruction,
	RepositoryBoardService,
	type BoardServiceChange,
	type BoardServiceOptions,
} from "@roo-code/core"
import { access } from "fs/promises"
import { join } from "path"
import {
	boardStateEventSchema,
	boardResultSchema,
	type BoardError,
	type BoardExtensionMessage,
	type BoardScope,
	type BoardSnapshot,
	type BoardStateEvent,
	type BoardRequest,
	type BoardResult,
} from "@roo-code/types"

type ServiceFactory = (scope: BoardScope, options: BoardServiceOptions) => Promise<RepositoryBoardService>
type StoreExists = (scope: BoardScope) => Promise<boolean>
type CompileExecution = typeof compileTicketExecutionInstruction

/** Owns the selected board service and only publishes snapshots for that selection. */
export class BoardStatePublisher {
	private service: RepositoryBoardService | undefined
	private selection = 0
	private revision = 0
	private readonly subscribers = new Set<(message: BoardExtensionMessage) => unknown>()
	private latestMessage: BoardExtensionMessage | undefined
	private readonly requests = new Map<string, Promise<BoardResult>>()

	constructor(
		post?: (message: BoardExtensionMessage) => unknown,
		private readonly createService: ServiceFactory = RepositoryBoardService.create,
		private readonly storeExists: StoreExists = async (scope) => {
			try {
				await access(join(scope.rootPath, ".agilecode"))
				return true
			} catch {
				return false
			}
		},
		private readonly compileExecution: CompileExecution = compileTicketExecutionInstruction,
	) {
		if (post) this.subscribers.add(post)
	}

	/** Connects another board rendering context to this authoritative session. */
	subscribe(post: (message: BoardExtensionMessage) => unknown): () => void {
		this.subscribers.add(post)
		if (this.latestMessage) void post(this.latestMessage)
		return () => this.subscribers.delete(post)
	}

	async select(scope: BoardScope): Promise<void> {
		const selection = ++this.selection
		this.revision = 0
		this.service?.dispose()
		this.service = undefined
		await this.publish({ type: "board_state_changed", boardId: scope.id, scope, revision: 0, status: "loading" })

		try {
			const service = await this.createService(scope, {
				onDidChange: (change) => this.onChange(selection, scope, change),
				onDidError: (error) => this.onError(selection, scope, error),
			})
			if (selection !== this.selection) {
				service.dispose()
				return
			}
			this.service = service
			await this.publishReady(scope, service.state, service.recoveryDiagnostics)
		} catch (error) {
			if (selection !== this.selection) return
			if (!(await this.storeExists(scope))) {
				if (selection !== this.selection) return
				await this.publish({
					type: "board_state_changed",
					boardId: scope.id,
					scope,
					revision: ++this.revision,
					status: "uninitialized",
				})
				return
			}
			await this.onError(selection, scope, error)
		}
	}

	/** Returns a defensive snapshot only when the requested identity is selected. */
	readSnapshot(boardId: string): BoardSnapshot | undefined {
		if (!this.service || this.service.state.manifest.scope.id !== boardId) return undefined
		const state = this.service.state
		return {
			scope: state.manifest.scope,
			board: state.board,
			settings: state.settings,
			activeTickets: state.activeTickets,
			archivedTickets: state.archivedTickets,
			diagnostics: this.service.recoveryDiagnostics,
		}
	}

	readRevision(boardId: string): number | undefined {
		return this.service?.state.manifest.scope.id === boardId ? this.revision : undefined
	}

	/** Executes an agent mutation against the exact board revision it inspected. */
	async executeAgentRequest(request: BoardRequest, expectedRevision: number): Promise<BoardResult> {
		if (this.readRevision(request.boardId) !== expectedRevision) {
			return boardResultSchema.parse({
				requestId: request.requestId,
				boardId: request.boardId,
				operation: request.operation,
				ok: false,
				error: {
					operation: request.operation,
					code: "conflict",
					message:
						"The board changed after it was inspected. Inspect it again and retry with the new revision.",
					retryable: true,
				},
			})
		}
		return this.executeRequest(request)
	}

	/** Executes a correlated mutation once; repeated activation shares the same outcome. */
	async handleRequest(request: BoardRequest): Promise<void> {
		const key = `${request.boardId}:${request.requestId}`
		let pending = this.requests.get(key)
		if (!pending) {
			pending = this.executeRequest(request)
			this.requests.set(key, pending)
		}
		await this.publish({ type: "board_result", result: await pending })
	}

	/** Runs a non-persisting improvement through the same correlated, duplicate-safe request channel. */
	async handleImprovement(
		request: Extract<BoardRequest, { operation: "improve_ticket_draft" }>,
		improve: () => Promise<import("@roo-code/types").TicketStatementOfWork>,
	): Promise<void> {
		const key = `${request.boardId}:${request.requestId}`
		let pending = this.requests.get(key)
		if (!pending) {
			pending = (async (): Promise<BoardResult> => {
				try {
					return boardResultSchema.parse({
						requestId: request.requestId,
						boardId: request.boardId,
						operation: request.operation,
						ok: true,
						draft: await improve(),
					})
				} catch (error) {
					return boardResultSchema.parse({
						requestId: request.requestId,
						boardId: request.boardId,
						operation: request.operation,
						ok: false,
						error: {
							operation: request.operation,
							code: "execution_failed",
							message: error instanceof Error ? error.message : String(error),
							retryable: true,
						},
					})
				}
			})()
			this.requests.set(key, pending)
		}
		await this.publish({ type: "board_result", result: await pending })
	}

	/** Preflights an authoritative ticket, starts or resumes exactly one task, then records the confirmed association. */
	async handleExecution(
		request: Extract<BoardRequest, { operation: "start_ticket_execution" }>,
		execute: (
			instruction: string,
			rootPath: string,
			resumeHistoryItemId?: string,
		) => Promise<{ historyItemId: string }>,
	): Promise<void> {
		const key = `${request.boardId}:${request.requestId}`
		let pending = this.requests.get(key)
		if (!pending) {
			pending = (async (): Promise<BoardResult> => {
				const base = {
					requestId: request.requestId,
					boardId: request.boardId,
					operation: request.operation,
				} as const
				try {
					const scope = this.service?.state.manifest.scope
					if (!scope || scope.id !== request.boardId) {
						throw new Error("Select the ticket's repository board before executing it.")
					}
					const preflight = await this.compileExecution(scope, request.ticketId, {
						selectedBoardId: request.boardId,
					})
					if (!preflight.ok) throw new Error(preflight.message)

					const resumeHistoryItemId =
						preflight.ticket.lifecycle?.state === "blocked"
							? preflight.ticket.execution.historyItemIds.at(-1)
							: undefined
					const { historyItemId } = resumeHistoryItemId
						? await execute(preflight.instruction, scope.rootPath, resumeHistoryItemId)
						: await execute(preflight.instruction, scope.rootPath)
					const started = resumeHistoryItemId
						? await this.service!.resumeExecution(request.ticketId, historyItemId)
						: await this.service!.startExecution(request.ticketId, historyItemId)
					if (!started.ok) throw new Error(started.message)
					const ticket = started.state.activeTickets.find(({ id }) => id === request.ticketId)
					if (!ticket) throw new Error(`Started ticket ${request.ticketId} was not found after persistence.`)
					return boardResultSchema.parse({ ...base, ok: true, ticket, historyItemId })
				} catch (error) {
					return boardResultSchema.parse({
						...base,
						ok: false,
						error: {
							operation: request.operation,
							code: "execution_failed",
							message: error instanceof Error ? error.message : String(error),
							retryable: true,
						},
					})
				}
			})()
			this.requests.set(key, pending)
		}
		await this.publish({ type: "board_result", result: await pending })
	}

	private async executeRequest(request: BoardRequest): Promise<BoardResult> {
		if (
			request.operation !== "create_ticket" &&
			request.operation !== "update_ticket" &&
			request.operation !== "decompose_work" &&
			request.operation !== "move_ticket" &&
			request.operation !== "reorder_tickets" &&
			request.operation !== "block_ticket" &&
			request.operation !== "record_review_feedback" &&
			request.operation !== "archive_ticket" &&
			request.operation !== "restore_ticket" &&
			request.operation !== "delete_ticket"
		) {
			throw new Error(`Unsupported board operation: ${request.operation}`)
		}
		const base = { requestId: request.requestId, boardId: request.boardId, operation: request.operation } as const
		if (!this.service || this.service.state.manifest.scope.id !== request.boardId) {
			return {
				...base,
				ok: false,
				error: {
					operation: request.operation,
					code: "board_not_found",
					message: "Select the target board before creating a ticket.",
					retryable: false,
				},
			}
		}
		const result =
			request.operation === "decompose_work" && !request.createApprovedSet
				? { ok: true as const, value: [], state: this.service.state }
				: request.operation === "decompose_work"
					? await this.service.createTicketSet(request.proposal)
					: request.operation === "create_ticket"
						? await this.service.createFromStatementOfWork(
								request.ticket,
								request.initialState,
								request.originatingReview,
							)
						: request.operation === "update_ticket"
							? await this.service.updateStatementOfWork(
									request.ticketId,
									request.statementOfWork,
									this.executionState(request.ticketId),
								)
							: request.operation === "move_ticket"
								? await this.service.moveToPosition(
										request.ticketId,
										request.destination,
										request.position,
										"user",
										this.executionState(request.ticketId),
									)
								: request.operation === "block_ticket"
									? await this.service.manualBlock(
											request.ticketId,
											request.reason,
											"agent",
											this.executionState(request.ticketId),
											request.position,
										)
									: request.operation === "record_review_feedback"
										? await this.service.addReviewComment(
												request.ticketId,
												request.comment,
												request.author,
											)
										: request.operation === "archive_ticket"
											? await this.service.archive(
													request.ticketId,
													this.executionState(request.ticketId),
												)
											: request.operation === "restore_ticket"
												? await this.service.restore(request.ticketId)
												: request.operation === "delete_ticket"
													? await this.service.deletePermanently(
															request.ticketId,
															request.confirmed,
														)
													: await this.service.reorder(
															request.state,
															request.orderedIds,
															request.expectedOrder,
														)
		if (result.ok) {
			if (request.operation === "decompose_work")
				return boardResultSchema.parse({ ...base, ok: true, proposal: request.proposal, created: result.value })
			if (request.operation === "reorder_tickets")
				return boardResultSchema.parse({ ...base, ok: true, board: result.state.board })
			if (
				request.operation === "move_ticket" ||
				request.operation === "block_ticket" ||
				request.operation === "archive_ticket" ||
				request.operation === "restore_ticket"
			) {
				return boardResultSchema.parse({
					...base,
					ok: true,
					ticket: [...result.state.activeTickets, ...result.state.archivedTickets].find(
						({ id }) => id === request.ticketId,
					)!,
					board: result.state.board,
				})
			}
			if (request.operation === "delete_ticket") {
				return boardResultSchema.parse({
					...base,
					ok: true,
					ticketId: request.ticketId,
					board: result.state.board,
				})
			}
			return boardResultSchema.parse({ ...base, ok: true, ticket: result.value })
		}
		return boardResultSchema.parse({
			...base,
			ok: false,
			error: {
				operation: request.operation,
				code:
					result.code === "invalid-ticket"
						? "invalid_request"
						: result.code === "transition-rejected"
							? "invalid_transition"
							: result.code === "conflict"
								? "conflict"
								: "persistence_failed",
				message: result.message,
				retryable: result.code === "persistence-failed",
			},
		})
	}

	private executionState(ticketId: string): "none" | "active" | "resumable" {
		const ticket = this.service?.state.activeTickets.find(({ id }) => id === ticketId)
		if (ticket?.lifecycle.state === "in_progress") return "active"
		if (ticket?.lifecycle.state === "blocked" && ticket.execution.historyItemIds.length > 0) return "resumable"
		return "none"
	}

	dispose(): void {
		this.selection++
		this.service?.dispose()
		this.service = undefined
	}

	private async onChange(selection: number, scope: BoardScope, change: BoardServiceChange): Promise<void> {
		if (selection !== this.selection) return
		await this.publishReady(scope, change.state, change.diagnostics)
	}

	private async publishReady(
		scope: BoardScope,
		state: RepositoryBoardService["state"],
		diagnostics: RepositoryBoardService["recoveryDiagnostics"],
	): Promise<void> {
		const snapshot: BoardSnapshot = {
			scope,
			board: state.board,
			settings: state.settings,
			activeTickets: state.activeTickets,
			archivedTickets: state.archivedTickets,
			diagnostics,
		}
		await this.publish({
			type: "board_state_changed",
			boardId: scope.id,
			scope,
			revision: ++this.revision,
			status: "ready",
			snapshot,
		})
	}

	private async onError(selection: number, scope: BoardScope, cause: unknown): Promise<void> {
		if (selection !== this.selection) return
		const message = cause instanceof Error ? cause.message : String(cause)
		const error: BoardError = {
			operation: "load_board",
			code: "persistence_failed",
			message,
			retryable: true,
		}
		await this.publish({
			type: "board_state_changed",
			boardId: scope.id,
			scope,
			revision: ++this.revision,
			status: "error",
			error,
			diagnostics: [message],
		})
	}

	private async publish(message: BoardExtensionMessage): Promise<void> {
		const parsed = message.type === "board_state_changed" ? boardStateEventSchema.parse(message) : message
		if (parsed.type === "board_state_changed") this.latestMessage = parsed
		await Promise.all([...this.subscribers].map(async (post) => post(parsed)))
	}
}
