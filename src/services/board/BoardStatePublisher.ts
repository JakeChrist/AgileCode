import { RepositoryBoardService, type BoardServiceChange, type BoardServiceOptions } from "@roo-code/core"
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

	private async executeRequest(request: BoardRequest): Promise<BoardResult> {
		if (
			request.operation !== "create_ticket" &&
			request.operation !== "update_ticket" &&
			request.operation !== "move_ticket" &&
			request.operation !== "reorder_tickets"
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
			request.operation === "create_ticket"
				? await this.service.createFromStatementOfWork(request.ticket)
				: request.operation === "update_ticket"
					? await this.service.updateStatementOfWork(request.ticketId, request.statementOfWork)
					: request.operation === "move_ticket"
						? await this.service.moveToPosition(
								request.ticketId,
								request.destination,
								request.position,
								"user",
								this.executionState(request.ticketId),
							)
						: await this.service.reorder(request.state, request.orderedIds, request.expectedOrder)
		if (result.ok) {
			if (request.operation === "reorder_tickets")
				return boardResultSchema.parse({ ...base, ok: true, board: result.state.board })
			if (request.operation === "move_ticket") {
				return boardResultSchema.parse({
					...base,
					ok: true,
					ticket: result.state.activeTickets.find(({ id }) => id === request.ticketId)!,
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
